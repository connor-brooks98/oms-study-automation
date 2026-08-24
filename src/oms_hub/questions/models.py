"""Immutable source-grounded board-question contracts."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class QuestionMode(StrEnum):
    LECTURE_RECALL = "lecture_recall"
    LECTURE_APPLICATION = "lecture_application"
    BOARD_STYLE = "board_style"
    INTEGRATED_BOARD_STYLE = "integrated_board_style"
    COMLEX_OMM = "comlex_omm"
    REMEDIATION = "remediation"
    TIMED_MIXED_BLOCK = "timed_mixed_block"


class QuestionStatus(StrEnum):
    DRAFT = "draft"
    VALIDATING = "validating"
    QUARANTINED = "quarantined"
    APPROVED = "approved"
    RETIRED = "retired"


class QuestionClaimRole(StrEnum):
    STEM = "stem"
    CORRECT_SUPPORT = "correct_support"
    DISTRACTOR_SUPPORT = "distractor_support"
    RATIONALE = "rationale"
    TEACHING_POINT = "teaching_point"


def _nonblank(value: str, label: str) -> str:
    if not value.strip():
        raise ValueError(f"{label} must not be blank")
    return value


def _nonblank_ids(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if any(not value.strip() for value in values):
        raise ValueError(f"{label} must contain nonempty IDs")
    return values


_NonblankString = Annotated[str, Field(min_length=1, pattern=r"\S")]


class _QuestionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class QuestionOption(_QuestionModel):
    option_id: _NonblankString
    text: _NonblankString
    rationale: _NonblankString
    evidence_ids: tuple[_NonblankString, ...] = Field(min_length=1)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def lists_become_tuples(cls, values: object) -> object:
        return tuple(values) if isinstance(values, list) else values

    @field_validator("option_id", "text", "rationale")
    @classmethod
    def text_is_nonblank(cls, value: str, info: object) -> str:
        label = getattr(info, "field_name", "value")
        return _nonblank(value, label)

    @field_validator("evidence_ids")
    @classmethod
    def evidence_ids_are_nonblank(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _nonblank_ids(values, "evidence_ids")


class QuestionClaim(_QuestionModel):
    claim_id: _NonblankString
    role: QuestionClaimRole
    text: _NonblankString
    evidence_ids: tuple[_NonblankString, ...] = Field(min_length=1)

    @field_validator("role", mode="before")
    @classmethod
    def strings_become_roles(cls, value: object) -> object:
        return QuestionClaimRole(value) if isinstance(value, str) else value

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def lists_become_tuples(cls, values: object) -> object:
        return tuple(values) if isinstance(values, list) else values

    @field_validator("claim_id", "text")
    @classmethod
    def text_is_nonblank(cls, value: str, info: object) -> str:
        label = getattr(info, "field_name", "value")
        return _nonblank(value, label)

    @field_validator("evidence_ids")
    @classmethod
    def evidence_ids_are_nonblank(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _nonblank_ids(values, "evidence_ids")


class BoardQuestionDraft(_QuestionModel):
    stem: _NonblankString
    lead_in: _NonblankString
    options: tuple[QuestionOption, ...] = Field(min_length=4, max_length=5)
    correct_option_id: _NonblankString
    objective_ids: tuple[_NonblankString, ...] = Field(min_length=1)
    difficulty: int = Field(ge=1, le=5)
    blueprint_tags: tuple[_NonblankString, ...] = ()
    claims: tuple[QuestionClaim, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def structural_counts(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        options = value.get("options")
        if isinstance(options, (list, tuple)) and len(options) not in (4, 5):
            raise ValueError("options must contain four or five options")
        objective_ids = value.get("objective_ids")
        if isinstance(objective_ids, (list, tuple)) and not objective_ids:
            raise ValueError("at least one objective ID is required")
        return value

    @field_validator("options", "objective_ids", "blueprint_tags", "claims", mode="before")
    @classmethod
    def lists_become_tuples(cls, values: object) -> object:
        return tuple(values) if isinstance(values, list) else values

    @field_validator("stem", "lead_in", "correct_option_id")
    @classmethod
    def text_is_nonblank(cls, value: str, info: object) -> str:
        label = getattr(info, "field_name", "value")
        return _nonblank(value, label)

    @field_validator("objective_ids", "blueprint_tags")
    @classmethod
    def ids_are_nonblank(cls, values: tuple[str, ...], info: object) -> tuple[str, ...]:
        label = getattr(info, "field_name", "value")
        return _nonblank_ids(values, label)

    @model_validator(mode="after")
    def option_ids_are_unique_and_correct_id_exists(self) -> BoardQuestionDraft:
        option_ids = tuple(option.option_id for option in self.options)
        if len(set(option_ids)) != len(option_ids):
            raise ValueError("option IDs must be unique")
        if self.correct_option_id not in option_ids:
            raise ValueError("correct_option_id must identify an existing option")
        return self


class QuestionValidationResult(_QuestionModel):
    valid: bool
    codes: tuple[_NonblankString, ...] = ()

    @field_validator("codes", mode="before")
    @classmethod
    def lists_become_tuples(cls, values: object) -> object:
        return tuple(values) if isinstance(values, list) else values

    @field_validator("codes")
    @classmethod
    def codes_are_nonblank(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _nonblank_ids(values, "codes")


class QuestionVersion(_QuestionModel):
    question_id: _NonblankString
    version: int = Field(ge=1)
    mode: QuestionMode
    status: QuestionStatus = QuestionStatus.DRAFT
    draft: BoardQuestionDraft
    source_revision_ids: tuple[_NonblankString, ...] = Field(min_length=1)
    evidence_ids: tuple[_NonblankString, ...] = ()
    prompt_version: _NonblankString
    schema_version: _NonblankString
    model_version: _NonblankString
    input_hash: _NonblankString
    output_hash: _NonblankString

    @field_validator("mode", mode="before")
    @classmethod
    def strings_become_modes(cls, value: object) -> object:
        return QuestionMode(value) if isinstance(value, str) else value

    @field_validator("status", mode="before")
    @classmethod
    def strings_become_statuses(cls, value: object) -> object:
        return QuestionStatus(value) if isinstance(value, str) else value

    @field_validator("source_revision_ids", "evidence_ids", mode="before")
    @classmethod
    def lists_become_tuples(cls, values: object) -> object:
        return tuple(values) if isinstance(values, list) else values

    @field_validator("source_revision_ids", "evidence_ids")
    @classmethod
    def provenance_ids_are_nonblank(
        cls, values: tuple[str, ...], info: object
    ) -> tuple[str, ...]:
        label = getattr(info, "field_name", "value")
        return _nonblank_ids(values, label)

    @field_validator(
        "question_id",
        "prompt_version",
        "schema_version",
        "model_version",
        "input_hash",
        "output_hash",
    )
    @classmethod
    def versions_are_nonblank(cls, value: str, info: object) -> str:
        label = getattr(info, "field_name", "value")
        return _nonblank(value, label)
