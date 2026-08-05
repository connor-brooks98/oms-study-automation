import json
from dataclasses import dataclass

import pytest

from oms_hub.llm.domain import GeneratedText, LLMTask, ProviderName
from oms_hub.study_generation.notebook import NotebookQuestionResult, NotebookQuestionStatus
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


def test_notebook_outage_does_not_call_fallback() -> None:
    fallback = GeneratedFallback(_generated())
    resolver = PracticeAnswerResolver(RaisingNotebook(), fallback)

    with pytest.raises(RuntimeError, match="offline"):
        resolver.resolve(_draft(), _scope())

    assert fallback.requests == []


def test_notebook_answered_sets_notebook_provenance_without_ai_gate() -> None:
    notebook = ResultNotebook(
        NotebookQuestionResult(
            NotebookQuestionStatus.ANSWERED,
            0,
            "Biceps flexes the elbow.",
            ("Course guide p4",),
        )
    )
    fallback = GeneratedFallback(_generated())

    resolved = PracticeAnswerResolver(notebook, fallback).resolve(_draft(), _scope())

    assert resolved.correct_index == 0
    assert resolved.answer_provenance is AnswerProvenance.NOTEBOOKLM
    assert resolved.verification_required is False
    assert resolved.source_refs == _draft().source_refs
    assert fallback.requests == []


def test_explicit_no_support_uses_configured_fallback_and_requires_verification() -> None:
    notebook = ResultNotebook(
        NotebookQuestionResult(NotebookQuestionStatus.NO_SUPPORT, None, "No selected support.", ())
    )
    fallback = GeneratedFallback(_generated())

    resolved = PracticeAnswerResolver(notebook, fallback).resolve(_draft(), _scope())

    assert resolved.correct_index == 1
    assert resolved.answer_provenance is AnswerProvenance.GENERATED_BY_AI
    assert resolved.verification_required is True
    assert resolved.verified_at is None
    assert fallback.requests[0].task is LLMTask.QUIZ_ANSWER_GENERATION
    assert fallback.requests[0].output_schema["type"] == "object"


@pytest.mark.parametrize(
    "payload",
    [
        {**_generated(1), "correct_index": 2},
        {"correct_index": 1, "rationale": "", "evidence": [], "uncertainty_note": ""},
    ],
)
def test_invalid_fallback_contract_does_not_return_a_draft(payload: dict[str, object]) -> None:
    notebook = ResultNotebook(
        NotebookQuestionResult(NotebookQuestionStatus.NO_SUPPORT, None, "No support.", ())
    )

    with pytest.raises(ValueError):
        PracticeAnswerResolver(notebook, GeneratedFallback(payload)).resolve(_draft(), _scope())
