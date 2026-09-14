import json
from dataclasses import dataclass

from oms_hub.llm.domain import GeneratedText, LLMTask, ProviderName
from oms_hub.study_generation.notebook import NotebookQuestionResult
from oms_hub.study_generation.practice_answers import (
    AnswerResolutionScope,
    PracticeAnswerResolver,
)
from oms_hub.study_generation.practice_domain import (
    AnswerProvenance,
    QuestionDraft,
    QuestionSourceRef,
)


def _draft(correct_index: int | None = None) -> QuestionDraft:
    return QuestionDraft(
        question_id="question-1",
        original_identifier="1",
        stem="Which muscle flexes the elbow?",
        choices=("Biceps", "Triceps"),
        correct_index=correct_index,
        rationale="Supplied rationale" if correct_index is not None else None,
        image_ref=None,
        source_refs=(QuestionSourceRef("questions", "page-1", "page 1"),),
        answer_provenance=(
            AnswerProvenance.PROVIDED_BY_SOURCE if correct_index is not None else None
        ),
        extraction_confidence=0.9,
        diagnostics=(),
        verification_required=correct_index is None,
        verified_at=None,
    )


def _scope() -> AnswerResolutionScope:
    return AnswerResolutionScope("Neuro", 1, ("support-1",))


class FailingNotebook:
    def answer_studio_question(self, *args: object) -> NotebookQuestionResult:
        raise AssertionError("NotebookLM should not be called")


class RaisingNotebook:
    def answer_studio_question(self, *args: object) -> NotebookQuestionResult:
        raise RuntimeError("offline")


class ResultNotebook:
    def __init__(self, result: NotebookQuestionResult) -> None:
        self.result = result
        self.requests: list[tuple[object, ...]] = []

    def answer_studio_question(self, *args: object) -> NotebookQuestionResult:
        self.requests.append(args)
        return self.result


class FailingFallback:
    def generate_text_for_task(self, *args: object, **kwargs: object) -> GeneratedText:
        raise AssertionError("fallback should not be called")


@dataclass(frozen=True)
class FallbackRequest:
    task: LLMTask
    instruction: str
    input_text: str
    output_schema: dict[str, object]


class GeneratedFallback:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.requests: list[FallbackRequest] = []

    def generate_text_for_task(
        self,
        task: LLMTask,
        instruction: str,
        input_text: str,
        *,
        output_schema: dict[str, object],
    ) -> GeneratedText:
        self.requests.append(FallbackRequest(task, instruction, input_text, output_schema))
        return GeneratedText(
            text=json.dumps(self.payload),
            provider=ProviderName.GEMINI,
            model="answer-model",
            request_id="answer-1",
            input_tokens=4,
            output_tokens=5,
            cost_microusd=1,
        )


def _generated(index: int = 1) -> dict[str, object]:
    return {
        "correct_index": index,
        "rationale": "Triceps extends rather than flexes the elbow.",
        "evidence": ["General anatomy reference"],
        "uncertainty_note": "Generated because the selected notebook sources had no support.",
    }


def test_supplied_answer_never_calls_notebook_or_fallback() -> None:
    resolver = PracticeAnswerResolver(FailingNotebook(), FailingFallback())
    resolved = resolver.resolve(_draft(0), _scope())

    assert resolved.answer_provenance is AnswerProvenance.PROVIDED_BY_SOURCE


def test_missing_answer_stays_in_review_without_notebook_or_paid_fallback() -> None:
    resolver = PracticeAnswerResolver(FailingNotebook(), FailingFallback())
    original = _draft()
    result = resolver.resolve(original, _scope())
    assert result.correct_index is None and result.answer_provenance is None
    assert result.verification_required and result.verified_at is None
    assert result.source_refs == original.source_refs
    assert result.blocking_diagnostics and "upload-only" in result.blocking_diagnostics[-1]
    assert resolver.resolve(result, _scope()) == result
