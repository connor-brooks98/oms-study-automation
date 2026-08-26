from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from oms_hub.ask.leakage import LeakResult, detect_answer_leak, safe_pre_submit_refusal


def test_leak_detector_catches_answer_with_punctuation_change() -> None:
    result = detect_answer_leak(
        "The diagnosis is heparin induced thrombocytopenia.",
        ["Heparin-induced thrombocytopenia"],
    )
    assert result.leaked


def test_leak_detector_normalizes_unicode_case_whitespace_and_hyphens() -> None:
    result = detect_answer_leak(
        "Ｔｈｅ\nＤＩＡＧＮＯＳＩＳ　ｉｓ　ｈｅｐａｒｉｎ–induced\tthrombocytopenia.",
        ["Heparin-induced thrombocytopenia"],
    )
    assert result.leaked


def test_leak_detector_removes_unicode_format_characters() -> None:
    result = detect_answer_leak(
        "Ｔｈｅ\u200b　ｄｉａｇｎｏｓｉｓ　ｉｓ　ｈｅｐａｒｉ\u2060ｎ–induced thrombocytopenia.",
        ["Heparin-induced thrombocytopenia"],
    )
    assert result.leaked


def test_option_label_formatting_is_normalized() -> None:
    assert detect_answer_leak("The answer is (Ｂ).", ["b"]).leaked
    assert detect_answer_leak("Choice: B is correct.", ["Option B"]).leaked


def test_decimal_option_label_formatting_is_normalized() -> None:
    assert detect_answer_leak("The answer is 1.", ["Option 1"]).leaked
    assert detect_answer_leak("The answer is 12.", ["Option 12"]).leaked
    assert detect_answer_leak("Ｔｈｅ　ａｎｓｗｅｒ　ｉｓ　１．", ["Ｏｐｔｉｏｎ　１"]).leaked
    assert detect_answer_leak("Ｔｈｅ　ａｎｓｗｅｒ　ｉｓ　１２．", ["Ｏｐｔｉｏｎ　１２"]).leaked
    assert not detect_answer_leak("The answer is 12.", ["Option 1"]).leaked


def test_string_protected_answers_are_one_value_not_character_values() -> None:
    assert detect_answer_leak("The diagnosis is heparin.", "heparin").leaked


@pytest.mark.parametrize("protected_answers", [None, 42, object()])
def test_malformed_outer_protected_answers_are_safe(protected_answers: object) -> None:
    assert not detect_answer_leak("The answer is 12.", protected_answers).leaked  # type: ignore[arg-type]


def test_non_string_values_inside_a_sequence_are_ignored() -> None:
    assert not detect_answer_leak("The answer is 12.", [None, 12]).leaked  # type: ignore[list-item]


def test_dotted_abbreviation_is_matched_when_variant_is_supplied() -> None:
    assert detect_answer_leak("The findings support H.I.T.", ["HIT"]).leaked


def test_short_common_answer_requires_token_boundary() -> None:
    result = detect_answer_leak("The patient should be kept warm.", ["War"])
    assert not result.leaked
    assert not detect_answer_leak("The patient described w a r injuries.", ["War"]).leaked
    assert detect_answer_leak("The patient described a war injury.", ["War"]).leaked


def test_long_answer_does_not_match_as_a_substring() -> None:
    assert not detect_answer_leak("The patient has heparinase activity.", ["heparin"]).leaked


def test_empty_or_blank_inputs_are_safe() -> None:
    assert not detect_answer_leak("", ["answer"]).leaked
    assert not detect_answer_leak("   \n\t", ["answer"]).leaked
    assert not detect_answer_leak("answer", ["", "  "]).leaked
    assert not detect_answer_leak("", [""]).leaked


def test_leak_result_is_immutable_and_non_sensitive() -> None:
    result = detect_answer_leak("The answer is heparin.", ["heparin"])
    assert isinstance(result, LeakResult)
    assert "heparin" not in repr(result).casefold()
    try:
        result.leaked = False  # type: ignore[misc]
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError("LeakResult must be immutable")


def test_safe_refusal_has_exact_text_and_no_provenance() -> None:
    answer = safe_pre_submit_refusal()
    assert (
        answer.answer_markdown
        == (
            "Submit the question first. I can still explain the underlying concept or point you "
            "to the relevant source."
        )
    )
    assert answer.claims == ()
    assert answer.citations == ()
    assert answer.insufficient_evidence is False
    assert answer.provider_request_id is None
    assert answer.retrieval_run_id is None
    assert answer.safe_response_reason == "pre_submit_answer_protection"
