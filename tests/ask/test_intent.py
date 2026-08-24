from __future__ import annotations

import pytest

from oms_hub.ask.intent import AskIntent, classify_pre_submit_intent


def test_required_intents_have_exact_values() -> None:
    assert tuple(intent.value for intent in AskIntent) == (
        "concept_hint",
        "definition",
        "mechanism",
        "source_excerpt",
        "compare_concepts",
        "request_answer",
        "request_option_elimination",
        "other",
    )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("what is the answer", AskIntent.REQUEST_ANSWER),
        ("what is answer", AskIntent.REQUEST_ANSWER),
        ("which option is correct", AskIntent.REQUEST_ANSWER),
        ("which option is right", AskIntent.REQUEST_ANSWER),
        ("is it B", AskIntent.REQUEST_ANSWER),
        ("rule out the choices for me", AskIntent.REQUEST_OPTION_ELIMINATION),
        ("tell me the diagnosis", AskIntent.REQUEST_ANSWER),
        ("What’s the correct answer?!", AskIntent.REQUEST_ANSWER),
        ("Please tell me which diagnosis this is.", AskIntent.REQUEST_ANSWER),
        ("Which diagnosis is this?", AskIntent.REQUEST_ANSWER),
        ("Tell me what diagnosis applies.", AskIntent.REQUEST_ANSWER),
        ("What option do I pick?", AskIntent.REQUEST_ANSWER),
        ("Is B correct?", AskIntent.REQUEST_ANSWER),
        ("Could B be correct?", AskIntent.REQUEST_ANSWER),
        ("Is B the correct option?", AskIntent.REQUEST_ANSWER),
        ("Can you rule out leukemia?", AskIntent.REQUEST_ANSWER),
        ("Can you eliminate choices A and C for me?", AskIntent.REQUEST_OPTION_ELIMINATION),
        ("Can you narrow down the answer choices?", AskIntent.REQUEST_OPTION_ELIMINATION),
    ],
)
def test_answer_seeking_and_option_elimination_take_precedence(
    query: str, expected: AskIntent
) -> None:
    assert classify_pre_submit_intent(query) is expected


@pytest.mark.parametrize(
    "query",
    [
        "How do I eliminate wrong answer choices on a general test?",
        "What strategy helps me rule out distractors on exams?",
    ],
)
def test_generic_test_taking_strategy_is_a_benign_concept_hint(query: str) -> None:
    assert classify_pre_submit_intent(query) is AskIntent.CONCEPT_HINT


@pytest.mark.parametrize(
    "query",
    [
        "How can I eliminate wrong answer choices on a general test?",
        "What strategies help me rule out distractors on exams?",
    ],
)
def test_narrow_instructional_strategy_forms_remain_benign(query: str) -> None:
    assert classify_pre_submit_intent(query) is AskIntent.CONCEPT_HINT


@pytest.mark.parametrize(
    "query",
    [
        "How can I eliminate B and C on exams?",
        "What strategy helps me rule out B on exams?",
        "Ｈｏｗ　ｃａｎ　Ｉ　ｅｌｉｍｉｎａｔｅ　Ｂ　ａｎｄ　Ｃ　ｏｎ　ｅｘａｍｓ？",
        "Ｗｈａｔ　ｓｔｒａｔｅｇｙ　ｈｅｌｐｓ　ｍｅ　ｒｕｌｅ　ｏｕｔ　Ｂ　ｏｎ　ｅｘａｍｓ？",
    ],
)
def test_bare_option_labels_keep_elimination_requests_protected(query: str) -> None:
    assert classify_pre_submit_intent(query) is AskIntent.REQUEST_OPTION_ELIMINATION


