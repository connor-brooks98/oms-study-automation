from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from oms_hub.anki.domain import Candidate, RetrievalPass
from oms_hub.anki.lcl import LectureConcept
from oms_hub.anki.v2_contracts import MissingFactV2

ANKING_CARD_TARGET = 60
CUSTOM_CARD_TARGET = 10

_IMPORTANCE = {
    "core": 3,
    "high": 3,
    "supporting": 2,
    "medium": 2,
    "low": 1,
}
_RETRIEVAL = {
    RetrievalPass.PASS_1: 3,
    RetrievalPass.PASS_2_RESCUE: 2,
    RetrievalPass.CONVERGENCE: 1,
}


@dataclass(frozen=True, slots=True)
class ExistingCardSelection:
    candidates: tuple[Candidate, ...]
    target: int
    eligible_count: int
    selected_count: int
    coverage_floor: int

    @property
    def overflow_count(self) -> int:
        return max(0, self.selected_count - self.target)


@dataclass(frozen=True, slots=True)
class GapFactSelection:
    selected_by_concept: Mapping[str, tuple[MissingFactV2, ...]]
    deferred_by_concept: Mapping[str, tuple[MissingFactV2, ...]]
    target: int
    selected_count: int

    @property
    def overflow_count(self) -> int:
        return max(0, self.selected_count - self.target)


def apply_existing_card_target(
    candidates: Sequence[Candidate],
    concepts: Sequence[LectureConcept],
    *,
    target: int = ANKING_CARD_TARGET,
) -> ExistingCardSelection:
    """Select existing cards coverage-first while treating the target as soft."""
    if target < 1:
        raise ValueError("existing-card target must be positive")
    eligible = tuple(candidate for candidate in candidates if candidate.selected)
    eligible_by_id = {candidate.note_id: candidate for candidate in eligible}
    concept_by_id = {concept.concept_id: concept for concept in concepts}
    matches = {
        candidate.note_id: _candidate_concepts(candidate)
        for candidate in eligible
    }

    selected_ids: set[int] = set()
    coverage_ids: set[int] = set()
    ordered_concepts = sorted(
        enumerate(concepts),
        key=lambda item: (
            -_concept_priority(item[1]),
            item[0],
        ),
    )
    for _, concept in ordered_concepts:
        options = [
            candidate
            for candidate in eligible
            if concept.concept_id in matches[candidate.note_id]
        ]
        if not options:
            continue
        chosen = max(
            options,
            key=lambda candidate: _candidate_rank(candidate, concept_by_id),
        )
        selected_ids.add(chosen.note_id)
        coverage_ids.add(chosen.note_id)

    remaining = sorted(
        (
            candidate
            for candidate in eligible
            if candidate.note_id not in selected_ids
        ),
        key=lambda candidate: _candidate_rank(candidate, concept_by_id),
        reverse=True,
    )
    available_slots = max(0, target - len(selected_ids))
    selected_ids.update(
        candidate.note_id for candidate in remaining[:available_slots]
    )

    budgeted: list[Candidate] = []
    for candidate in candidates:
        if candidate.note_id not in eligible_by_id:
            budgeted.append(candidate)
            continue
        selected = candidate.note_id in selected_ids
        provenance = dict(candidate.provenance)
        provenance["selection_budget"] = {
            "mode": "soft_target",
            "target": target,
            "selected": selected,
            "retained_for_concept_coverage": (
                candidate.note_id in coverage_ids
            ),
            "overflow": selected and len(selected_ids) > target,
        }
        budgeted.append(
            replace(candidate, selected=selected, provenance=provenance)
        )

    return ExistingCardSelection(
        candidates=tuple(budgeted),
        target=target,
        eligible_count=len(eligible),
        selected_count=len(selected_ids),
        coverage_floor=len(coverage_ids),
    )


def prioritize_gap_facts(
    concepts: Sequence[LectureConcept],
    missing_by_concept: Mapping[str, Sequence[MissingFactV2]],
    selected_existing_concepts: set[str],
    *,
    target: int = CUSTOM_CARD_TARGET,
) -> GapFactSelection:
    """Prioritize custom-card facts and permit narrow high-value overflow."""
    if target < 0:
        raise ValueError("custom-card target cannot be negative")
    ordered = sorted(
        enumerate(concepts),
        key=lambda item: (-_concept_priority(item[1]), item[0]),
    )
    ordered_concepts = [concept for _, concept in ordered]
    queue: list[tuple[LectureConcept, MissingFactV2]] = []
    depth = max(
        (len(missing_by_concept.get(concept.concept_id, ())) for concept in concepts),
        default=0,
    )
    for fact_index in range(depth):
        for concept in ordered_concepts:
            facts = missing_by_concept.get(concept.concept_id, ())
            if fact_index < len(facts):
                queue.append((concept, facts[fact_index]))

    selected_keys = {
        (concept.concept_id, fact.fact_id)
        for concept, fact in queue[:target]
    }
    represented = {concept_id for concept_id, _ in selected_keys}
    for concept in ordered_concepts:
        facts = missing_by_concept.get(concept.concept_id, ())
        if (
            facts
            and _concept_priority(concept) == 3
            and concept.concept_id not in selected_existing_concepts
            and concept.concept_id not in represented
        ):
            selected_keys.add((concept.concept_id, facts[0].fact_id))
            represented.add(concept.concept_id)

    selected: dict[str, tuple[MissingFactV2, ...]] = {}
    deferred: dict[str, tuple[MissingFactV2, ...]] = {}
    for concept in concepts:
        facts = tuple(missing_by_concept.get(concept.concept_id, ()))
        selected[concept.concept_id] = tuple(
            fact
            for fact in facts
            if (concept.concept_id, fact.fact_id) in selected_keys
        )
        deferred[concept.concept_id] = tuple(
            fact
            for fact in facts
            if (concept.concept_id, fact.fact_id) not in selected_keys
        )
    return GapFactSelection(
        selected_by_concept=selected,
        deferred_by_concept=deferred,
        target=target,
        selected_count=len(selected_keys),
    )


def candidate_concept_ids(candidate: Candidate) -> set[str]:
    return _candidate_concepts(candidate)


def _candidate_concepts(candidate: Candidate) -> set[str]:
    concept_ids = {
        str(item["concept_id"])
        for item in candidate.provenance.get("concept_matches", ())
        if isinstance(item, dict)
        and item.get("concept_id")
        and item.get("selected", True)
    }
    if not concept_ids and candidate.best_concept_id:
        concept_ids.add(candidate.best_concept_id)
    return concept_ids


def _concept_priority(concept: LectureConcept) -> int:
    return _IMPORTANCE.get(concept.importance, 1)


def _candidate_rank(
    candidate: Candidate,
    concept_by_id: Mapping[str, LectureConcept],
) -> tuple[int, float, int, int]:
    priority = max(
        (
            _concept_priority(concept_by_id[concept_id])
            for concept_id in _candidate_concepts(candidate)
            if concept_id in concept_by_id
        ),
        default=1,
    )
    return (
        priority,
        candidate.scores.get("boosted_score", 0.0),
        _RETRIEVAL.get(candidate.retrieval_pass, 0),
        -candidate.note_id,
    )
