from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, StringConstraints

from oms_hub.files.atomic import sha256_file
from oms_hub.study_generation.domain import PublishedQuizRecord
from oms_hub.study_generation.native_quiz import (
    grade_answer,
    public_quiz_content,
)
from oms_hub.study_generation.practice_domain import QuizContentKind
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.web.artifact_routes import outline_pdf_response
from oms_hub.web.lecture_labels import lecture_label
from oms_hub.web.routes import _course_hue

router = APIRouter(prefix="/public")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_STATIC_ROOT = Path(__file__).parent / "static"
_PublicId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][0-9]{1,3}$", max_length=4),
]
_ImageId = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$",
        max_length=64,
    ),
]


def _player_asset_version() -> str:
    """Content-address public player assets so a new player is visible immediately.

    The page itself is deliberately no-store.  Giving the two long-lived static
    assets a content-derived query value means a browser never reuses the
    previous player for up to its one-hour asset cache lifetime after a rollout.
    """
    javascript = sha256_file(_STATIC_ROOT / "public_quiz.js")[:12]
    stylesheet = sha256_file(_STATIC_ROOT / "public_quiz.css")[:12]
    return f"{javascript}-{stylesheet}-{_shared_style_version()}"


def _library_asset_version() -> str:
    """Content-address the cached public quiz-library assets."""
    javascript = sha256_file(_STATIC_ROOT / "public_quiz_library.js")[:12]
    stylesheet = sha256_file(_STATIC_ROOT / "public_quiz_library.css")[:12]
    return f"{javascript}-{stylesheet}-{_shared_style_version()}"


def _shared_style_version() -> str:
    """Content-address the fixed public shell styles as one deterministic set."""
    return "-".join(
        sha256_file(_STATIC_ROOT / name)[:12]
        for name in ("reset.css", "tokens.css", "study-hub.css")
    )


class AnswerSubmission(BaseModel):
    question_id: _PublicId
    choice_id: _PublicId


