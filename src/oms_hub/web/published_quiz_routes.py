from typing import Annotated, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StringConstraints

from oms_hub.study_generation.domain import (
    PublishedQuizLibrarySection,
    PublishedQuizOrderDirection,
)
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


def _repository(request: Request) -> GenerationRepository:
    return cast(GenerationRepository, request.app.state.generation_repository)


@router.delete("/{token}")
def unpublish_quiz(request: Request, token: _PublishedQuizToken) -> JSONResponse:
    require_form_csrf(request, None)
    try:
        unpublished = _repository(request).unpublish_quiz(token)
    except KeyError as error:
        raise HTTPException(404, "published quiz was not found") from error
    return JSONResponse({"token": unpublished, "state": "unpublished"})


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
