from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, StringConstraints

from oms_hub.security.rate_limit import (
    PublicQuizRateLimiter,
    public_client_identifier,
)
from oms_hub.study_generation.domain import PublishedQuizRecord
from oms_hub.study_generation.native_quiz import (
    grade_answer,
    public_quiz_content,
)
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.web.artifact_routes import outline_pdf_response

router = APIRouter(prefix="/public/quizzes")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_STATIC_ROOT = Path(__file__).parent / "static"
_PublicId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][0-9]{1,3}$", max_length=4),
]


class AnswerSubmission(BaseModel):
    question_id: _PublicId
    choice_id: _PublicId


def _lecture_number(row: dict[str, object]) -> int:
    return cast(int, row["lecture_number"])


@router.get("/assets/player.js", include_in_schema=False)
def player_javascript() -> FileResponse:
    return FileResponse(
        _STATIC_ROOT / "public_quiz.js",
        media_type="text/javascript",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/assets/player.css", include_in_schema=False)
def player_styles() -> FileResponse:
    return FileResponse(
        _STATIC_ROOT / "public_quiz.css",
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/assets/tokens.css", include_in_schema=False)
def shared_tokens() -> FileResponse:
    return FileResponse(
        _STATIC_ROOT / "tokens.css",
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/assets/library.js", include_in_schema=False)
def library_javascript() -> FileResponse:
    return FileResponse(
        _STATIC_ROOT / "public_quiz_library.js",
        media_type="text/javascript",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/assets/library.css", include_in_schema=False)
def library_styles() -> FileResponse:
    return FileResponse(
        _STATIC_ROOT / "public_quiz_library.css",
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=3600"},
    )


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


def _enforce_rate_limit(
    request: Request,
    category: str = "general",
) -> None:
    limiter = cast(
        PublicQuizRateLimiter,
        request.app.state.public_quiz_rate_limiter,
    )
    public_hostname = request.app.state.settings.public_hostname
    forwarded_address = (
        request.headers.get("CF-Connecting-IP")
        if public_hostname
        and (request.url.hostname or "").casefold() == public_hostname.casefold()
        else None
    )
    decision = limiter.check(
        public_client_identifier(
            forwarded_address,
            request.client.host if request.client else None,
        ),
        category,
    )
    if not decision.allowed:
        raise HTTPException(
            429,
            "Too many quiz requests. Please wait a moment and try again.",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )


@router.get("", response_class=HTMLResponse)
def quiz_library(request: Request) -> HTMLResponse:
    _enforce_rate_limit(request)
    courses: dict[str, dict[int, list[dict[str, object]]]] = {}
    repository = _repository(request)
    for published in repository.published_quizzes():
        lecture = request.app.state.catalog_repository.get_lecture(
            published.lecture_id
        )
        if lecture is None:
            continue
        outline = repository.current_outline(published.lecture_id)
        courses.setdefault(lecture.subject, {}).setdefault(
            lecture.exam_number,
            [],
        ).append(
            {
                "token": published.token,
                "version": published.version,
                "title": published.title,
                "lecture_number": lecture.lecture_number,
                "topic": lecture.topic,
                "url": f"/public/quizzes/{published.token}",
                "outline_url": (
                    f"/public/quizzes/{published.token}/outline"
                    if outline is not None
                    else None
                ),
            }
        )
    grouped = tuple(
        {
            "name": subject,
            "quiz_count": sum(len(rows) for rows in exams.values()),
            "exams": tuple(
                {
                    "number": number,
                    "quizzes": tuple(
                        sorted(
                            exams[number],
                            key=_lecture_number,
                        )
                    ),
                }
                for number in sorted(exams)
            ),
        }
        for subject, exams in sorted(
            courses.items(),
            key=lambda item: item[0].casefold(),
        )
    )
    return templates.TemplateResponse(
        request=request,
        name="public_quiz_library.html",
        context={"courses": grouped},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{token}", response_class=HTMLResponse)
def quiz_page(request: Request, token: str) -> HTMLResponse:
    _enforce_rate_limit(request)
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
    _enforce_rate_limit(request)
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


@router.get("/{token}/outline")
def public_outline(request: Request, token: str) -> FileResponse:
    _enforce_rate_limit(request, "outline")
    published = _published(request, token)
    record = _repository(request).current_outline(published.lecture_id)
    if record is None:
        raise HTTPException(404, "outline was not found")
    return outline_pdf_response(request, record)


@router.post("/{token}/answer")
def answer_question(
    request: Request,
    token: str,
    submission: AnswerSubmission,
) -> JSONResponse:
    _enforce_rate_limit(request)
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
