import pytest

from oms_hub.study_generation.practice_contracts import (
    ExtractedAnswer,
    ExtractedMatchingAnswer,
    ExtractedMatchingAnswerRow,
    ExtractedMatchingPrompt,
    ExtractedMatchingQuestion,
    ExtractedQuestion,
    SegmentCitation,
)
from oms_hub.study_generation.practice_domain import (
    AnswerProvenance,
    DiagnosticSeverity,
    MatchingQuestionDraft,
    QuestionSourceRef,
)
from oms_hub.study_generation.practice_matching import (
    PairingResult,
    pair_supplied_answers,
)


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


def _seven_by_seven_matching_fixture() -> tuple[
    ExtractedMatchingQuestion,
    tuple[ExtractedMatchingAnswer, ...],
    tuple[QuestionSourceRef, ...],
    tuple[tuple[QuestionSourceRef, ...], ...],
]:
    labels = tuple("ABCDEFG")
    mapping = (5, 4, 1, 0, 2, 6, 3)
    question = ExtractedMatchingQuestion(
        kind="matching",
        original_identifier="1",
        stem="Match each neutral description with its neutral term.",
        prompts=tuple(
            ExtractedMatchingPrompt(
                original_identifier=label,
                text=f"Description {label}",
                supplied_correct_index=None,
            )
            for label in labels
        ),
        choices=tuple(f"Term {number}" for number in range(1, 8)),
        rationale=None,
        source_segments=(
            SegmentCitation(source_id="questions", segment_key="question-1"),
        ),
        candidate_assets=(),
        confidence=0.99,
    )

    def answer_group(group_labels: tuple[str, ...]) -> ExtractedMatchingAnswer:
        rows = tuple(
            ExtractedMatchingAnswerRow(
                prompt_identifier=label,
                correct_index=mapping[labels.index(label)],
                rationale=None,
                source_segments=(
                    SegmentCitation(
                        source_id="answers",
                        segment_key=f"answer-{label.lower()}",
                    ),
                ),
            )
            for label in group_labels
        )
        return ExtractedMatchingAnswer(
            kind="matching", original_identifier="1", matches=rows
        )

    groups = (labels[:3], labels[3:])
    answers = tuple(answer_group(group) for group in groups)
    answer_refs = tuple(
        tuple(
            QuestionSourceRef(
                "answers", f"answer-{label.lower()}", f"page {page_number}"
            )
            for label in group
        )
        for page_number, group in enumerate(groups, start=4)
    )
    question_refs = (
        QuestionSourceRef("questions", "question-1", "page 1"),
    )
    return question, answers, question_refs, answer_refs


def _mutated_matching_pairing(mutation: str) -> PairingResult:
    question, answers, question_refs, answer_refs = _seven_by_seven_matching_fixture()
    questions = [question]
    question_ref_groups = [question_refs]
    answer_groups = list(answers)
    answer_ref_groups = list(answer_refs)

    if mutation == "missing":
        answer_groups[1] = answer_groups[1].model_copy(
            update={"matches": answer_groups[1].matches[:-1]}
        )
        answer_ref_groups[1] = answer_ref_groups[1][:-1]
    elif mutation == "duplicate_prompt":
        prompts = list(question.prompts)
        prompts[1] = prompts[1].model_copy(update={"original_identifier": "A"})
        questions[0] = question.model_copy(update={"prompts": tuple(prompts)})
    elif mutation == "conflict":
        prompts = list(question.prompts)
        prompts[0] = prompts[0].model_copy(update={"supplied_correct_index": 0})
        questions[0] = question.model_copy(update={"prompts": tuple(prompts)})
    elif mutation == "out_of_range":
        rows = list(answer_groups[0].matches)
        rows[0] = rows[0].model_copy(update={"correct_index": 7})
        answer_groups[0] = answer_groups[0].model_copy(update={"matches": tuple(rows)})
    elif mutation == "unknown_prompt":
        unknown = ExtractedMatchingAnswerRow(
            prompt_identifier="Z",
            correct_index=0,
            rationale=None,
            source_segments=(
                SegmentCitation(source_id="answers", segment_key="answer-z"),
            ),
        )
        answer_groups[1] = answer_groups[1].model_copy(
            update={"matches": (*answer_groups[1].matches, unknown)}
        )
        answer_ref_groups[1] = (
            *answer_ref_groups[1],
            QuestionSourceRef("answers", "answer-z", "page 5"),
        )
    elif mutation == "unmatched_group":
        unmatched = ExtractedMatchingAnswerRow(
            prompt_identifier="A",
            correct_index=0,
            rationale=None,
            source_segments=(
                SegmentCitation(source_id="answers", segment_key="answer-group-2"),
            ),
        )
        answer_groups.append(
            ExtractedMatchingAnswer(
                kind="matching", original_identifier="2", matches=(unmatched,)
            )
        )
        answer_ref_groups.append(
            (QuestionSourceRef("answers", "answer-group-2", "page 6"),)
        )
    elif mutation == "duplicate_group":
        questions.append(question.model_copy(update={"stem": "Second matching group"}))
        question_ref_groups.append(question_refs)
    else:
        raise AssertionError(f"unknown matching mutation: {mutation}")

    return pair_supplied_answers(
        tuple(questions),
        tuple(answer_groups),
        question_source_refs=tuple(question_ref_groups),
        answer_source_refs=tuple(answer_ref_groups),
    )


