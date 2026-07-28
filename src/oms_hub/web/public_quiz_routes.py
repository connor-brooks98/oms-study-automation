from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, StringConstraints

from oms_hub.study_generation.domain import PublishedQuizRecord
from oms_hub.study_generation.native_quiz import (
    grade_answer,
    public_quiz_content,
)
from oms_hub.study_generation.repository import GenerationRepository

router = APIRouter(prefix="/public/quizzes")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_PublicId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][0-9]{1,3}$", max_length=4),
]


class AnswerSubmission(BaseModel):
    question_id: _PublicId
    choice_id: _PublicId


def _repository(request: Request) -> GenerationRepository:
    return cast(
        GenerationRepository,
        request.app.state.generation_repository,
    )


def _published(request: Request, token: str) -> PublishedQuizRecord:
    published = _repository(request).published_quiz(token)
    if published is None:
        raise HTTPException(404, "quiz was not found")
    return published


@router.get("/{token}", response_class=HTMLResponse)
def quiz_page(request: Request, token: str) -> HTMLResponse:
    published = _published(request, token)
    lecture = request.app.state.catalog_repository.get_lecture(
        published.lecture_id
    )
    if lecture is None:
        raise HTTPException(404, "quiz was not found")
    return templates.TemplateResponse(
        request=request,
        name="public_quiz.html",
        context={
            "quiz": published,
            "lecture": lecture,
            "content_url": f"/public/quizzes/{token}/content",
            "answer_url": f"/public/quizzes/{token}/answer",
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{token}/content")
def quiz_content(request: Request, token: str) -> JSONResponse:
    published = _published(request, token)
    lecture = request.app.state.catalog_repository.get_lecture(
        published.lecture_id
    )
    if lecture is None:
        raise HTTPException(404, "quiz was not found")
    return JSONResponse(
        {
            "token": published.token,
            "version": published.version,
            "course": lecture.subject,
            "exam_number": lecture.exam_number,
            "lecture_number": lecture.lecture_number,
            "topic": lecture.topic,
            **public_quiz_content(published.quiz),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/{token}/answer")
def answer_question(
    request: Request,
    token: str,
    submission: AnswerSubmission,
) -> JSONResponse:
    published = _published(request, token)
    try:
        feedback = grade_answer(
            published.quiz,
            submission.question_id,
            submission.choice_id,
        )
    except KeyError as error:
        raise HTTPException(404, "quiz question was not found") from error
    return JSONResponse(
        {
            "correct": feedback.correct,
            "correct_choice_id": feedback.correct_choice_id,
            "rationale": feedback.rationale,
        },
        headers={"Cache-Control": "no-store"},
    )