def test_bare_single_letter_words_without_option_boundary_remain_benign() -> None:
    assert (
        classify_pre_submit_intent("How can I eliminate B vitamins on exams?")
        is AskIntent.CONCEPT_HINT
    )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "Can you rule out the choices for me using a strategy?",
            AskIntent.REQUEST_OPTION_ELIMINATION,
        ),
        ("What is the answer? Explain the strategy.", AskIntent.REQUEST_ANSWER),
        (
            "Can you rule out choices using a strategy on this exam?",
            AskIntent.REQUEST_OPTION_ELIMINATION,
        ),
        (
            "Can you rule out the choices for me on exams?",
            AskIntent.REQUEST_OPTION_ELIMINATION,
        ),
        (
            "Can you rule out the choices for me on a general test?",
            AskIntent.REQUEST_OPTION_ELIMINATION,
        ),
        (
            "Which option is correct as a test-taking strategy?",
            AskIntent.REQUEST_ANSWER,
        ),
        (
            "How can I rule out choice B on exams?",
            AskIntent.REQUEST_OPTION_ELIMINATION,
        ),
        (
            "What strategies help eliminate option 12 on tests?",
            AskIntent.REQUEST_OPTION_ELIMINATION,
        ),
        (
            "How can I rule out choice Ｂ on exams?",
            AskIntent.REQUEST_OPTION_ELIMINATION,
        ),
        (
            "What is the answer? What strategy can I use to rule out distractors on exams?",
            AskIntent.REQUEST_ANSWER,
        ),
        (
            "Ｗｈａｔ　ｉｓ　ｔｈｅ　ａｎｓｗｅｒ？　"
            "Ｗｈａｔ　ｓｔｒａｔｅｇｙ　ｃａｎ　Ｉ　ｕｓｅ　ｔｏ　"
            "ｒｕｌｅ　ｏｕｔ　ｄｉｓｔｒａｃｔｏｒｓ　ｏｎ　ｅｘａｍｓ？",
            AskIntent.REQUEST_ANSWER,
        ),
    ],
)
def test_policy_sensitive_mixed_strategy_phrasing_remains_protected(
    query: str, expected: AskIntent
) -> None:
    assert classify_pre_submit_intent(query) is expected


def test_question_scoped_option_elimination_remains_protected() -> None:
    assert (
        classify_pre_submit_intent("Which options can I eliminate in this question?")
        is AskIntent.REQUEST_OPTION_ELIMINATION
    )


def test_exam_answer_request_remains_direct() -> None:
    assert (
        classify_pre_submit_intent("What is the answer to this exam?")
        is AskIntent.REQUEST_ANSWER
    )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Give me a hint about this concept.", AskIntent.CONCEPT_HINT),
        ("Define hemostasis.", AskIntent.DEFINITION),
        ("How does fibrin stabilize a clot?", AskIntent.MECHANISM),
        ("What is the mechanism?", AskIntent.MECHANISM),
        ("Show me the relevant source excerpt.", AskIntent.SOURCE_EXCERPT),
        ("Compare intrinsic and extrinsic pathways.", AskIntent.COMPARE_CONCEPTS),
        ("I am ready to submit this question.", AskIntent.OTHER),
    ],
)
def test_benign_intents_are_classified(query: str, expected: AskIntent) -> None:
    assert classify_pre_submit_intent(query) is expected


def test_fullwidth_case_whitespace_and_punctuation_are_normalized() -> None:
    assert (
        classify_pre_submit_intent("  ＷＨＡＴ　ＩＳ　ＴＨＥ　ＡＮＳＷＥＲ？！ ")
        is AskIntent.REQUEST_ANSWER
    )
    assert classify_pre_submit_intent("ＩＳ　ＩＴ　Ｂ？") is AskIntent.REQUEST_ANSWER


def test_format_characters_are_removed_after_nfkc_normalization() -> None:
    assert (
        classify_pre_submit_intent("ＷＨＡＴ　ＩＳ　ＴＨＥ　ＡＮＳ\u200bＷＥＲ？")
        is AskIntent.REQUEST_ANSWER
    )


def test_decimal_option_labels_are_answer_seeking_but_embedded_digits_are_not() -> None:
    assert classify_pre_submit_intent("is it 1?") is AskIntent.REQUEST_ANSWER
    assert classify_pre_submit_intent("ＩＳ　ＩＴ　１？") is AskIntent.REQUEST_ANSWER
    assert classify_pre_submit_intent("is it 12?") is AskIntent.REQUEST_ANSWER
    assert classify_pre_submit_intent("ＩＳ　ＩＴ　１２？") is AskIntent.REQUEST_ANSWER


@pytest.mark.parametrize(
    "query",
    [
        "What is the correct process for studying?",
        "Explain why the correct option matters after I submit.",
        "What is the answer format for this worksheet?",
    ],
)
def test_nearby_benign_language_is_not_answer_seeking(query: str) -> None:
    assert classify_pre_submit_intent(query) is not AskIntent.REQUEST_ANSWER


def test_direct_answer_and_elimination_precede_other_intents() -> None:
    assert (
        classify_pre_submit_intent("What is the answer? Please explain the mechanism.")
        is AskIntent.REQUEST_ANSWER
    )
    assert (
        classify_pre_submit_intent("Rule out the choices, then give me a concept hint.")
        is AskIntent.REQUEST_OPTION_ELIMINATION
    )