def test_matching_pairing_merges_later_key_rows_into_one_complete_group() -> None:
    question, answers, question_refs, answer_refs = _seven_by_seven_matching_fixture()

    result = pair_supplied_answers(
        (question,),
        answers,
        question_source_refs=(question_refs,),
        answer_source_refs=answer_refs,
    )

    assert result.diagnostics == ()
    assert len(result.drafts) == 1
    draft = result.drafts[0]
    assert isinstance(draft, MatchingQuestionDraft)
    assert tuple(prompt.id for prompt in draft.prompts) == tuple(
        f"p{i}" for i in range(1, 8)
    )
    assert tuple(prompt.correct_index for prompt in draft.prompts) == (5, 4, 1, 0, 2, 6, 3)
    assert draft.rationale == (
        "Source-marked matches: A -> Term 6; B -> Term 5; C -> Term 2; "
        "D -> Term 1; E -> Term 3; F -> Term 7; G -> Term 4."
    )
    assert draft.source_refs == tuple(
        dict.fromkeys((question_refs[0], *answer_refs[0], *answer_refs[1]))
    )
    assert draft.answer_provenance is AnswerProvenance.PROVIDED_BY_SOURCE


@pytest.mark.parametrize(
    ("mutation", "code", "run_level"),
    [
        ("missing", "missing-supplied-matching-answer", False),
        ("duplicate_prompt", "duplicate-matching-prompt-identifier", False),
        ("conflict", "conflicting-supplied-matching-answer", False),
        ("out_of_range", "supplied-matching-answer-out-of-bounds", False),
        ("unknown_prompt", "unknown-matching-prompt-answer", True),
        ("unmatched_group", "unmatched-matching-answer-group", True),
        ("duplicate_group", "duplicate-matching-question-identifier", True),
    ],
)
def test_matching_pairing_fails_closed_with_stable_diagnostic_codes(
    mutation: str, code: str, run_level: bool
) -> None:
    result = _mutated_matching_pairing(mutation)
    owned_codes = {
        diagnostic.code for draft in result.drafts for diagnostic in draft.diagnostics
    }
    run_codes = {diagnostic.code for diagnostic in result.diagnostics}
    assert code in (run_codes if run_level else owned_codes)
    assert code not in (owned_codes if run_level else run_codes)


def test_matching_pairing_excludes_invalid_rows_from_provenance() -> None:
    question, answers, question_refs, answer_refs = _seven_by_seven_matching_fixture()
    unknown = ExtractedMatchingAnswerRow(
        prompt_identifier="Z",
        correct_index=0,
        rationale=None,
        source_segments=(SegmentCitation(source_id="answers", segment_key="answer-z"),),
    )
    answers = (
        answers[0],
        answers[1].model_copy(update={"matches": (*answers[1].matches, unknown)}),
    )
    answer_refs = (
        answer_refs[0],
        (*answer_refs[1], QuestionSourceRef("answers", "answer-z", "page 5")),
    )

    draft = pair_supplied_answers(
        (question,),
        answers,
        question_source_refs=(question_refs,),
        answer_source_refs=answer_refs,
    ).drafts[0]

    assert "answer-z" not in {ref.segment_key for ref in draft.source_refs}


