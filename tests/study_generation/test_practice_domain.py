from oms_hub.study_generation.domain import QuizImageRef
from oms_hub.study_generation.practice_domain import (
    DiagnosticSeverity,
    DraftDiagnostic,
    MatchingPromptDraft,
    MatchingQuestionDraft,
    QuestionDraft,
    QuestionSourceRef,
)


def test_question_draft_exposes_only_blocking_diagnostics() -> None:
    draft = QuestionDraft(
        question_id="q1",
        original_identifier="1",
        stem="Which structure is affected?",
        choices=("A", "B"),
        correct_index=None,
        rationale=None,
        image_ref=QuizImageRef("image-1", "Questions", "p1", "figure"),
        source_refs=(QuestionSourceRef("source-1", "segment-1", "p1"),),
        answer_provenance=None,
        extraction_confidence=0.7,
        diagnostics=(
            DraftDiagnostic("missing-answer", "Answer is missing", DiagnosticSeverity.BLOCKER),
            DraftDiagnostic("low-confidence", "Review wording", DiagnosticSeverity.WARNING),
        ),
        verification_required=True,
        verified_at=None,
    )

    assert draft.blocking_diagnostics == ("Answer is missing",)


def test_matching_question_draft_exposes_only_blocking_diagnostics() -> None:
    draft = MatchingQuestionDraft(
        question_id="matching-1",
        original_identifier="1",
        stem="Match each description.",
        prompts=(MatchingPromptDraft("p1", "A", "Description A", None),),
        choices=("Term one", "Term two"),
        rationale=None,
        image_ref=None,
        source_refs=(),
        answer_provenance=None,
        extraction_confidence=0.7,
        diagnostics=(
            DraftDiagnostic("missing-answer", "Answer is missing", DiagnosticSeverity.BLOCKER),
            DraftDiagnostic("low-confidence", "Review wording", DiagnosticSeverity.WARNING),
        ),
        verification_required=True,
        verified_at=None,
    )

    assert draft.blocking_diagnostics == ("Answer is missing",)
