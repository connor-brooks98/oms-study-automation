"""Source-bounded extraction and deterministic consolidation of objective proposals."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from typing import Protocol

from oms_hub.knowledge.models import EvidenceUnit, SourceRevisionState
from oms_hub.knowledge.policy import SourceScopeError, filter_allowed_evidence
from oms_hub.objectives.models import ObjectiveEdgeType
from oms_hub.providers.contracts import RetrievalScope, TruthMode

OBJECTIVE_EXTRACTION_PROMPT_VERSION = "objective-extraction-v1"
MAX_SOURCE_REVISIONS = 32
MAX_EVIDENCE_UNITS = 2_000
MAX_EVIDENCE_TEXT_CHARACTERS = 20_000
MAX_MODEL_INPUT_CHARACTERS = 200_000
MAX_PROPOSALS = 500

_OBSERVABLE_VERBS = frozenset(
    {
        "apply",
        "calculate",
        "classify",
        "compare",
        "define",
        "describe",
        "diagnose",
        "differentiate",
        "evaluate",
        "explain",
        "identify",
        "interpret",
        "predict",
        "recognize",
        "select",
    }
)
_TOKEN = re.compile(r"[a-z0-9]+")


def _required(value: object, field_name: str, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} must contain at most {maximum} characters")
    return normalized


def _identifiers(
    values: object,
    field_name: str,
    *,
    maximum: int = 256,
) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError(f"{field_name} must be a sequence of identifiers")
    if len(values) > maximum:
        raise ValueError(f"{field_name} must contain at most {maximum} identifiers")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        identifier = _required(value, field_name)
        if identifier not in seen:
            seen.add(identifier)
            result.append(identifier)
    return tuple(result)


def _entities(value: str) -> tuple[str, ...]:
    return tuple(sorted(set(_TOKEN.findall(value.casefold()))))


@dataclass(frozen=True, slots=True)
class SuggestedObjectiveLink:
    edge_type: ObjectiveEdgeType
    target_concept: str

    def __post_init__(self) -> None:
        edge_type = ObjectiveEdgeType(self.edge_type)
        if edge_type not in (
            ObjectiveEdgeType.PREREQUISITE,
            ObjectiveEdgeType.CONTRASTS_WITH,
        ):
            raise ValueError("suggested link must be a prerequisite or contrast")
        object.__setattr__(self, "edge_type", edge_type)
        object.__setattr__(
            self,
            "target_concept",
            _required(self.target_concept, "target_concept"),
        )


@dataclass(frozen=True, slots=True)
class ProposedObjective:
    observable_verb: str
    concept: str
    description: str
    course_id: str
    exam_id: str | None
    lecture_ids: tuple[str, ...]
    source_revision_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    suggested_links: tuple[SuggestedObjectiveLink, ...] = ()
    proposal_id: str = field(init=False)

    def __post_init__(self) -> None:
        verb = _required(self.observable_verb, "observable_verb").casefold()
        if verb not in _OBSERVABLE_VERBS:
            raise ValueError("observable verb must be one approved testable verb")
        concept = _required(self.concept, "concept")
        if not _entities(concept):
            raise ValueError("concept must be testable")
        object.__setattr__(self, "observable_verb", verb)
        object.__setattr__(self, "concept", concept)
        object.__setattr__(
            self,
            "description",
            _required(self.description, "description", 1_000),
        )
        object.__setattr__(self, "course_id", _required(self.course_id, "course_id"))
        if self.exam_id is not None:
            object.__setattr__(self, "exam_id", _required(self.exam_id, "exam_id"))
        limits = {"lecture_ids": 64, "source_revision_ids": 32, "evidence_ids": 256}
        for field_name, maximum in limits.items():
            values = _identifiers(
                getattr(self, field_name),
                field_name,
                maximum=maximum,
            )
            values = tuple(sorted(values)) if field_name == "lecture_ids" else values
            if field_name in ("source_revision_ids", "evidence_ids") and not values:
                raise ValueError(f"{field_name} must not be empty")
            object.__setattr__(self, field_name, values)
        links = tuple(self.suggested_links)
        if len(links) > 32:
            raise ValueError("suggested_links must contain at most 32 links")
        if not all(isinstance(link, SuggestedObjectiveLink) for link in links):
            raise ValueError("suggested_links must contain suggested link records")
        object.__setattr__(self, "suggested_links", tuple(dict.fromkeys(links)))
        object.__setattr__(self, "proposal_id", _proposal_id(self))


@dataclass(frozen=True, slots=True)
class ObjectiveEvidenceInput:
    evidence_id: str
    source_revision_id: str
    text: str
    course_id: str | None
    exam_id: str | None
    lecture_id: str | None


@dataclass(frozen=True, slots=True)
class ConsolidationCandidate:
    text: str
    evidence_ids: tuple[str, ...]


class ObjectiveProposalGenerator(Protocol):
    def propose(
        self,
        prompt_version: str,
        evidence: tuple[ObjectiveEvidenceInput, ...],
    ) -> tuple[ProposedObjective, ...]: ...


class ObjectiveConsolidator(Protocol):
    def should_merge(
        self,
        left: ConsolidationCandidate,
        right: ConsolidationCandidate,
    ) -> bool: ...


class _KnowledgeReader(Protocol):
    def get_revision(self, revision_id: str) -> object | None: ...

    def list_evidence(self, revision_id: str) -> tuple[EvidenceUnit, ...]: ...


class ObjectiveExtractor:
    def __init__(
        self,
        knowledge: _KnowledgeReader,
        generator: ObjectiveProposalGenerator,
        consolidator: ObjectiveConsolidator | None = None,
    ) -> None:
        self.knowledge = knowledge
        self.generator = generator
        self.consolidator = consolidator

    def extract(self, source_revision_ids: tuple[str, ...]) -> tuple[ProposedObjective, ...]:
        revision_ids = _identifiers(
            source_revision_ids,
            "source_revision_ids",
            maximum=MAX_SOURCE_REVISIONS,
        )
        if not revision_ids:
            raise ValueError("source_revision_ids must not be empty")
        units: dict[str, EvidenceUnit] = {}
        inputs: list[ObjectiveEvidenceInput] = []
        input_characters = 0
        for revision_id in revision_ids:
            revision = self.knowledge.get_revision(revision_id)
            if revision is None:
                raise KeyError(revision_id)
            if getattr(revision, "state", None) is not SourceRevisionState.READY:
                raise ValueError("objective extraction requires ready source revisions")
            for unit in self.knowledge.list_evidence(revision_id):
                if not unit.supports_medical_claims or unit.retired_at is not None:
                    continue
                if len(inputs) >= MAX_EVIDENCE_UNITS:
                    raise ValueError(
                        f"objective extraction accepts at most {MAX_EVIDENCE_UNITS} evidence units"
                    )
                if len(unit.normalized_text) > MAX_EVIDENCE_TEXT_CHARACTERS:
                    raise ValueError(
                        "objective extraction evidence text exceeds the per-unit limit"
                    )
                metadata = (
                    unit.evidence_id,
                    unit.source_revision_id,
                    unit.course_id or "",
                    unit.exam_id or "",
                    unit.lecture_id or "",
                )
                if any(len(value) > 200 for value in metadata):
                    raise ValueError("objective extraction evidence metadata exceeds its limit")
                input_characters += len(unit.normalized_text) + sum(map(len, metadata))
                if input_characters > MAX_MODEL_INPUT_CHARACTERS:
                    raise ValueError("objective extraction model input exceeds its limit")
                units[unit.evidence_id] = unit
                inputs.append(
                    ObjectiveEvidenceInput(
                        unit.evidence_id,
                        unit.source_revision_id,
                        unit.normalized_text,
                        unit.course_id,
                        unit.exam_id,
                        unit.lecture_id,
                    )
                )
        if not inputs:
            raise ValueError("objective extraction requires evidence")

        proposals = self.generator.propose(
            OBJECTIVE_EXTRACTION_PROMPT_VERSION,
            tuple(inputs),
        )
        if not isinstance(proposals, tuple):
            raise ValueError("generator must return a tuple of proposed objectives")
        if len(proposals) > MAX_PROPOSALS:
            raise ValueError(f"generator must return at most {MAX_PROPOSALS} proposals")
        validated = tuple(
            self._validate_proposal(proposal, revision_ids, units)
            for proposal in proposals
        )
        return self._deduplicate(validated)

    def _validate_proposal(
        self,
        proposal: ProposedObjective,
        requested_revisions: tuple[str, ...],
        units: dict[str, EvidenceUnit],
    ) -> ProposedObjective:
        if not isinstance(proposal, ProposedObjective):
            raise ValueError("generator returned an invalid proposed objective")
        if not set(proposal.source_revision_ids).issubset(requested_revisions):
            raise ValueError("proposal source revision is outside the extraction request")
        missing = set(proposal.evidence_ids) - set(units)
        if missing:
            raise KeyError(f"unknown evidence IDs: {sorted(missing)}")
        selected = tuple(units[evidence_id] for evidence_id in proposal.evidence_ids)
        used_revisions = {unit.source_revision_id for unit in selected}
        if used_revisions != set(proposal.source_revision_ids):
            raise ValueError("proposal source revisions must match its evidence")
        scope = RetrievalScope(
            proposal.course_id,
            proposal.exam_id,
            proposal.lecture_ids,
            TruthMode.COURSE_AND_LITERATURE,
            proposal.source_revision_ids,
        )
        try:
            allowed = filter_allowed_evidence(scope, selected)
        except SourceScopeError as error:
            raise ValueError(str(error)) from error
        if len(allowed) != len(selected) or any(
            unit.retired_at is not None or not unit.supports_medical_claims
            for unit in allowed
        ):
            raise ValueError("proposal evidence authority is not allowed")
        return proposal

    def _deduplicate(
        self,
        proposals: tuple[ProposedObjective, ...],
    ) -> tuple[ProposedObjective, ...]:
        exact: list[ProposedObjective] = []
        exact_indices: dict[tuple[object, ...], int] = {}
        for proposal in proposals:
            key = _key(proposal)
            index = exact_indices.get(key)
            if index is None:
                exact_indices[key] = len(exact)
                exact.append(proposal)
            else:
                exact[index] = _combine(exact[index], proposal)

        result: list[ProposedObjective] = []
        for proposal in exact:
            ambiguous = next(
                (
                    index
                    for index, item in enumerate(result)
                    if _near_duplicate(item, proposal)
                ),
                None,
            )
            if ambiguous is not None and self.consolidator is not None:
                existing = result[ambiguous]
                if self.consolidator.should_merge(
                    _candidate(existing),
                    _candidate(proposal),
                ):
                    result[ambiguous] = _combine(existing, proposal)
                    continue
            result.append(proposal)
        return tuple(result)


def _scope(proposal: ProposedObjective) -> tuple[object, ...]:
    return proposal.course_id, proposal.exam_id, proposal.lecture_ids


def _key(proposal: ProposedObjective) -> tuple[object, ...]:
    return proposal.observable_verb, _entities(proposal.concept), _scope(proposal)


def _proposal_id(proposal: ProposedObjective) -> str:
    payload = json.dumps(_key(proposal), separators=(",", ":"), ensure_ascii=True)
    return "obj_" + hashlib.sha256(payload.encode()).hexdigest()[:24]


def _ordered_union(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*left, *right)))


def _combine(left: ProposedObjective, right: ProposedObjective) -> ProposedObjective:
    return replace(
        left,
        source_revision_ids=_ordered_union(
            left.source_revision_ids,
            right.source_revision_ids,
        ),
        evidence_ids=_ordered_union(left.evidence_ids, right.evidence_ids),
        suggested_links=tuple(dict.fromkeys((*left.suggested_links, *right.suggested_links))),
    )


def _near_duplicate(left: ProposedObjective, right: ProposedObjective) -> bool:
    if left.observable_verb != right.observable_verb or _scope(left) != _scope(right):
        return False
    if not set(left.evidence_ids).intersection(right.evidence_ids):
        return False
    left_entities = set(_entities(left.concept))
    right_entities = set(_entities(right.concept))
    overlap = len(left_entities.intersection(right_entities))
    return bool(overlap) and overlap / len(left_entities.union(right_entities)) >= 0.6


def _candidate(proposal: ProposedObjective) -> ConsolidationCandidate:
    return ConsolidationCandidate(
        f"{proposal.observable_verb} {proposal.concept}: {proposal.description}",
        proposal.evidence_ids,
    )
