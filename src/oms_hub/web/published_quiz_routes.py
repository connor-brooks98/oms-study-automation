from typing import Annotated, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import StringConstraints

from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.web.csrf import require_form_csrf

router = APIRouter(prefix="/api/published-quizzes")
_PublishedQuizToken = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64),
]


@router.delete("/{token}")
def unpublish_quiz(request: Request, token: _PublishedQuizToken) -> JSONResponse:
    require_form_csrf(request, None)
    repository = cast(GenerationRepository, request.app.state.generation_repository)
    try:
        unpublished = repository.unpublish_quiz(token)
    except KeyError as error:
        raise HTTPException(404, "published quiz was not found") from error
    return JSONResponse({"token": unpublished, "state": "unpublished"})
