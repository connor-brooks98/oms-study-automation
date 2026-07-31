import pytest
from pydantic import ValidationError

from oms_hub.anki.v2_contracts import (
    AuditVerdictV2,
    ConvergenceConceptV2,
    CoverageJudgmentV2,
    GeneratedGapCardV2,
    IntentionallyUncitedV2,
    LectureConceptLedgerV2,
    LectureConceptV2,
    MissingFactV2,
    ParaphraseExpansionV2,
    UnresolvedGapV2,
)


def _concept() -> LectureConceptV2:
    return LectureConceptV2(
        concept_id="C01",
        canonical_statement="Hereditary spherocytosis increases MCHC.",
        hypothetical_card="HS shows {{c1::increased MCHC}}.",
        primary_entity="hereditary spherocytosis",
        aliases=("HS", "spherocytes", "EMA binding"),
        paraphrases=(
            "hereditary spherocytosis MCHC",
            "hereditary spherocytosis CBC finding",
            "hereditary spherocytosis increased MCHC",
        ),
        depth="deep",
        emphasis_flag=True,
        importance="high",
        passage_ids=("SLD:07:0031", "TRX:07:0198"),
    )


def test_lcl_v2_requires_unique_concepts_and_passage_disposition() -> None:
    ledger = LectureConceptLedgerV2(
        lecture_entity_count=6,
        concepts=(_concept(),),
        intentionally_uncited=(
            IntentionallyUncitedV2(
                passage_id="SLD:07:0001",
                reason="title_slide",
            ),
        ),
    )

    assert ledger.concepts[0].importance == "high"
    assert ledger.intentionally_uncited[0].reason == "title_slide"

    with pytest.raises(ValidationError, match="concept IDs"):
        LectureConceptLedgerV2(
            lecture_entity_count=6,
            concepts=(_concept(), _concept()),
            intentionally_uncited=(),
        )


def test_lcl_v2_rejects_paraphrase_that_drops_primary_entity() -> None:
    with pytest.raises(ValidationError, match="primary entity"):
        _concept().model_copy(
            update={
                "paraphrases": (
                    "increased MCHC",
                    "hereditary spherocytosis CBC finding",
                    "hereditary spherocytosis diagnostic test",
                )
            }
        ).model_validate(
            _concept().model_copy(
                update={
                    "paraphrases": (
                        "increased MCHC",
                        "hereditary spherocytosis CBC finding",
                        "hereditary spherocytosis diagnostic test",
                    )
                }
            ).model_dump()
        )


def test_coverage_v2_missing_facts_are_atomic_and_unique() -> None:
    fact = MissingFactV2(
        fact_id="C01-M1",
        statement="Hereditary spherocytosis increases MCHC.",
        passage_ids=("SLD:07:0031",),
    )
    judgment = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(1001, 1002),
        missing_facts=(fact,),
        rationale="The existing cards omit the CBC finding.",
    )

    assert judgment.missing_facts == (fact,)

    with pytest.raises(ValidationError, match="missing fact IDs"):
        CoverageJudgmentV2(
            concept_id="C01",
            supporting_note_ids=(1001,),
            missing_facts=(fact, fact),
            rationale="Duplicate output.",
        )


def test_audit_v2_enforces_summary_only_cannot_be_kept() -> None:
    with pytest.raises(ValidationError, match="summary-only"):
        AuditVerdictV2(
            nid=1001,
            verdict="keep",
            primary_subject="hereditary spherocytosis",
            support="summary_only",
            reason="Only the generated summary mentions it",
            structure_issue=(),
        )


def test_gap_v2_generated_and_unresolved_share_fact_identity() -> None:
    generated = GeneratedGapCardV2(
        fact_id="C01-M1",
        status="generated",
        text="<b>HS</b> shows {{c1::<b>increased</b>}} MCHC.",
        extra="Membrane loss reduces the surface-area-to-volume ratio.",
        note_type="AnKingOverhaul (AnKing Step Deck / AnKingMed)",
        source_passage_ids=("SLD:07:0031", "TRX:07:0198"),
        split=False,
        image_needed=None,
    )
    unresolved = UnresolvedGapV2(
        fact_id="C01-M2",
        status="unresolved",
        reason="source evidence is insufficient",
        duplicate_of_note_id=None,
    )

    assert generated.fact_id == "C01-M1"
    assert unresolved.fact_id == "C01-M2"


def test_paraphrase_expansion_requires_exactly_three_unique_queries() -> None:
    expansion = ParaphraseExpansionV2(
        concept_id="C01",
        paraphrases=(
            "hereditary spherocytosis splenic sequestration",
            "hereditary spherocytosis osmotic fragility",
            "hereditary spherocytosis splenectomy indication",
        ),
        targeting="Residual diagnostic and treatment facts.",
    )

    assert len(expansion.paraphrases) == 3

    with pytest.raises(ValidationError, match="at least 3 items"):
        ParaphraseExpansionV2(
            concept_id="C01",
            paraphrases=(
                "hereditary spherocytosis osmotic fragility",
                "hereditary spherocytosis splenectomy indication",
            ),
            targeting="Too few queries.",
        )

    with pytest.raises(ValidationError, match="unique"):
        ParaphraseExpansionV2(
            concept_id="C01",
            paraphrases=(
                "hereditary spherocytosis osmotic fragility",
                "hereditary spherocytosis osmotic fragility",
                "hereditary spherocytosis splenectomy indication",
            ),
            targeting="Duplicate query.",
        )


def test_convergence_contract_reconciles_pass_count_growth_and_seen_notes() -> None:
    convergence = ConvergenceConceptV2(
        concept_id="C01",
        passes_run=2,
        seen_note_ids=(1001, 1002, 1003),
        growth=(1.0, 1 / 3),
        converged=False,
    )

    assert convergence.growth == (1.0, 1 / 3)

    with pytest.raises(ValidationError, match="pass count"):
        convergence.model_copy(
            update={"passes_run": 3},
        ).model_validate(
            convergence.model_copy(update={"passes_run": 3}).model_dump()
        )

    with pytest.raises(ValidationError, match="unique"):
        convergence.model_copy(
            update={"seen_note_ids": (1001, 1001)},
        ).model_validate(
            convergence.model_copy(
                update={"seen_note_ids": (1001, 1001)}
            ).model_dump()
        )
