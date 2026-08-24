from __future__ import annotations

import pytest
from pydantic import ValidationError

from oms_hub.ask.models import (
    AskMessage,
    AskMode,
    AskPageContext,
    AskRequest,
    AskThread,
    CitationView,
    GroundedAnswer,
    GroundedClaim,
    QuizPageContext,
)
from oms_hub.providers.contracts import RetrievalScope, TruthMode


def _scope() -> RetrievalScope:
    return RetrievalScope(
        course_id="heme",
        exam_id="e2",
        lecture_ids=("l13",),
        truth_mode=TruthMode.COURSE_ONLY,
    )


def test_pre_submit_context_forbids_correct_answer_fields() -> None:
    for field, value in (
        ("correct_option_id", "D"),
        ("correct_answer_text", "heparin-induced thrombocytopenia"),
        ("rationale", "The platelet count falls after heparin exposure."),
        ("is_correct", False),
    ):
        with pytest.raises(ValueError, match="correct|rationale|grading"):
            QuizPageContext.model_validate(
                {
                    "quiz_id": "qz-1",
                    "question_id": "q-1",
                    "submitted": False,
                    "selected_option_id": None,
                    field: value,
                }
            )


def test_pre_submit_context_forbids_unknown_answer_extra() -> None:
    with pytest.raises(ValidationError):
        QuizPageContext.model_validate(
            {
                "quiz_id": "qz-1",
                "question_id": "q-1",
                "submitted": False,
                "selected_option_id": None,
                "correct_answer": "D",
            }
        )


def test_post_submit_context_can_include_grading_fields() -> None:
    context = QuizPageContext(
        quiz_id="qz-1",
        question_id="q-1",
        submitted=True,
        selected_option_id="B",
        correct_option_id="D",
        correct_answer_text="heparin-induced thrombocytopenia",
        rationale="The platelet count falls after heparin exposure.",
        is_correct=False,
    )
    assert context.kind == "quiz_question"
    assert context.correct_option_id == "D"
    assert context.is_correct is False


def test_quiz_question_context_cannot_be_partial_base_context() -> None:
    with pytest.raises(ValidationError):
        AskPageContext.model_validate({"kind": "quiz_question"})


def test_request_rejects_partial_quiz_context() -> None:
    with pytest.raises(ValidationError):
        AskRequest.model_validate(
            {
                "query": "Can you explain this?",
                "mode": AskMode.QUIZ_PRE_SUBMIT,
                "scope": _scope(),
                "page_context": {"kind": "quiz_question", "quiz_id": "qz-1"},
            }
        )


def test_pre_submit_mode_rejects_submitted_quiz_context() -> None:
    context = QuizPageContext(
        quiz_id="qz-1",
        question_id="q-1",
        submitted=True,
        selected_option_id="B",
        correct_option_id="D",
        rationale="The platelet count falls after heparin exposure.",
        is_correct=False,
    )
    with pytest.raises(ValueError, match="pre-submit"):
        AskRequest(
            query="Can you explain this?",
            mode=AskMode.QUIZ_PRE_SUBMIT,
            scope=_scope(),
            page_context=context,
        )


def test_post_submit_mode_rejects_pre_submit_quiz_context() -> None:
    context = QuizPageContext(
        quiz_id="qz-1",
        question_id="q-1",
        submitted=False,
        selected_option_id="B",
    )
    with pytest.raises(ValueError, match="post-submit"):
        AskThread(
            thread_id="thread-1",
            mode=AskMode.QUIZ_POST_SUBMIT,
            scope=_scope(),
            page_context=context,
        )


def test_course_only_is_explicit_and_preserved() -> None:
    request = AskRequest(
        query="Why is PTT prolonged?",
        mode=AskMode.LECTURE,
        scope=_scope(),
    )
    assert request.scope.truth_mode is TruthMode.COURSE_ONLY


def test_request_context_lists_are_immutable() -> None:
    request = AskRequest.model_validate(
        {
            "query": "Explain the mechanism.",
            "mode": AskMode.LECTURE,
            "scope": _scope(),
            "page_context": {"kind": "lecture", "objective_ids": ["obj-1"]},
        }
    )
    assert request.page_context is not None
    assert request.page_context.objective_ids == ("obj-1",)


def test_grounded_answer_contains_claims_and_citations() -> None:
    answer = GroundedAnswer(
        answer_markdown="The pathway is activated.",
        claims=(GroundedClaim(text="The pathway is activated.", evidence_ids=("ev-1",)),),
        citations=(CitationView(evidence_id="ev-1", label="Lecture 13, slide 4"),),
        insufficient_evidence=False,
        provider_request_id="provider-1",
        retrieval_run_id="retrieval-1",
    )
    assert answer.claims[0].evidence_ids == ("ev-1",)
    assert answer.citations[0].evidence_id == "ev-1"


def test_thread_and_message_are_strict_and_frozen() -> None:
    thread = AskThread(thread_id="thread-1", mode=AskMode.GLOBAL, scope=_scope())
    message = AskMessage(
        message_id="message-1",
        thread_id=thread.thread_id,
        role="user",
        content="Explain this.",
    )
    with pytest.raises(ValidationError):
        thread.mode = AskMode.EXAM
    with pytest.raises(ValidationError):
        AskMessage.model_validate(
            {
                "message_id": "message-1",
                "thread_id": "thread-1",
                "role": "user",
                "content": "Explain this.",
                "unexpected": True,
            }
        )
    assert message.role == "user"


@pytest.mark.parametrize(
    "mode",
    [
        AskMode.GLOBAL,
        AskMode.LECTURE,
        AskMode.EXAM,
        AskMode.QUIZ_PRE_SUBMIT,
        AskMode.QUIZ_POST_SUBMIT,
    ],
)
def test_required_ask_modes(mode: AskMode) -> None:
    page_context = None
    if mode is AskMode.QUIZ_PRE_SUBMIT:
        page_context = QuizPageContext(quiz_id="qz-1", question_id="q-1", submitted=False)
    elif mode is AskMode.QUIZ_POST_SUBMIT:
        page_context = QuizPageContext(quiz_id="qz-1", question_id="q-1", submitted=True)
    request = AskRequest(
        query="Explain this.", mode=mode, scope=_scope(), page_context=page_context
    )
    assert request.mode is mode
