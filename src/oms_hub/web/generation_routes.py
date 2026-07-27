from typing import Annotated, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from oms_hub.study_generation.domain import PromptKind
from oms_hub.study_generation.prompts import PromptConfigurationError, PromptFileService
from oms_hub.study_generation.repository import GenerationRepository

router = APIRouter(prefix="/settings/generation")


class PromptPathUpdate(BaseModel):
    path: Annotated[str, Field(min_length=1, max_length=2048)]


def _repository(request: Request) -> GenerationRepository:
    return cast(
        GenerationRepository,
        request.app.state.generation_repository,
    )


def _kind(value: str) -> PromptKind:
    try:
        return PromptKind(value)
    except ValueError as error:
        raise HTTPException(404, "prompt kind was not found") from error


@router.post("/prompts/{kind}")
def save_prompt_path(
    request: Request,
    kind: str,
    update: PromptPathUpdate,
) -> JSONResponse:
    selected = _kind(kind)
    try:
        _repository(request).set_prompt_path(selected, update.path)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return JSONResponse(
        {"kind": selected.value, "path": update.path.strip()},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/prompts/{kind}/test")
def test_prompt_path(request: Request, kind: str) -> JSONResponse:
    selected = _kind(kind)
    try:
        prompt = PromptFileService(_repository(request)).inspect(selected)
    except PromptConfigurationError as error:
        return JSONResponse(
            {"kind": selected.value, "state": "invalid", "message": str(error)},
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        {
            "kind": selected.value,
            "state": "valid",
            "path": str(prompt.path),
            "sha256": prompt.sha256,
            "modified_at": prompt.modified_at,
        },
        headers={"Cache-Control": "no-store"},
    )
