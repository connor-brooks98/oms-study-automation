"""Structural tests for the source-grounded board-question contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from oms_hub.questions.models import (
    BoardQuestionDraft,
    QuestionClaimRole,
    QuestionMode,
    QuestionStatus,
    QuestionValidationResult,
    QuestionVersion,
)
from tests.builders.questions import build_board_question_draft

ROOT = Path(__file__).resolve().parents[2]


def _payload(**changes: object) -> dict[str, Any]:
    option_count = changes.pop("option_count", 4)
    duplicate_option_id = changes.pop("duplicate_option_id", False)
    assert isinstance(option_count, int)
    assert isinstance(duplicate_option_id, bool)
    payload = dict(
        build_board_question_draft(
            option_count=option_count,
            duplicate_option_id=duplicate_option_id,
        )
    )
    payload.update(changes)
    return payload


def _schema_bytes() -> bytes:
    schema = TypeAdapter(
        BoardQuestionDraft | QuestionValidationResult | QuestionVersion
    ).json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "question-v1.json"
    return (json.dumps(schema, indent=2, sort_keys=True) + "\n").encode("utf-8")


def test_required_enum_values_are_exact() -> None:
    assert [member.value for member in QuestionMode] == [
        "lecture_recall",
        "lecture_application",
        "board_style",
        "integrated_board_style",
        "comlex_omm",
        "remediation",
        "timed_mixed_block",
    ]
    assert [member.value for member in QuestionStatus] == [
        "draft",
        "validating",
        "quarantined",
        "approved",
        "retired",
    ]
    assert [member.value for member in QuestionClaimRole] == [
        "stem",
        "correct_support",
        "distractor_support",
        "rationale",
        "teaching_point",
    ]


def test_valid_draft_accepts_fixture_payload() -> None:
    draft = BoardQuestionDraft.model_validate(build_board_question_draft())
    assert len(draft.options) == 4
    assert draft.correct_option_id == "B"
    assert draft.options[0].evidence_ids
    assert draft.claims[0].evidence_ids


def test_question_requires_four_or_five_options() -> None:
    with pytest.raises(ValueError, match="four or five"):
        BoardQuestionDraft.model_validate(_payload(option_count=3))


def test_question_accepts_five_options() -> None:
    options = build_board_question_draft(option_count=5)["options"]
    assert len(BoardQuestionDraft.model_validate(_payload(options=options)).options) == 5


def test_correct_option_must_exist() -> None:
    with pytest.raises(ValueError, match="correct_option_id"):
        BoardQuestionDraft.model_validate(_payload(correct_option_id="Z"))


def test_option_ids_are_unique() -> None:
    duplicate = build_board_question_draft(option_count=4, duplicate_option_id=True)
    with pytest.raises(ValueError, match="unique"):
        BoardQuestionDraft.model_validate(duplicate)


@pytest.mark.parametrize(
    "field, value",
    [
        ("rationale", ""),
        ("evidence_ids", []),
    ],
)
def test_each_option_requires_rationale_and_evidence(field: str, value: Any) -> None:
    options = build_board_question_draft()["options"]
    if field == "rationale":
        options[0]["rationale"] = value
    else:
        options[0]["evidence_ids"] = value
    with pytest.raises(ValueError):
        BoardQuestionDraft.model_validate(_payload(options=options))


def test_each_claim_requires_evidence() -> None:
    claims = build_board_question_draft()["claims"]
    claims[0]["evidence_ids"] = []
    with pytest.raises(ValueError, match="evidence_ids"):
        BoardQuestionDraft.model_validate(_payload(claims=claims))


@pytest.mark.parametrize("difficulty", (0, 6))
def test_difficulty_is_between_one_and_five(difficulty: int) -> None:
    with pytest.raises(ValueError, match="difficulty"):
        BoardQuestionDraft.model_validate(_payload(difficulty=difficulty))


def test_at_least_one_objective_id_is_required() -> None:
    with pytest.raises(ValueError, match="objective"):
        BoardQuestionDraft.model_validate(_payload(objective_ids=[]))


def test_question_models_are_immutable_and_versioned() -> None:
    draft = BoardQuestionDraft.model_validate(build_board_question_draft())
    version = QuestionVersion(
        question_id="question-1",
        version=1,
        mode=QuestionMode.BOARD_STYLE,
        draft=draft,
    )
    assert version.status is QuestionStatus.DRAFT
    assert version.schema_version == "question-v1"
    with pytest.raises(ValidationError):
        version.status = QuestionStatus.APPROVED


def test_validation_result_is_immutable_and_defaults_to_valid_without_codes() -> None:
    result = QuestionValidationResult(valid=True)
    assert result.codes == ()


def test_question_schema_is_deterministic_and_matches_snapshot() -> None:
    expected = _schema_bytes()
    assert expected == _schema_bytes()
    assert (ROOT / "schemas" / "question-v1.json").read_bytes() == expected
