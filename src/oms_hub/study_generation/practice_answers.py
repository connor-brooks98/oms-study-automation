"""Retain missing imported answers for review without NotebookLM or paid fallback."""

from dataclasses import dataclass, replace
from typing import Protocol

from oms_hub.llm.domain import GeneratedText, LLMTask
from oms_hub.study_generation.notebook import (
    NOTEBOOKLM_UPLOAD_ONLY,
    NotebookQuestionResult,
)
from oms_hub.study_generation.practice_domain import (
    DiagnosticSeverity,
    DraftDiagnostic,
    QuestionDraft,
)


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


class PracticeAnswerResolver:
    def __init__(self, notebook: NotebookQuestionGateway, fallback: TaskTextGenerator) -> None:
        self.notebook = notebook
        self.fallback = fallback

    def resolve(self, draft: QuestionDraft, scope: AnswerResolutionScope) -> QuestionDraft:
        if draft.correct_index is not None:
            return draft
        diagnostic = DraftDiagnostic(
            "notebook-generation-disabled", NOTEBOOKLM_UPLOAD_ONLY, DiagnosticSeverity.BLOCKER
        )
        return replace(
            draft, verification_required=True, verified_at=None,
            diagnostics=tuple(dict.fromkeys((*draft.diagnostics, diagnostic))),
        )
