"""Strict, immutable contracts for Ask StudyHub."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from oms_hub.providers.contracts import RetrievalScope


class AskMode(StrEnum):
    """The supported Ask StudyHub interaction modes."""

    GLOBAL = "global"
    LECTURE = "lecture"
    EXAM = "exam"
    QUIZ_PRE_SUBMIT = "quiz_pre_submit"
    QUIZ_POST_SUBMIT = "quiz_post_submit"


class AskPageContext(BaseModel):
    """Safe page metadata sent with an Ask request."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["main_hub", "exam", "lecture", "quiz"]
    objective_ids: tuple[str, ...] = Field(default=())

    @field_validator("objective_ids", mode="before")
    @classmethod
    def lists_become_immutable_tuples(cls, values: object) -> object:
        return tuple(values) if isinstance(values, list) else values


class QuizPageContext(BaseModel):
    """Quiz context whose answer-bearing fields are post-submit only."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["quiz_question"] = "quiz_question"
    objective_ids: tuple[str, ...] = Field(default=())
    quiz_id: str = Field(min_length=1)
    question_id: str = Field(min_length=1)
    submitted: bool
    selected_option_id: str | None = None
    correct_option_id: str | None = None
    correct_answer_text: str | None = None
    rationale: str | None = None
    is_correct: bool | None = None

    @field_validator("objective_ids", mode="before")
    @classmethod
    def lists_become_immutable_tuples(cls, values: object) -> object:
        return tuple(values) if isinstance(values, list) else values

    @model_validator(mode="before")
    @classmethod
    def reject_pre_submit_answer_fields(cls, values: object) -> object:
        if isinstance(values, dict) and values.get("submitted") is False:
            protected = {
                "correct_answer_text",
                "correct_option_id",
                "is_correct",
                "rationale",
            }
            supplied = sorted(protected.intersection(values))
            if supplied:
                fields = ", ".join(supplied)
                raise ValueError(
                    "pre-submit quiz context cannot include correct-answer or rationale "
                    f"fields: {fields}"
                )
        return values


def _validate_quiz_mode(
    mode: AskMode, page_context: AskPageContext | QuizPageContext | None
) -> None:
    if mode is AskMode.QUIZ_PRE_SUBMIT:
        if not isinstance(page_context, QuizPageContext) or page_context.submitted:
            raise ValueError("quiz_pre_submit requires a pre-submit QuizPageContext")
    elif mode is AskMode.QUIZ_POST_SUBMIT:
        if not isinstance(page_context, QuizPageContext) or not page_context.submitted:
            raise ValueError("quiz_post_submit requires a post-submit QuizPageContext")


class AskRequest(BaseModel):
    """A scoped Ask query."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    query: str = Field(min_length=1)
    mode: AskMode
    scope: RetrievalScope
    page_context: AskPageContext | QuizPageContext | None = None
    thread_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def quiz_mode_matches_context(self) -> AskRequest:
        _validate_quiz_mode(self.mode, self.page_context)
        return self


class AskThread(BaseModel):
    """The scope and mode that define an isolated Ask conversation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    thread_id: str = Field(min_length=1)
    mode: AskMode
    scope: RetrievalScope
    page_context: AskPageContext | QuizPageContext | None = None

    @model_validator(mode="after")
    def quiz_mode_matches_context(self) -> AskThread:
        _validate_quiz_mode(self.mode, self.page_context)
        return self


class AskMessage(BaseModel):
    """One user or assistant message in an Ask thread."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    message_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class GroundedClaim(BaseModel):
    """One answer claim and the evidence IDs that support it."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    text: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def lists_become_immutable_tuples(cls, values: object) -> object:
        return tuple(values) if isinstance(values, list) else values


class CitationView(BaseModel):
    """A user-facing citation keyed to a stable Study Hub evidence ID."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    evidence_id: str = Field(min_length=1)
    label: str = Field(min_length=1)


class GroundedAnswer(BaseModel):
    """A grounded answer or a safe fail-closed response."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    answer_markdown: str = Field(min_length=1)
    claims: tuple[GroundedClaim, ...] = Field(default=())
    citations: tuple[CitationView, ...] = Field(default=())
    insufficient_evidence: bool
    safe_response_reason: str | None = None
    provider_request_id: str | None = None
    retrieval_run_id: str | None = None

    @field_validator("claims", "citations", mode="before")
    @classmethod
    def lists_become_immutable_tuples(cls, values: object) -> object:
        return tuple(values) if isinstance(values, list) else values
