"""NotebookLM-first missing-answer resolution for imported practice questions."""

import json
from dataclasses import dataclass, replace
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from oms_hub.llm.domain import GeneratedText, LLMTask
from oms_hub.study_generation.notebook import NotebookQuestionResult, NotebookQuestionStatus
from oms_hub.study_generation.practice_domain import AnswerProvenance, QuestionDraft


@dataclass(frozen=True, slots=True)
class AnswerResolutionScope:
    subject: str
    exam_number: int
    supporting_source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.subject.strip() or self.exam_number < 1:
            raise ValueError("answer resolution scope is invalid")
        if not self.supporting_source_ids or len(self.supporting_source_ids) != len(
            set(self.supporting_source_ids)
        ):
            raise ValueError("supporting source IDs must be distinct and nonempty")


class NotebookQuestionGateway(Protocol):
    def answer_studio_question(
        self,
        subject: str,
        exam_number: int,
        question: QuestionDraft,
        source_ids: tuple[str, ...],
    ) -> NotebookQuestionResult: ...


class TaskTextGenerator(Protocol):
    def generate_text_for_task(
        self,
        task: LLMTask,
        instruction: str,
        input_text: str,
        *,
        output_schema: dict[str, object],
    ) -> GeneratedText: ...


class GeneratedAnswerContract(BaseModel):
    """Strict fallback answer contract; evidence and uncertainty remain auditable."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    correct_index: int = Field(ge=0, le=7)
    rationale: str = Field(min_length=1, max_length=20_000)
    evidence: tuple[str, ...] = Field(min_length=1, max_length=50)
    uncertainty_note: str = Field(min_length=1, max_length=10_000)

    @field_validator("evidence", mode="before")
    @classmethod
    def lists_become_tuples(cls, values: object) -> object:
        return tuple(values) if isinstance(values, list) else values

    @field_validator("evidence")
    @classmethod
    def evidence_is_nonempty_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("evidence entries must not be blank")
        return tuple(value.strip() for value in values)

    @field_validator("rationale", "uncertainty_note")
    @classmethod
    def required_text_is_not_whitespace(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("required text must not be blank")
        return normalized


class PracticeAnswerResolver:
    def __init__(self, notebook: NotebookQuestionGateway, fallback: TaskTextGenerator) -> None:
        self.notebook = notebook
        self.fallback = fallback

    def resolve(self, draft: QuestionDraft, scope: AnswerResolutionScope) -> QuestionDraft:
        if draft.correct_index is not None:
            return draft
        notebook_result = self.notebook.answer_studio_question(
            scope.subject,
            scope.exam_number,
            draft,
            scope.supporting_source_ids,
        )
        if notebook_result.status is NotebookQuestionStatus.ANSWERED:
            correct_index = _checked_index(notebook_result.correct_index, draft)
            return replace(
                draft,
                correct_index=correct_index,
                rationale=notebook_result.rationale,
                answer_provenance=AnswerProvenance.NOTEBOOKLM,
                verification_required=False,
                verified_at=None,
            )
        if notebook_result.status is not NotebookQuestionStatus.NO_SUPPORT:
            raise ValueError("NotebookLM returned an unsupported answer status")
        generated = self._generate_fallback(draft, notebook_result)
        correct_index = _checked_index(generated.correct_index, draft)
        return replace(
            draft,
            correct_index=correct_index,
            rationale=generated.rationale,
            answer_provenance=AnswerProvenance.GENERATED_BY_AI,
            verification_required=True,
            verified_at=None,
        )

    def _generate_fallback(
        self,
        draft: QuestionDraft,
        notebook_result: NotebookQuestionResult,
    ) -> GeneratedAnswerContract:
        generated = self.fallback.generate_text_for_task(
            LLMTask.QUIZ_ANSWER_GENERATION,
            "Return only JSON matching the answer schema. Select exactly one answer, provide "
            "a rationale, evidence list, and an uncertainty note. NotebookLM reported no "
            "support in the selected sources.",
            json.dumps(
                {
                    "question": draft.stem,
                    "choices": list(draft.choices),
                    "notebook_evidence": list(notebook_result.evidence),
                }
            ),
            output_schema=GeneratedAnswerContract.model_json_schema(),
        )
        try:
            return GeneratedAnswerContract.model_validate(json.loads(generated.text))
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as error:
            raise ValueError("fallback answer violates the required contract") from error


def _checked_index(value: int | None, draft: QuestionDraft) -> int:
    if value is None or value >= len(draft.choices):
        raise ValueError("answer index is outside the available choices")
    return value
