from dataclasses import replace

from oms_hub.anki.card_budget import (
    apply_existing_card_target,
    prioritize_gap_facts,
)
from oms_hub.anki.domain import Candidate
from oms_hub.anki.lcl import LectureConcept, LedgerSourceRef
from oms_hub.anki.v2_contracts import MissingFactV2


def _concept(
    concept_id: str,
    *,
    importance: str = "medium",
) -> LectureConcept:
    return LectureConcept(
        concept_id=concept_id,
        source_refs=(LedgerSourceRef(passage_id="a" * 64),),
        statement=f"Statement {concept_id}",
        hypothetical_card=f"Card {concept_id}",
        paraphrases=(f"{concept_id} one", f"{concept_id} two"),
        importance=importance,
        primary_entity=concept_id,
        depth="deep" if importance == "high" else "medium",
        emphasis_flag=importance == "high",
        source_passage_ids=("a" * 64,),
    )


def _candidate(note_id: int, concept_id: str, score: float) -> Candidate:
    return Candidate(
        note_id=note_id,
        content_hash=f"{note_id:064x}",
        best_concept_id=concept_id,
        provenance={
            "concept_matches": [
                {
                    "concept_id": concept_id,
                    "selected": True,
                    "score": score,
                }
            ]
        },
        scores={"boosted_score": score},
        predicted_band="covered",
        verdict="include",
        confidence=1.0,
        reason="Audited keep",
        context_trap=False,
        recall_direction="forward",
        mnemonic_classification="none",
        dedupe_disposition="unique",
        selected=True,
    )


def _fact(concept_id: str, number: int) -> MissingFactV2:
    return MissingFactV2(
        fact_id=f"{concept_id}-M{number}",
        statement=f"Missing fact {concept_id}-{number}",
        passage_ids=(f"SLD:{concept_id}:01",),
    )


def test_existing_target_preserves_one_card_per_concept_before_filling() -> None:
    concepts = (_concept("C01", importance="high"), _concept("C02"))
    candidates = (
        _candidate(1, "C01", 0.9),
        _candidate(2, "C01", 0.8),
        _candidate(3, "C02", 0.1),
    )

    result = apply_existing_card_target(candidates, concepts, target=2)

    selected = {item.note_id for item in result.candidates if item.selected}
    assert selected == {1, 3}
    assert result.coverage_floor == 2
    assert result.overflow_count == 0


def test_existing_target_allows_overflow_for_concept_coverage() -> None:
    concepts = tuple(_concept(f"C{number:02}") for number in range(1, 4))
    candidates = tuple(
        _candidate(number, concept.concept_id, 1 / number)
        for number, concept in enumerate(concepts, start=1)
    )

    result = apply_existing_card_target(candidates, concepts, target=2)

    assert sum(item.selected for item in result.candidates) == 3
    assert result.overflow_count == 1


def test_custom_target_prioritizes_breadth_and_defers_lower_value_facts() -> None:
    concepts = (
        _concept("C01", importance="high"),
        _concept("C02"),
    )
    missing = {
        "C01": (_fact("C01", 1), _fact("C01", 2)),
        "C02": (_fact("C02", 1), _fact("C02", 2)),
    }

    result = prioritize_gap_facts(
        concepts,
        missing,
        {"C01", "C02"},
        target=2,
    )

    assert [fact.fact_id for fact in result.selected_by_concept["C01"]] == [
        "C01-M1"
    ]
    assert [fact.fact_id for fact in result.selected_by_concept["C02"]] == [
        "C02-M1"
    ]
    assert result.selected_count == 2


def test_custom_target_allows_key_concept_overflow_when_no_existing_card() -> None:
    concepts = (
        _concept("C01", importance="high"),
        _concept("C02", importance="high"),
    )
    missing = {
        "C01": (_fact("C01", 1),),
        "C02": (_fact("C02", 1),),
    }

    result = prioritize_gap_facts(concepts, missing, set(), target=1)

    assert result.selected_count == 2
    assert result.overflow_count == 1


def test_non_audit_keep_remains_unselected() -> None:
    concept = _concept("C01")
    dropped = replace(
        _candidate(1, "C01", 1.0),
        verdict="drop",
        selected=False,
    )

    result = apply_existing_card_target((dropped,), (concept,))

    assert result.candidates == (dropped,)
