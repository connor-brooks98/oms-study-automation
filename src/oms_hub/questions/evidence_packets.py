"""Bounded, policy-checked evidence packets for board-question generation."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from bisect import bisect_left
from dataclasses import dataclass

from oms_hub.knowledge.ids import sha256_text
from oms_hub.knowledge.models import EvidenceUnit, SourceRevisionState
from oms_hub.knowledge.policy import SourceScopeError, filter_allowed_evidence, validate_scope
from oms_hub.knowledge.repository import KnowledgeRepository
from oms_hub.providers.contracts import (
    AuthorityClass,
    EvidenceRef,
    RetrievalProvider,
    RetrievalRequest,
    RetrievalResult,
    RetrievalScope,
)
from oms_hub.questions.models import QuestionMode

MAX_EVIDENCE_UNITS = 16
MAX_EVIDENCE_CHARACTERS = 18_000
MAX_INTEGRATED_OBJECTIVES = 4

__all__ = (
    "MAX_EVIDENCE_CHARACTERS",
    "MAX_EVIDENCE_UNITS",
    "MAX_INTEGRATED_OBJECTIVES",
    "QuestionEvidenceError",
    "QuestionEvidenceLocator",
    "QuestionEvidencePacket",
    "QuestionEvidencePacketBuilder",
    "QuestionGenerationRequest",
    "QuestionObjective",
    "QuestionPacketEvidence",
)


class QuestionEvidenceError(RuntimeError):
    """Raised when generation cannot receive a trustworthy bounded packet."""


@dataclass(frozen=True, slots=True)
class QuestionObjective:
    objective_id: str
    display_name: str

    def __post_init__(self) -> None:
        _require_text(self.objective_id, "objective_id")
        _require_text(self.display_name, "objective display_name")


@dataclass(frozen=True, slots=True)
class QuestionGenerationRequest:
    objectives: tuple[QuestionObjective, ...]
    mode: QuestionMode
    difficulty: int
    scope: RetrievalScope
    correct_answer_concept: str
    correct_answer_concept_signature: str
    prior_tested_concept_signatures: tuple[str, ...] = ()
    forbidden_repeat_signatures: tuple[str, ...] = ()
    style_constraints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.objectives, tuple) or not self.objectives:
            raise ValueError("objectives must be a nonempty tuple")
        if any(not isinstance(objective, QuestionObjective) for objective in self.objectives):
            raise ValueError("objectives must contain QuestionObjective values")
        objective_ids = tuple(objective.objective_id for objective in self.objectives)
        if len(objective_ids) != len(set(objective_ids)):
            raise ValueError("objective IDs must be unique")
        if not isinstance(self.mode, QuestionMode):
            raise ValueError("mode must be a QuestionMode")
        if isinstance(self.difficulty, bool) or not isinstance(self.difficulty, int):
            raise ValueError("difficulty must be an integer from 1 through 5")
        if not 1 <= self.difficulty <= 5:
            raise ValueError("difficulty must be from 1 through 5")
        validate_scope(self.scope)
        _require_text(self.correct_answer_concept, "correct_answer_concept")
        _require_text(
            self.correct_answer_concept_signature,
            "correct_answer_concept_signature",
        )
        _require_text_tuple(
            self.prior_tested_concept_signatures,
            "prior_tested_concept_signatures",
        )
        _require_text_tuple(
            self.forbidden_repeat_signatures,
            "forbidden_repeat_signatures",
        )
        _require_text_tuple(self.style_constraints, "style_constraints")


@dataclass(frozen=True, slots=True)
class QuestionEvidenceLocator:
    evidence_id: str
    source_revision_id: str
    authority_class: AuthorityClass
    locator_kind: str
    locator_value: str
    checksum: str


@dataclass(frozen=True, slots=True)
class QuestionPacketEvidence:
    claim_signature: str
    normalized_text: str
    objective_ids: tuple[str, ...]
    locators: tuple[QuestionEvidenceLocator, ...]
    source_priority: int
    supports_correct_answer_concept: bool

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(locator.evidence_id for locator in self.locators)


@dataclass(frozen=True, slots=True)
class QuestionEvidencePacket:
    objectives: tuple[QuestionObjective, ...]
    mode: QuestionMode
    difficulty: int
    evidence: tuple[QuestionPacketEvidence, ...]
    source_snapshot_hash: str
    prior_tested_concept_signatures: tuple[str, ...]
    forbidden_repeat_signatures: tuple[str, ...]
    style_constraints: tuple[str, ...]
    omitted_evidence_ids: tuple[str, ...]

    @property
    def objective_ids(self) -> tuple[str, ...]:
        return tuple(objective.objective_id for objective in self.objectives)

    @property
    def objective_display_names(self) -> tuple[str, ...]:
        return tuple(objective.display_name for objective in self.objectives)

    @property
    def allowed_evidence(self) -> tuple[QuestionPacketEvidence, ...]:
        return self.evidence


@dataclass(frozen=True, slots=True)
class _ResolvedEvidence:
    unit: EvidenceUnit
    supports_correct_answer_concept: bool
    objective_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class _CoverState:
    selected: tuple[_ResolvedEvidence, ...]
    characters: int
    priority: int


@dataclass(frozen=True, slots=True)
class _GroupOption:
    roles: int
    selected: tuple[_ResolvedEvidence, ...]
    characters: int
    priority: int


class QuestionEvidencePacketBuilder:
    def __init__(
        self,
        provider: RetrievalProvider,
        knowledge: KnowledgeRepository,
    ) -> None:
        self.provider = provider
        self.knowledge = knowledge

    async def build(self, request: QuestionGenerationRequest) -> QuestionEvidencePacket:
        if not isinstance(request, QuestionGenerationRequest):
            raise QuestionEvidenceError("request must be a QuestionGenerationRequest")
        if len(request.objectives) > MAX_INTEGRATED_OBJECTIVES:
            raise QuestionEvidenceError("question packets support at most 4 objectives")
        if (
            request.correct_answer_concept_signature
            in request.forbidden_repeat_signatures
        ):
            raise QuestionEvidenceError("correct-answer concept has a forbidden repeat signature")

        resolved: dict[str, _ResolvedEvidence] = {}
        concept_result = await self._retrieve(request.correct_answer_concept, request.scope)
        if concept_result.insufficient_evidence or not concept_result.evidence:
            raise QuestionEvidenceError("no evidence for correct-answer concept")
        self._merge_result(
            resolved,
            concept_result,
            request.scope,
            supports_correct_answer_concept=True,
            objective_id=None,
        )

        for objective in request.objectives:
            objective_result = await self._retrieve(objective.display_name, request.scope)
            if objective_result.insufficient_evidence or not objective_result.evidence:
                raise QuestionEvidenceError(
                    f"objective {objective.objective_id!r} has no evidence"
                )
            self._merge_result(
                resolved,
                objective_result,
                request.scope,
                supports_correct_answer_concept=False,
                objective_id=objective.objective_id,
            )

        if not resolved:
            raise QuestionEvidenceError("no evidence is available for question generation")
        selected, omitted_ids = _select_bounded(
            tuple(resolved.values()),
            request.objectives,
        )
        packet_evidence = _group_evidence(selected)
        self._revalidate_selected(selected, request.scope)
        return QuestionEvidencePacket(
            objectives=request.objectives,
            mode=request.mode,
            difficulty=request.difficulty,
            evidence=packet_evidence,
            source_snapshot_hash=_source_snapshot_hash(packet_evidence),
            prior_tested_concept_signatures=request.prior_tested_concept_signatures,
            forbidden_repeat_signatures=request.forbidden_repeat_signatures,
            style_constraints=request.style_constraints,
            omitted_evidence_ids=omitted_ids,
        )

    async def _retrieve(
        self,
        query: str,
        scope: RetrievalScope,
    ) -> RetrievalResult:
        result = await self.provider.retrieve(
            RetrievalRequest(query=query, scope=scope, maximum_evidence=MAX_EVIDENCE_UNITS)
        )
        if not isinstance(result, RetrievalResult):
            raise QuestionEvidenceError("retrieval provider returned an invalid result")
        if not isinstance(result.evidence, tuple):
            raise QuestionEvidenceError("retrieval evidence must be a tuple")
        if len(result.evidence) > MAX_EVIDENCE_UNITS:
            raise QuestionEvidenceError("retrieval result must contain at most 16 refs")
        if type(result.insufficient_evidence) is not bool:
            raise QuestionEvidenceError("retrieval insufficiency flag must be a bool")
        if (
            not isinstance(result.provider_request_id, str)
            or not result.provider_request_id.strip()
        ):
            raise QuestionEvidenceError("retrieval provider request id must be nonblank")
        for ref in result.evidence:
            _validate_ref_shape(ref)
        return result

    def _merge_result(
        self,
        destination: dict[str, _ResolvedEvidence],
        result: RetrievalResult,
        scope: RetrievalScope,
        *,
        supports_correct_answer_concept: bool,
        objective_id: str | None,
    ) -> None:
        for ref in result.evidence:
            unit = self._resolve_ref(ref, scope)
            previous = destination.get(unit.evidence_id)
            objective_ids = frozenset() if objective_id is None else frozenset({objective_id})
            supports_concept = supports_correct_answer_concept
            if previous is not None:
                objective_ids = previous.objective_ids | objective_ids
                supports_concept = previous.supports_correct_answer_concept or supports_concept
            destination[unit.evidence_id] = _ResolvedEvidence(
                unit,
                supports_concept,
                objective_ids,
            )

    def _resolve_ref(self, ref: EvidenceRef, scope: RetrievalScope) -> EvidenceUnit:
        if not isinstance(ref, EvidenceRef):
            raise QuestionEvidenceError("retrieval returned an invalid evidence reference")
        revision = self.knowledge.get_revision(ref.source_revision_id)
        if revision is None:
            raise QuestionEvidenceError(
                f"unknown source revision {ref.source_revision_id!r}"
            )
        if revision.state is not SourceRevisionState.READY:
            raise QuestionEvidenceError(
                f"source revision {ref.source_revision_id!r} is not READY"
            )
        unit = next(
            (
                candidate
                for candidate in self.knowledge.list_evidence(ref.source_revision_id)
                if candidate.evidence_id == ref.evidence_id
            ),
            None,
        )
        if unit is None:
            raise QuestionEvidenceError(f"unknown evidence {ref.evidence_id!r}")
        if unit.retired_at is not None:
            raise QuestionEvidenceError(f"evidence {ref.evidence_id!r} is retired")
        if ref.authority_class is not unit.authority_class:
            raise QuestionEvidenceError(
                "provider evidence authority does not match canonical authority"
            )
        if (
            ref.locator_kind != unit.locator.kind.value
            or ref.locator_value != unit.locator.value
        ):
            raise QuestionEvidenceError(
                "provider evidence locator does not match canonical locator"
            )
        expected_checksum = f"sha256:{unit.content_sha256}"
        if (
            ref.excerpt != unit.normalized_text
            or ref.checksum != expected_checksum
            or sha256_text(ref.excerpt) != unit.content_sha256
        ):
            raise QuestionEvidenceError(
                "provider evidence excerpt or checksum does not match canonical content"
            )
        if unit.authority_class is AuthorityClass.GENERATED_ARTIFACT:
            raise QuestionEvidenceError("generated artifact cannot provide question authority")
        try:
            allowed = filter_allowed_evidence(scope, (unit,))
        except SourceScopeError as error:
            raise QuestionEvidenceError(f"evidence scope rejected: {error}") from error
        if not allowed:
            raise QuestionEvidenceError(
                f"{scope.truth_mode.value} does not allow {unit.authority_class.value} evidence"
            )
        if not _normalized_text(unit.normalized_text):
            raise QuestionEvidenceError("canonical evidence text must not be blank")
        return unit

    def _revalidate_selected(
        self,
        selected: tuple[_ResolvedEvidence, ...],
        scope: RetrievalScope,
    ) -> None:
        for item in selected:
            unit = item.unit
            current = self._resolve_ref(
                EvidenceRef(
                    evidence_id=unit.evidence_id,
                    source_revision_id=unit.source_revision_id,
                    authority_class=unit.authority_class,
                    locator_kind=unit.locator.kind.value,
                    locator_value=unit.locator.value,
                    excerpt=unit.normalized_text,
                    checksum=f"sha256:{unit.content_sha256}",
                ),
                scope,
            )
            if current != unit:
                raise QuestionEvidenceError("canonical evidence changed during packet build")


def _group_evidence(
    resolved: tuple[_ResolvedEvidence, ...],
) -> tuple[QuestionPacketEvidence, ...]:
    groups: dict[str, list[_ResolvedEvidence]] = {}
    for item in resolved:
        signature = _claim_signature(item.unit.normalized_text)
        groups.setdefault(signature, []).append(item)

    packet_units: list[QuestionPacketEvidence] = []
    for signature, items in groups.items():
        ordered = sorted(
            items,
            key=lambda item: (-item.unit.source_priority, item.unit.evidence_id),
        )
        representative = ordered[0].unit
        locators = tuple(
            sorted(
                (
                    QuestionEvidenceLocator(
                        evidence_id=item.unit.evidence_id,
                        source_revision_id=item.unit.source_revision_id,
                        authority_class=item.unit.authority_class,
                        locator_kind=item.unit.locator.kind.value,
                        locator_value=item.unit.locator.value,
                        checksum=f"sha256:{item.unit.content_sha256}",
                    )
                    for item in items
                ),
                key=lambda locator: (
                    locator.evidence_id,
                    locator.source_revision_id,
                    locator.locator_kind,
                    locator.locator_value,
                ),
            )
        )
        packet_units.append(
            QuestionPacketEvidence(
                claim_signature=signature,
                normalized_text=_normalized_text(representative.normalized_text),
                objective_ids=tuple(
                    sorted(
                        {
                            objective_id
                            for item in items
                            for objective_id in item.objective_ids
                        }
                    )
                ),
                locators=locators,
                source_priority=max(item.unit.source_priority for item in items),
                supports_correct_answer_concept=any(
                    item.supports_correct_answer_concept for item in items
                ),
            )
        )
    return tuple(sorted(packet_units, key=_packet_sort_key))


def _select_bounded(
    candidates: tuple[_ResolvedEvidence, ...],
    objectives: tuple[QuestionObjective, ...],
) -> tuple[tuple[_ResolvedEvidence, ...], tuple[str, ...]]:
    ordered = tuple(sorted(candidates, key=_resolved_sort_key))
    role_count = 1 + len(objectives)
    full_role_mask = (1 << role_count) - 1
    available_role_mask = 0
    for item in ordered:
        available_role_mask |= _role_mask(item, objectives)
    if full_role_mask & ~available_role_mask:
        raise QuestionEvidenceError("bounded packet lacks required evidence coverage")

    groups: dict[str, list[_ResolvedEvidence]] = {}
    for item in ordered:
        groups.setdefault(_claim_signature(item.unit.normalized_text), []).append(item)
    states: dict[tuple[int, int], tuple[_CoverState, ...]] = {
        (0, 0): (_CoverState((), 0, 0),)
    }
    for signature in sorted(groups):
        options = _group_options(tuple(groups[signature]), objectives)
        next_states = dict(states)
        for (mask, count), frontier in states.items():
            for state in frontier:
                for option in options:
                    new_mask = mask | option.roles
                    new_count = count + len(option.selected)
                    if (
                        new_mask == mask
                        or new_count > MAX_EVIDENCE_UNITS
                        or state.characters + option.characters
                        > MAX_EVIDENCE_CHARACTERS
                    ):
                        continue
                    candidate = _CoverState(
                        selected=state.selected + option.selected,
                        characters=state.characters + option.characters,
                        priority=state.priority + option.priority,
                    )
                    key = (new_mask, new_count)
                    next_states[key] = _insert_required_state(
                        next_states.get(key, ()),
                        candidate,
                    )
        states = next_states

    complete = tuple(
        state
        for (mask, _), frontier in states.items()
        if mask == full_role_mask
        for state in frontier
    )
    if not complete:
        raise QuestionEvidenceError("bounded packet cannot retain required evidence coverage")
    best = min(
        complete,
        key=lambda state: (
            -state.priority,
            state.characters,
            len(state.selected),
            _state_ids(state),
        ),
    ).selected
    if len(best) > MAX_EVIDENCE_UNITS:
        raise QuestionEvidenceError("required evidence cover exceeds 16 canonical units")

    selected = {item.unit.evidence_id: item for item in best}
    deferred: list[_ResolvedEvidence] = []
    for item in ordered:
        evidence_id = item.unit.evidence_id
        if evidence_id in selected:
            continue
        if _add_or_swap(selected, item, objectives):
            _retry_deferred(selected, deferred, objectives)
        else:
            deferred.append(item)

    selected_ids = set(selected)
    omitted_ids = tuple(
        sorted(
            item.unit.evidence_id
            for item in candidates
            if item.unit.evidence_id not in selected_ids
        )
    )
    return tuple(sorted(selected.values(), key=_resolved_sort_key)), omitted_ids


def _role_mask(
    item: _ResolvedEvidence,
    objectives: tuple[QuestionObjective, ...],
) -> int:
    mask = 1 if item.supports_correct_answer_concept else 0
    for index, objective in enumerate(objectives, start=1):
        if objective.objective_id in item.objective_ids:
            mask |= 1 << index
    return mask


def _insert_required_state(
    frontier: tuple[_CoverState, ...],
    candidate: _CoverState,
) -> tuple[_CoverState, ...]:
    characters = [state.characters for state in frontier]
    index = bisect_left(characters, candidate.characters)
    if index and _required_dominates(frontier[index - 1], candidate):
        return frontier
    retained = list(frontier)
    if index < len(retained) and retained[index].characters == candidate.characters:
        if _required_dominates(retained[index], candidate):
            return frontier
        retained.pop(index)
    while index < len(retained) and _required_dominates(candidate, retained[index]):
        retained.pop(index)
    retained.insert(index, candidate)
    return tuple(retained)


def _required_dominates(first: _CoverState, second: _CoverState) -> bool:
    if first.characters > second.characters or first.priority < second.priority:
        return False
    if first.characters < second.characters or first.priority > second.priority:
        return True
    return _state_ids(first) < _state_ids(second)


def _add_or_swap(
    selected: dict[str, _ResolvedEvidence],
    candidate: _ResolvedEvidence,
    objectives: tuple[QuestionObjective, ...],
) -> bool:
    if len(selected) < MAX_EVIDENCE_UNITS:
        projected = (*selected.values(), candidate)
        if _evidence_characters(tuple(projected)) <= MAX_EVIDENCE_CHARACTERS:
            selected[candidate.unit.evidence_id] = candidate
            return True
    return _swap_for_priority(selected, candidate, objectives)


def _retry_deferred(
    selected: dict[str, _ResolvedEvidence],
    deferred: list[_ResolvedEvidence],
    objectives: tuple[QuestionObjective, ...],
) -> None:
    changed = True
    while changed:
        changed = False
        for candidate in tuple(deferred):
            if _add_or_swap(selected, candidate, objectives):
                deferred.remove(candidate)
                changed = True
                break


def _swap_for_priority(
    selected: dict[str, _ResolvedEvidence],
    candidate: _ResolvedEvidence,
    objectives: tuple[QuestionObjective, ...],
) -> bool:
    removable_items = sorted(
        selected.values(),
        key=lambda item: (item.unit.source_priority, item.unit.evidence_id),
    )
    for removable in removable_items:
        if candidate.unit.source_priority <= removable.unit.source_priority:
            return False
        projected = tuple(
            item
            for evidence_id, item in selected.items()
            if evidence_id != removable.unit.evidence_id
        ) + (candidate,)
        if not _covers_required_roles(projected, objectives):
            continue
        if _evidence_characters(projected) > MAX_EVIDENCE_CHARACTERS:
            continue
        del selected[removable.unit.evidence_id]
        selected[candidate.unit.evidence_id] = candidate
        return True
    return False


def _covers_required_roles(
    selected: tuple[_ResolvedEvidence, ...],
    objectives: tuple[QuestionObjective, ...],
) -> bool:
    available = 0
    for item in selected:
        available |= _role_mask(item, objectives)
    return available == (1 << (1 + len(objectives))) - 1


def _group_options(
    items: tuple[_ResolvedEvidence, ...],
    objectives: tuple[QuestionObjective, ...],
) -> tuple[_GroupOption, ...]:
    ordered = tuple(sorted(items, key=_resolved_sort_key))
    options: dict[tuple[int, int, int], _GroupOption] = {}
    for representative_index, representative in enumerate(ordered):
        characters = len(_normalized_text(representative.unit.normalized_text))
        if characters > MAX_EVIDENCE_CHARACTERS:
            continue
        representative_roles = _role_mask(representative, objectives)
        member_states: dict[int, tuple[_ResolvedEvidence, ...]] = {
            representative_roles: (representative,)
        }
        for member in ordered[representative_index + 1 :]:
            member_roles = _role_mask(member, objectives)
            prior_states = tuple(member_states.items())
            for roles, selected in prior_states:
                combined_roles = roles | member_roles
                if combined_roles == roles:
                    continue
                candidate = (*selected, member)
                current_members = member_states.get(combined_roles)
                if current_members is None or _member_selection_key(
                    candidate
                ) < _member_selection_key(current_members):
                    member_states[combined_roles] = candidate
        for roles, selected in member_states.items():
            option = _GroupOption(
                roles=roles,
                selected=selected,
                characters=characters,
                priority=sum(item.unit.source_priority for item in selected),
            )
            key = (roles, len(selected), characters)
            current_option = options.get(key)
            if current_option is None or _group_option_key(option) < _group_option_key(
                current_option
            ):
                options[key] = option
    return tuple(sorted(options.values(), key=_group_option_key))


def _member_selection_key(
    selected: tuple[_ResolvedEvidence, ...],
) -> tuple[int, int, tuple[str, ...]]:
    return (
        len(selected),
        -sum(item.unit.source_priority for item in selected),
        tuple(sorted(item.unit.evidence_id for item in selected)),
    )


def _group_option_key(option: _GroupOption) -> tuple[int, int, int, tuple[str, ...]]:
    return (
        option.characters,
        len(option.selected),
        -option.priority,
        tuple(sorted(item.unit.evidence_id for item in option.selected)),
    )


def _state_ids(state: _CoverState) -> tuple[str, ...]:
    return tuple(sorted(item.unit.evidence_id for item in state.selected))


def _resolved_sort_key(item: _ResolvedEvidence) -> tuple[int, str]:
    return (-item.unit.source_priority, item.unit.evidence_id)


def _evidence_characters(items: tuple[_ResolvedEvidence, ...]) -> int:
    by_signature: dict[str, str] = {}
    for item in sorted(items, key=_resolved_sort_key):
        signature = _claim_signature(item.unit.normalized_text)
        by_signature.setdefault(signature, _normalized_text(item.unit.normalized_text))
    return sum(len(text) for text in by_signature.values())


def _packet_sort_key(unit: QuestionPacketEvidence) -> tuple[int, str, str]:
    return (-unit.source_priority, unit.evidence_ids[0], unit.claim_signature)


def _source_snapshot_hash(evidence: tuple[QuestionPacketEvidence, ...]) -> str:
    snapshot = [
        {
            "authority_class": locator.authority_class.value,
            "checksum": locator.checksum,
            "evidence_id": locator.evidence_id,
            "locator_kind": locator.locator_kind,
            "locator_value": locator.locator_value,
            "source_revision_id": locator.source_revision_id,
        }
        for unit in evidence
        for locator in unit.locators
    ]
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_OPERATOR_TRANSLATION = str.maketrans(
    {
        "≤": "<=",
        "≦": "<=",
        "≥": ">=",
        "≧": ">=",
        "±": "+/-",
        "−": "-",
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
    }
)


def _claim_signature(text: str) -> str:
    comparable = _normalized_text(text).casefold().translate(_OPERATOR_TRANSLATION)
    tokens: list[str] = []
    word: list[str] = []

    def flush_word() -> None:
        if word:
            tokens.append("".join(word))
            word.clear()

    for index, character in enumerate(comparable):
        category = unicodedata.category(character)
        is_decimal_point = (
            character == "."
            and index > 0
            and index + 1 < len(comparable)
            and comparable[index - 1].isdecimal()
            and comparable[index + 1].isdecimal()
        )
        if category[0] in {"L", "M", "N"} or character == "_" or is_decimal_point:
            word.append(character)
            continue
        flush_word()
        if category[0] == "S" or character in "<>=+-/%":
            tokens.append(character)
    flush_word()
    normalized = " ".join(tokens)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _normalized_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be nonblank")


def _require_text_tuple(value: object, field_name: str) -> None:
    if not isinstance(value, tuple):
        raise ValueError(f"{field_name} must be a tuple")
    for item in value:
        _require_text(item, field_name)


def _validate_ref_shape(ref: object) -> None:
    if not isinstance(ref, EvidenceRef):
        raise QuestionEvidenceError("retrieval returned an invalid evidence reference")
    for value in (
        ref.evidence_id,
        ref.source_revision_id,
        ref.locator_kind,
        ref.locator_value,
        ref.excerpt,
        ref.checksum,
    ):
        if not isinstance(value, str) or not value.strip():
            raise QuestionEvidenceError("retrieval evidence reference fields must be nonblank")
    if not isinstance(ref.authority_class, AuthorityClass):
        raise QuestionEvidenceError("retrieval evidence authority must be an AuthorityClass")