@router.get("/quizzes/assets/player.js", include_in_schema=False)
def player_javascript() -> FileResponse:
    return FileResponse(
        _STATIC_ROOT / "public_quiz.js",
        media_type="text/javascript",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/quizzes/assets/player.css", include_in_schema=False)
def player_styles() -> FileResponse:
    return FileResponse(
        _STATIC_ROOT / "public_quiz.css",
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/quizzes/assets/library.js", include_in_schema=False)
def library_javascript() -> FileResponse:
    return FileResponse(
        _STATIC_ROOT / "public_quiz_library.js",
        media_type="text/javascript",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/quizzes/assets/library.css", include_in_schema=False)
def library_styles() -> FileResponse:
    return FileResponse(
        _STATIC_ROOT / "public_quiz_library.css",
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/quizzes/assets/tokens.css", include_in_schema=False)
def design_tokens() -> FileResponse:
    return FileResponse(
        _STATIC_ROOT / "tokens.css",
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/quizzes/assets/reset.css", include_in_schema=False)
def reset_styles() -> FileResponse:
    return FileResponse(
        _STATIC_ROOT / "reset.css",
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/quizzes/assets/study-hub.css", include_in_schema=False)
def study_hub_styles() -> FileResponse:
    return FileResponse(
        _STATIC_ROOT / "study-hub.css",
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


def _normalized_subject(value: str) -> str:
    return " ".join(value.casefold().split())


def _quiz_library(
    request: Request,
    content_kinds: frozenset[QuizContentKind],
    *,
    title: str,
    summary: str,
    empty_title: str,
    empty_summary: str,
    library_path: str,
    management_mode: bool = False,
) -> HTMLResponse:
    courses: dict[str, dict[int, list[dict[str, object]]]] = {}
    course_names: dict[str, str] = {}
    repository = _repository(request)
    for published in repository.published_quizzes(content_kinds):
        lecture = (
            request.app.state.catalog_repository.get_lecture(published.lecture_id)
            if published.lecture_id is not None
            else None
        )
        subject = lecture.subject if lecture is not None else published.destination_subject
        subject_key = _normalized_subject(
            lecture.subject if lecture is not None else published.destination_subject_key
        )
        exam_number = (
            lecture.exam_number
            if lecture is not None
            else published.destination_exam_number
        )
        outline = (
            repository.current_outline(published.lecture_id)
            if published.lecture_id is not None
            else None
        )
        course_names.setdefault(subject_key, subject)
        courses.setdefault(subject_key, {}).setdefault(
            exam_number,
            [],
        ).append(
            {
                "token": published.token,
                "version": published.version,
                "title": published.title,
                "display_order": published.display_order,
                "lecture_number": (
                    lecture.lecture_number if lecture is not None else None
                ),
                "is_studio": lecture is None,
                "primary_label": (
                    lecture_label(lecture.subject, lecture.lecture_number)
                    if lecture is not None
                    else published.title
                ),
                "secondary_label": lecture.topic if lecture is not None else None,
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
            "name": course_names[subject_key],
            "key": subject_key,
            "hue": _course_hue(course_names[subject_key]),
            "quiz_count": sum(len(rows) for rows in exams.values()),
            "exams": tuple(
                {
                    "number": number,
                    "quiz_count": len(exams[number]),
                    "quizzes": tuple(exams[number]),
                }
                for number in sorted(exams)
            ),
        }
        for subject_key, exams in sorted(
            courses.items(),
            key=lambda item: item[0],
        )
    )
    return templates.TemplateResponse(
        request=request,
        name="public_quiz_library.html",
        context={
            "courses": grouped,
            "library_title": title,
            "library_summary": summary,
            "empty_title": empty_title,
            "empty_summary": empty_summary,
            "library_path": library_path,
            "management_mode": management_mode,
            "quiz_library_path": (
                "/studio/library/quizzes" if management_mode else "/public/quizzes"
            ),
            "practice_library_path": (
                "/studio/library/practice-questions"
                if management_mode
                else "/public/practice-questions"
            ),
            "library_asset_version": _library_asset_version(),
        },
        headers={"Cache-Control": "no-store"},
    )


def quiz_library_response(
    request: Request,
    *,
    management_mode: bool = False,
) -> HTMLResponse:
    return _quiz_library(
        request,
        frozenset({QuizContentKind.LECTURE_QUIZ, QuizContentKind.EXAM_REVIEW}),
        title="Course quiz library",
        summary="Choose a course, exam, and quiz.",
        empty_title="No quizzes are published yet",
        empty_summary="Published lecture and exam-review quizzes will appear here automatically.",
        library_path=(
            "/studio/library/quizzes" if management_mode else "/public/quizzes"
        ),
        management_mode=management_mode,
    )


def practice_question_library_response(
    request: Request,
    *,
    management_mode: bool = False,
) -> HTMLResponse:
    return _quiz_library(
        request,
        frozenset({QuizContentKind.PRACTICE_QUESTIONS}),
        title="Practice questions library",
        summary="Choose a course and exam to review imported practice questions.",
        empty_title="No practice questions are published yet",
        empty_summary="Published practice questions will appear here automatically.",
        library_path=(
            "/studio/library/practice-questions"
            if management_mode
            else "/public/practice-questions"
        ),
        management_mode=management_mode,
    )


@router.get("/quizzes", response_class=HTMLResponse)
def quiz_library(request: Request) -> HTMLResponse:
    return quiz_library_response(request)


@router.get("/practice-questions", response_class=HTMLResponse)
def practice_question_library(request: Request) -> HTMLResponse:
    return practice_question_library_response(request)


@router.get("/quizzes/{token}", response_class=HTMLResponse)
def quiz_page(request: Request, token: str) -> HTMLResponse:
    published = _published(request, token)
    is_practice_questions = (
        published.content_kind == QuizContentKind.PRACTICE_QUESTIONS
    )
    lecture = (
        request.app.state.catalog_repository.get_lecture(published.lecture_id)
        if published.lecture_id is not None
        else None
    )
    return templates.TemplateResponse(
        request=request,
        name="public_quiz.html",
        context={
            "quiz": published,
            "quiz_context": {
                "subject": (
                    lecture.subject
                    if lecture is not None
                    else published.destination_subject
                ),
                "exam_number": (
                    lecture.exam_number
                    if lecture is not None
                    else published.destination_exam_number
                ),
                "lecture_number": (
                    lecture.lecture_number if lecture is not None else None
                ),
            },
            "content_url": f"/public/quizzes/{token}/content",
            "answer_url": f"/public/quizzes/{token}/answer",
            "library_url": (
                "/public/practice-questions"
                if is_practice_questions
                else "/public/quizzes"
            ),
            "library_label": (
                "Back to practice questions"
                if is_practice_questions
                else "Back to quizzes"
            ),
            "player_asset_version": _player_asset_version(),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/quizzes/{token}/content")
def quiz_content(request: Request, token: str) -> JSONResponse:
    published = _published(request, token)
    lecture = (
        request.app.state.catalog_repository.get_lecture(published.lecture_id)
        if published.lecture_id is not None
        else None
    )
    image_urls = {
        media.image_key: (
            f"/public/quizzes/{published.token}/media/{media.image_key}",
            media.alt_text,
            media.width,
            media.height,
        )
        for media in _repository(request).published_quiz_media(published.token)
    }
    content = {
        "token": published.token,
        "version": published.version,
        "course": (
            lecture.subject
            if lecture is not None
            else published.destination_subject
        ),
        "exam_number": (
            lecture.exam_number
            if lecture is not None
            else published.destination_exam_number
        ),
        "topic": (
            lecture.topic
            if lecture is not None
            else published.title
        ),
        **public_quiz_content(published.quiz, image_urls),
    }
    if lecture is not None:
        content["lecture_number"] = lecture.lecture_number
    return JSONResponse(
        content,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/quizzes/{token}/media/{image_key}")
def public_quiz_media(
    request: Request,
    token: str,
    image_key: _ImageId,
) -> FileResponse:
    _published(request, token)
    media = _repository(request).published_quiz_media_item(token, image_key)
    if media is None or not media.path.is_file():
        raise HTTPException(404, "quiz image was not found")
    try:
        valid = sha256_file(media.path) == media.sha256
    except OSError:
        valid = False
    if not valid:
        raise HTTPException(404, "quiz image was not found")
    return FileResponse(
        media.path,
        media_type=media.media_type,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/quizzes/{token}/outline")
def public_outline(request: Request, token: str) -> FileResponse:
    published = _published(request, token)
    if published.lecture_id is None:
        raise HTTPException(404, "outline was not found")
    record = _repository(request).current_outline(published.lecture_id)
    if record is None:
        raise HTTPException(404, "outline was not found")
    return outline_pdf_response(request, record)


@router.post("/quizzes/{token}/answer")
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
