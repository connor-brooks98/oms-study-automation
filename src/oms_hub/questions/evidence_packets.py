"""Bounded, policy-checked evidence packets for board-question generation."""

from __future__ import annotations

import hashlib
import json
import unicodedata
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
_MAX_COVER_STATES_PER_MASK = 128

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
    signatures: frozenset[str]
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
    candidate_roles = {
        item.unit.evidence_id: _role_mask(item, objectives) for item in ordered
    }
    available_role_mask = 0
    for mask in candidate_roles.values():
        available_role_mask |= mask
    if full_role_mask & ~available_role_mask:
        raise QuestionEvidenceError("bounded packet lacks required evidence coverage")

    states: dict[int, tuple[_CoverState, ...]] = {
        0: (_CoverState((), frozenset(), 0, 0),)
    }
    for item in ordered:
        item_roles = candidate_roles[item.unit.evidence_id]
        if not item_roles:
            continue
        prior_states = tuple(
            (mask, state) for mask, frontier in states.items() for state in frontier
        )
        for mask, state in prior_states:
            new_mask = mask | item_roles
            if new_mask == mask or len(state.selected) >= MAX_EVIDENCE_UNITS:
                continue
            signature = _claim_signature(item.unit.normalized_text)
            added_characters = (
                0
                if signature in state.signatures
                else len(_normalized_text(item.unit.normalized_text))
            )
            if state.characters + added_characters > MAX_EVIDENCE_CHARACTERS:
                continue
            candidate = _CoverState(
                selected=state.selected + (item,),
                signatures=state.signatures | {signature},
                characters=state.characters + added_characters,
                priority=state.priority + item.unit.source_priority,
            )
            states[new_mask] = _bounded_frontier(
                (*states.get(new_mask, ()), candidate)
            )

    complete = states.get(full_role_mask, ())
    if not complete:
        raise QuestionEvidenceError("bounded packet cannot retain required evidence coverage")
    best = min(complete, key=_cover_state_key).selected
    if len(best) > MAX_EVIDENCE_UNITS:
        raise QuestionEvidenceError("required evidence cover exceeds 16 canonical units")

    selected = {item.unit.evidence_id: item for item in best}
    for item in ordered:
        evidence_id = item.unit.evidence_id
        if evidence_id in selected:
            continue
        if len(selected) >= MAX_EVIDENCE_UNITS:
            break
        projected = (*selected.values(), item)
        projected_characters = _evidence_characters(tuple(projected))
        if projected_characters > MAX_EVIDENCE_CHARACTERS:
            continue
        selected[evidence_id] = item

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


def _bounded_frontier(states: tuple[_CoverState, ...]) -> tuple[_CoverState, ...]:
    unique = {
        tuple(item.unit.evidence_id for item in state.selected): state for state in states
    }
    frontier: list[_CoverState] = []
    for state in unique.values():
        if any(_dominates(other, state) for other in unique.values() if other is not state):
            continue
        frontier.append(state)
    if len(frontier) <= _MAX_COVER_STATES_PER_MASK:
        return tuple(sorted(frontier, key=_cover_state_key))
    by_quality = sorted(frontier, key=_cover_state_key)[:64]
    by_budget = sorted(
        frontier,
        key=lambda state: (
            state.characters,
            -state.priority,
            len(state.selected),
            _selected_ids(state),
        ),
    )[:64]
    retained = {_selected_ids(state): state for state in (*by_quality, *by_budget)}
    return tuple(sorted(retained.values(), key=_cover_state_key))


def _dominates(first: _CoverState, second: _CoverState) -> bool:
    return (
        first.signatures == second.signatures
        and first.characters <= second.characters
        and first.priority >= second.priority
        and len(first.selected) <= len(second.selected)
        and (
            first.characters < second.characters
            or first.priority > second.priority
            or len(first.selected) < len(second.selected)
        )
    )


def _cover_state_key(state: _CoverState) -> tuple[int, int, tuple[str, ...]]:
    return (-state.priority, len(state.selected), _selected_ids(state))


def _selected_ids(state: _CoverState) -> tuple[str, ...]:
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
