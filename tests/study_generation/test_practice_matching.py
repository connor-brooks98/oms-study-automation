from oms_hub.study_generation.practice_contracts import (
    ExtractedAnswer,
    ExtractedQuestion,
    SegmentCitation,
)
from oms_hub.study_generation.practice_domain import AnswerProvenance, DiagnosticSeverity
from oms_hub.study_generation.practice_matching import pair_supplied_answers


def question(identifier: str | None, stem: str, *, confidence: float = 0.9) -> ExtractedQuestion:
    return ExtractedQuestion(
        original_identifier=identifier,
        stem=stem,
        choices=("A", "B"),
        supplied_correct_index=None,
        rationale=None,
        source_segments=(
            SegmentCitation(source_id="questions", segment_key=f"question-{stem}"),
        ),
        candidate_assets=(),
        confidence=confidence,
    )


def answer(identifier: str | None, index: int) -> ExtractedAnswer:
    return ExtractedAnswer(
        original_identifier=identifier,
        correct_index=index,
        rationale=None,
        source_segments=(
            SegmentCitation(source_id="answers", segment_key=f"answer-{identifier}"),
        ),
    )


def test_exact_question_numbers_pair_before_semantic_matching() -> None:
    drafts = pair_supplied_answers(
        questions=(question("1", "First?"), question("2", "Second?")),
        answers=(answer("2", 1), answer("1", 0)),
    )

    assert [draft.correct_index for draft in drafts] == [0, 1]
    assert all(draft.answer_provenance is AnswerProvenance.PROVIDED_BY_SOURCE for draft in drafts)


def test_conflicting_answer_entries_create_blocker() -> None:
    drafts = pair_supplied_answers(
        questions=(question("1", "First?"),),
        answers=(answer("1", 0), answer("1", 1)),
    )

    assert drafts[0].correct_index is None
    assert "conflicting supplied answers" in drafts[0].blocking_diagnostics


def test_duplicate_question_ids_and_unmatched_entries_remain_blocked() -> None:
    drafts = pair_supplied_answers(
        questions=(question("Q1", "One"), question("Question 1", "Another")),
        answers=(answer("1.", 0), answer("2", 1)),
    )

    assert all(draft.correct_index is None for draft in drafts)
    assert all(draft.verification_required for draft in drafts)
    messages = tuple(message for draft in drafts for message in draft.blocking_diagnostics)
    assert any("duplicate question identifier" in message for message in messages)
    assert any("unmatched supplied answer" in message for message in messages)


def test_aligned_order_only_pairs_complete_unique_identifier_sets() -> None:
    drafts = pair_supplied_answers(
        questions=(question(None, "First"), question(None, "Second")),
        answers=(answer(None, 1), answer(None, 0)),
    )

    assert [draft.correct_index for draft in drafts] == [1, 0]
    assert all(draft.answer_provenance is AnswerProvenance.PROVIDED_BY_SOURCE for draft in drafts)


def test_residual_source_order_pairs_after_exact_identifier_match() -> None:
    drafts = pair_supplied_answers(
        questions=(question("1", "First"), question("2", "Second")),
        answers=(answer("1", 1), answer(None, 0)),
    )

    assert [draft.correct_index for draft in drafts] == [1, 0]


def test_different_numbered_sets_do_not_fall_back_to_source_order() -> None:
    drafts = pair_supplied_answers(
        questions=(question("1", "First"), question("2", "Second")),
        answers=(answer("3", 1), answer("4", 0)),
    )

    assert all(draft.correct_index is None for draft in drafts)
    assert any(
        "unmatched supplied answer" in message
        for draft in drafts
        for message in draft.blocking_diagnostics
    )


def test_different_nonnumeric_identifiers_do_not_fall_back_to_source_order() -> None:
    drafts = pair_supplied_answers((question("A", "First"),), (answer("B", 1),))

    assert drafts[0].correct_index is None
    assert any("unmatched supplied answer" in item for item in drafts[0].blocking_diagnostics)


def test_duplicate_nonnumeric_identifiers_disable_source_order_pairing() -> None:
    drafts = pair_supplied_answers(
        (question("A", "First"), question("a.", "Second")),
        (answer(None, 0), answer(None, 1)),
    )

    assert all(draft.correct_index is None for draft in drafts)
    assert any("duplicate question identifier" in draft.blocking_diagnostics for draft in drafts)


def test_out_of_bounds_answer_is_a_blocker_instead_of_a_guessed_answer() -> None:
    draft = pair_supplied_answers((question("1", "First"),), (answer("1", 2),))[0]

    assert draft.correct_index is None
    assert draft.diagnostics[0].severity is DiagnosticSeverity.BLOCKER
    assert "outside the available choices" in draft.blocking_diagnostics[0]
