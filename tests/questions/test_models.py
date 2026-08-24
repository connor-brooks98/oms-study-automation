"""Structural tests for the source-grounded board-question contracts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
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
    schema = _schema_payload()
    return (json.dumps(schema, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _schema_payload() -> dict[str, Any]:
    schema = TypeAdapter(
        BoardQuestionDraft | QuestionValidationResult | QuestionVersion
    ).json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "question-v1.json"
    return schema


def _schema_matches(instance: object, schema: Mapping[str, Any], root: Mapping[str, Any]) -> bool:
    if "$ref" in schema:
        reference = schema["$ref"]
        assert isinstance(reference, str)
        definition = root
        for part in reference.removeprefix("#/").split("/"):
            value = definition.get(part)
            assert isinstance(value, Mapping)
            definition = value
        return _schema_matches(instance, definition, root)
    if "anyOf" in schema:
        alternatives = schema["anyOf"]
        assert isinstance(alternatives, list)
        return any(
            isinstance(alternative, Mapping)
            and _schema_matches(instance, alternative, root)
            for alternative in alternatives
        )
    if "enum" in schema and instance not in schema["enum"]:
        return False
    schema_type = schema.get("type")
    if schema_type == "object":
        if not isinstance(instance, Mapping):
            return False
        required = schema.get("required", [])
        assert isinstance(required, list)
        if any(field not in instance for field in required):
            return False
        properties = schema.get("properties", {})
        assert isinstance(properties, Mapping)
        if schema.get("additionalProperties") is False and any(
            field not in properties for field in instance
        ):
            return False
        return all(
            field not in properties
            or not isinstance(properties[field], Mapping)
            or _schema_matches(value, properties[field], root)
            for field, value in instance.items()
        )
    if schema_type == "array":
        if not isinstance(instance, list):
            return False
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(instance) < minimum:
            return False
        if isinstance(maximum, int) and len(instance) > maximum:
            return False
        items = schema.get("items")
        return not isinstance(items, Mapping) or all(
            _schema_matches(value, items, root) for value in instance
        )
    if schema_type == "string":
        if not isinstance(instance, str):
            return False
        minimum = schema.get("minLength")
        if isinstance(minimum, int) and len(instance) < minimum:
            return False
        pattern = schema.get("pattern")
        return not isinstance(pattern, str) or re.search(pattern, instance) is not None
    if schema_type == "integer":
        if isinstance(instance, bool) or not isinstance(instance, int):
            return False
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        return (not isinstance(minimum, int) or instance >= minimum) and (
            not isinstance(maximum, int) or instance <= maximum
        )
    if schema_type == "boolean":
        return isinstance(instance, bool)
    return True


def _version_payload(draft: BoardQuestionDraft) -> dict[str, Any]:
    return {
        "question_id": "question-1",
        "version": 1,
        "mode": QuestionMode.BOARD_STYLE,
        "draft": draft,
        "source_revision_ids": ("revision-1",),
        "evidence_ids": ("evidence-1",),
        "prompt_version": "prompt-v1",
        "schema_version": "question-v1",
        "model_version": "model-v1",
        "input_hash": "input-hash",
        "output_hash": "output-hash",
    }


def _version_wire_payload(**changes: object) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "question_id": "question-1",
        "version": 1,
        "mode": "board_style",
        "draft": build_board_question_draft(),
        "source_revision_ids": ["revision-1"],
        "evidence_ids": ["evidence-1"],
        "prompt_version": "prompt-v1",
        "schema_version": "question-v1",
        "model_version": "model-v1",
        "input_hash": "input-hash",
        "output_hash": "output-hash",
    }
    payload.update(changes)
    return payload


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


@pytest.mark.parametrize(
    "field",
    (
        "source_revision_ids",
        "prompt_version",
        "schema_version",
        "model_version",
        "input_hash",
        "output_hash",
    ),
)
def test_question_version_requires_provenance_fields(field: str) -> None:
    payload = _version_payload(BoardQuestionDraft.model_validate(build_board_question_draft()))
    del payload[field]
    with pytest.raises(ValueError, match=field):
        QuestionVersion.model_validate(payload)


@pytest.mark.parametrize(
    "field, value",
    (
        ("source_revision_ids", ()),
        ("source_revision_ids", ("",)),
        ("prompt_version", ""),
        ("schema_version", ""),
        ("model_version", ""),
        ("input_hash", ""),
        ("output_hash", ""),
    ),
)
def test_question_version_rejects_blank_provenance_fields(field: str, value: object) -> None:
    payload = _version_payload(BoardQuestionDraft.model_validate(build_board_question_draft()))
    payload[field] = value
    with pytest.raises(ValueError, match=field):
        QuestionVersion.model_validate(payload)


def test_question_models_are_immutable_and_versioned() -> None:
    draft = BoardQuestionDraft.model_validate(build_board_question_draft())
    version = QuestionVersion.model_validate(_version_payload(draft))
    assert version.status is QuestionStatus.DRAFT
    assert version.schema_version == "question-v1"
    with pytest.raises(ValidationError):
        version.status = QuestionStatus.APPROVED


def test_validation_result_is_immutable_and_defaults_to_valid_without_codes() -> None:
    result = QuestionValidationResult(valid=True)
    assert result.codes == ()


def test_question_schema_candidate_v2_is_deterministic_and_v1_snapshot_is_frozen() -> None:
    candidate_schema = _schema_payload()
    candidate_schema["$id"] = "question-v2.json"
    candidate = (json.dumps(candidate_schema, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    assert candidate == (
        json.dumps(candidate_schema, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    assert len(candidate) == 7_379
    assert hashlib.sha256(candidate).hexdigest() == (
        "0f535c43fc1de3eadc61970f615370d3b23bc1046c7bef5f7bdeb01419a8294d"
    )

    frozen_v1 = (ROOT / "schemas" / "question-v1.json").read_bytes()
    assert len(frozen_v1) == 293
    assert hashlib.sha256(frozen_v1).hexdigest() == (
        "968449d9dca8da71a28658360fe6a2d8e61cf35e49c5d8a9ab6e7a4564e7eb9d"
    )
    assert frozen_v1 != candidate


@pytest.mark.parametrize(
    "payload",
    (
        _payload(stem=" \t"),
        _payload(objective_ids=["\n"]),
        _payload(
            options=[
                {
                    **option,
                    "evidence_ids": ["\t"],
                }
                for option in build_board_question_draft()["options"]
            ]
        ),
        _version_wire_payload(source_revision_ids=[" \n"]),
        _version_wire_payload(prompt_version="\t"),
        {"valid": False, "codes": [" "]},
    ),
)
def test_generated_schema_rejects_model_invalid_nonblank_values(
    payload: dict[str, Any],
) -> None:
    schema = _schema_payload()
    assert not _schema_matches(payload, schema, schema)
