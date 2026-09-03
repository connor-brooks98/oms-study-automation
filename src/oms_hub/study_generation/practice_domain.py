from dataclasses import dataclass
from enum import StrEnum

from oms_hub.study_generation.domain import QuizImageRef


class QuizWorkflowKind(StrEnum):
    NOTEBOOK_GENERATION = "notebook_generation"
    DIRECT_IMPORT = "direct_import"


class QuizContentKind(StrEnum):
    LECTURE_QUIZ = "lecture_quiz"
    EXAM_REVIEW = "exam_review"
    PRACTICE_QUESTIONS = "practice_questions"


class StudioSourcePurpose(StrEnum):
    NOTEBOOK = "notebook"
    LOCAL_IMPORT = "local_import"


class ImportSourceRole(StrEnum):
    QUESTIONS = "questions"
    ANSWER_KEY = "answer_key"
    SUPPORTING_REFERENCE = "supporting_reference"
    COMBINED = "combined_questions_answers"


class AnswerProvenance(StrEnum):
    PROVIDED_BY_SOURCE = "provided_by_source"
    NOTEBOOKLM = "notebooklm"
    GENERATED_BY_AI = "generated_by_ai"
    MANUALLY_CORRECTED = "manually_corrected"


@dataclass(frozen=True, slots=True)
class ImportSourceSelection:
    source_id: str
    role: ImportSourceRole
    attach_to_notebook: bool = False


class DiagnosticSeverity(StrEnum):
    BLOCKER = "blocker"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class DraftDiagnostic:
    code: str
    message: str
    severity: DiagnosticSeverity


@dataclass(frozen=True, slots=True)
class QuestionSourceRef:
    source_id: str
    segment_key: str
    locator: str


@dataclass(frozen=True, slots=True)
class QuestionDraft:
    question_id: str
    original_identifier: str | None
    stem: str
    choices: tuple[str, ...]
    correct_index: int | None
    rationale: str | None
    image_ref: QuizImageRef | None
    source_refs: tuple[QuestionSourceRef, ...]
    answer_provenance: AnswerProvenance | None
    extraction_confidence: float
    diagnostics: tuple[DraftDiagnostic, ...]
    verification_required: bool
    verified_at: str | None

    @property
    def blocking_diagnostics(self) -> tuple[str, ...]:
        return tuple(
            diagnostic.message
            for diagnostic in self.diagnostics
            if diagnostic.severity is DiagnosticSeverity.BLOCKER
        )


@dataclass(frozen=True, slots=True)
class MatchingPromptDraft:
    id: str
    label: str
    text: str
    correct_index: int | None


@dataclass(frozen=True, slots=True)
class MatchingQuestionDraft:
    question_id: str
    original_identifier: str | None
    stem: str
    prompts: tuple[MatchingPromptDraft, ...]
    choices: tuple[str, ...]
    rationale: str | None
    image_ref: QuizImageRef | None
    source_refs: tuple[QuestionSourceRef, ...]
    answer_provenance: AnswerProvenance | None
    extraction_confidence: float
    diagnostics: tuple[DraftDiagnostic, ...]
    verification_required: bool
    verified_at: str | None

    @property
    def blocking_diagnostics(self) -> tuple[str, ...]:
        return tuple(
            item.message
            for item in self.diagnostics
            if item.severity is DiagnosticSeverity.BLOCKER
        )


QuestionDraftValue = QuestionDraft | MatchingQuestionDraft
