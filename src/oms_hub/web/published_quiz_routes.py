import json
from typing import Annotated, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StringConstraints

from oms_hub.study_generation.domain import (
    PublishedQuizLibrarySection,
    PublishedQuizOrderDirection,
)
from oms_hub.study_generation.practice_domain import QuizContentKind
from oms_hub.study_generation.native_quiz import serialize_native_quiz
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.web.csrf import require_form_csrf

router = APIRouter(prefix="/api/published-quizzes")
_PublishedQuizToken = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64),
]


class PublishedQuizTitleUpdate(BaseModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class PublishedQuizLibraryMove(BaseModel):
    section: PublishedQuizLibrarySection


class PublishedQuizOrderMove(BaseModel):
    direction: PublishedQuizOrderDirection


class PublishedQuizPayloadUpdate(BaseModel):
    payload_json: Annotated[str, StringConstraints(min_length=2, max_length=200_000)]


def _repository(request: Request) -> GenerationRepository:
    return cast(GenerationRepository, request.app.state.generation_repository)


def _normalized_subject(value: str) -> str:
    return " ".join(value.casefold().split())


def _library_scope(request: Request, token: str) -> dict[str, object]:
    """Capture the immutable identity needed to read a changed library scope."""
    repository = _repository(request)
    published = repository.published_quiz(token)
    if published is None:
        raise KeyError(token)
    catalog = request.app.state.catalog_repository
    lecture = (
        catalog.get_lecture(published.lecture_id)
        if published.lecture_id is not None
        else None
    )
    subject = lecture.subject if lecture is not None else published.destination_subject
    exam_number = (
        lecture.exam_number if lecture is not None else published.destination_exam_number
    )
    kinds = (
        frozenset({QuizContentKind.PRACTICE_QUESTIONS})
        if published.content_kind == QuizContentKind.PRACTICE_QUESTIONS.value
        else frozenset({QuizContentKind.LECTURE_QUIZ, QuizContentKind.EXAM_REVIEW})
    )
    subject_key = _normalized_subject(subject)
    return {
        "course_key": subject_key,
        "exam_number": exam_number,
        "exam_key": f"{subject_key}:{exam_number}",
        "content_kinds": kinds,
    }


def _library_counts(request: Request, scope: dict[str, object]) -> dict[str, int]:
    """Read the post-mutation scope rather than deriving counts from stale data."""
    repository = _repository(request)
    catalog = request.app.state.catalog_repository
    subject_key = cast(str, scope["course_key"])
    exam_number = cast(int, scope["exam_number"])
    kinds = cast(frozenset[QuizContentKind], scope["content_kinds"])
    course_count = exam_count = 0
    for candidate in repository.published_quizzes(kinds):
        candidate_lecture = (
            catalog.get_lecture(candidate.lecture_id)
            if candidate.lecture_id is not None
            else None
        )
        candidate_subject = (
            candidate_lecture.subject
            if candidate_lecture is not None
            else candidate.destination_subject
        )
        candidate_exam = (
            candidate_lecture.exam_number
            if candidate_lecture is not None
            else candidate.destination_exam_number
        )
        if _normalized_subject(candidate_subject) != subject_key:
            continue
        course_count += 1
        if candidate_exam == exam_number:
            exam_count += 1
    return {"course_quiz_count": course_count, "exam_quiz_count": exam_count}


@router.delete("/{token}")
def unpublish_quiz(request: Request, token: _PublishedQuizToken) -> JSONResponse:
    require_form_csrf(request, None)
    try:
        scope = _library_scope(request, token)
        unpublished = _repository(request).unpublish_quiz(token)
    except KeyError as error:
        raise HTTPException(404, "published quiz was not found") from error
    return JSONResponse(
        {
            "token": unpublished,
            "state": "unpublished",
            **{key: value for key, value in scope.items() if key != "content_kinds"},
            **_library_counts(request, scope),
        }
    )


@router.patch("/{token}/title")
def rename_quiz(
    request: Request,
    token: _PublishedQuizToken,
    payload: PublishedQuizTitleUpdate,
) -> JSONResponse:
    require_form_csrf(request, None)
    try:
        published = _repository(request).rename_published_quiz(token, payload.title)
    except KeyError as error:
        raise HTTPException(404, "published quiz was not found") from error
    return JSONResponse({"token": published.token, "title": published.title})


@router.patch("/{token}/library")
def move_quiz_to_library(
    request: Request,
    token: _PublishedQuizToken,
    payload: PublishedQuizLibraryMove,
) -> JSONResponse:
    require_form_csrf(request, None)
    try:
        published = _repository(request).move_published_quiz(token, payload.section)
    except KeyError as error:
        raise HTTPException(404, "published quiz was not found") from error
    return JSONResponse(
        {
            "token": published.token,
            "section": payload.section.value,
            "content_kind": published.content_kind,
        }
    )


@router.patch("/{token}/order")
def reorder_quiz(
    request: Request,
    token: _PublishedQuizToken,
    payload: PublishedQuizOrderMove,
) -> JSONResponse:
    require_form_csrf(request, None)
    try:
        published = _repository(request).reorder_published_quiz(token, payload.direction)
    except KeyError as error:
        raise HTTPException(404, "published quiz was not found") from error
    return JSONResponse(
        {
            "token": published.token,
            "direction": payload.direction.value,
            "display_order": published.display_order,
        }
    )


@router.patch("/{token}/payload")
def replace_quiz_payload(
    request: Request,
    token: _PublishedQuizToken,
    payload: PublishedQuizPayloadUpdate,
) -> JSONResponse:
    require_form_csrf(request, None)
    try:
        published = _repository(request).replace_published_quiz_payload(
            token, payload.payload_json
        )
    except KeyError as error:
        raise HTTPException(404, "published quiz was not found") from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return JSONResponse(
        {"token": published.token, "title": published.title, "version": published.version}
    )


@router.get("/{token}/payload")
def quiz_payload(request: Request, token: _PublishedQuizToken) -> JSONResponse:
    published = _repository(request).published_quiz(token)
    if published is None:
        raise HTTPException(404, "published quiz was not found")
    return JSONResponse(json.loads(serialize_native_quiz(published.quiz)))


@router.get("/{token}/flags")
def open_quiz_flags(request: Request, token: _PublishedQuizToken) -> JSONResponse:
    try:
        return JSONResponse({"flags": _repository(request).open_published_quiz_flags(token)})
    except KeyError as error:
        raise HTTPException(404, "published quiz was not found") from error