def test_exact_question_numbers_pair_before_semantic_matching() -> None:
    result = pair_supplied_answers(
        questions=(question("1", "First?"), question("2", "Second?")),
        answers=(answer("2", 1), answer("1", 0)),
    )
    drafts = result.drafts

    assert [draft.correct_index for draft in drafts] == [0, 1]
    assert [draft.rationale for draft in drafts] == [
        "Source-marked correct answer: A",
        "Source-marked correct answer: B",
    ]
    assert [tuple(item.code for item in draft.diagnostics) for draft in drafts] == [(), ()]
    assert all(draft.answer_provenance is AnswerProvenance.PROVIDED_BY_SOURCE for draft in drafts)


def test_conflicting_answer_entries_create_blocker() -> None:
    drafts = pair_supplied_answers(
        questions=(question("1", "First?"),),
        answers=(answer("1", 0), answer("1", 1)),
    ).drafts

    assert drafts[0].correct_index is None
    assert {item.code for item in drafts[0].diagnostics} == {
        "duplicate-supplied-answer",
        "unmatched-supplied-answer",
    }
    assert "conflicting supplied answers" in drafts[0].blocking_diagnostics


def test_duplicate_question_ids_and_unmatched_entries_remain_blocked() -> None:
    drafts = pair_supplied_answers(
        questions=(question("Q1", "One"), question("Question 1", "Another")),
        answers=(answer("1.", 0), answer("2", 1)),
    ).drafts

    assert all(draft.correct_index is None for draft in drafts)
    assert all(draft.verification_required for draft in drafts)
    messages = tuple(message for draft in drafts for message in draft.blocking_diagnostics)
    assert any("duplicate question identifier" in message for message in messages)
    assert any("unmatched supplied answer" in message for message in messages)


def test_aligned_order_only_pairs_complete_unique_identifier_sets() -> None:
    drafts = pair_supplied_answers(
        questions=(question(None, "First"), question(None, "Second")),
        answers=(answer(None, 1), answer(None, 0)),
    ).drafts

    assert [draft.correct_index for draft in drafts] == [1, 0]
    assert all(draft.answer_provenance is AnswerProvenance.PROVIDED_BY_SOURCE for draft in drafts)


def test_residual_source_order_pairs_after_exact_identifier_match() -> None:
    drafts = pair_supplied_answers(
        questions=(question("1", "First"), question("2", "Second")),
        answers=(answer("1", 1), answer(None, 0)),
    ).drafts

    assert [draft.correct_index for draft in drafts] == [1, 0]


def test_conflicting_staggered_residual_labels_disable_all_source_order_pairing() -> None:
    drafts = pair_supplied_answers(
        (question(None, "First"), question("B", "Second")),
        (answer("C", 1), answer(None, 0)),
    ).drafts

    assert all(draft.correct_index is None for draft in drafts)
    assert all(draft.verification_required for draft in drafts)


def test_different_numbered_sets_do_not_fall_back_to_source_order() -> None:
    drafts = pair_supplied_answers(
        questions=(question("1", "First"), question("2", "Second")),
        answers=(answer("3", 1), answer("4", 0)),
    ).drafts

    assert all(draft.correct_index is None for draft in drafts)
    assert any(
        "unmatched supplied answer" in message
        for draft in drafts
        for message in draft.blocking_diagnostics
    )


def test_different_nonnumeric_identifiers_do_not_fall_back_to_source_order() -> None:
    drafts = pair_supplied_answers((question("A", "First"),), (answer("B", 1),)).drafts

    assert drafts[0].correct_index is None
    assert any("unmatched supplied answer" in item for item in drafts[0].blocking_diagnostics)


def test_duplicate_nonnumeric_identifiers_disable_source_order_pairing() -> None:
    drafts = pair_supplied_answers(
        (question("A", "First"), question("a.", "Second")),
        (answer(None, 0), answer(None, 1)),
    ).drafts

    assert all(draft.correct_index is None for draft in drafts)
    assert any("duplicate question identifier" in draft.blocking_diagnostics for draft in drafts)


def test_out_of_bounds_answer_is_a_blocker_instead_of_a_guessed_answer() -> None:
    draft = pair_supplied_answers((question("1", "First"),), (answer("1", 2),)).drafts[0]

    assert draft.correct_index is None
    assert draft.diagnostics[0].severity is DiagnosticSeverity.BLOCKER
    assert "outside the available choices" in draft.blocking_diagnostics[0]
