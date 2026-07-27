import threading
from typing import Annotated, cast

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from oms_hub.study_generation.domain import (
    GenerationJob,
    GenerationKind,
    PromptKind,
)
from oms_hub.study_generation.google_connection import (
    GoogleConnectionService,
    GoogleConnectionStatus,
)
from oms_hub.study_generation.prompts import PromptConfigurationError, PromptFileService
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.study_generation.service import (
    GenerationPrerequisiteError,
    GenerationService,
)

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


def _google(request: Request) -> GoogleConnectionService:
    return cast(GoogleConnectionService, request.app.state.google_connection)


def _google_payload(status: GoogleConnectionStatus) -> dict[str, object]:
    return {
        "state": status.state,
        "account_email": status.account_email,
        "surfaces": [
            {"name": surface.name, "state": surface.state}
            for surface in status.surfaces
        ],
        "message": status.message,
    }


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


google_router = APIRouter(prefix="/settings/google")
lecture_router = APIRouter(prefix="/lectures")


def _generation_service(request: Request) -> GenerationService:
    return cast(GenerationService, request.app.state.generation_service)


def _job_payload(job: GenerationJob | None) -> dict[str, object]:
    if job is None:
        return {"state": "ready", "stage": None, "message": None}
    return {
        "id": job.id,
        "state": job.state.value,
        "stage": job.stage.value,
        "message": job.error,
    }


@lecture_router.get("/{lecture_id}/generation-status")
def lecture_generation_status(request: Request, lecture_id: int) -> JSONResponse:
    if request.app.state.catalog_repository.get_lecture(lecture_id) is None:
        raise HTTPException(404, "lecture was not found")
    repository = _repository(request)
    outline = repository.current_outline(lecture_id)
    quiz = repository.current_quiz(lecture_id)
    return JSONResponse(
        {
            "outline": {
                **_job_payload(
                    repository.current_job(lecture_id, GenerationKind.OUTLINE)
                ),
                "url": (
                    f"/artifacts/outlines/{outline.id}"
                    if outline is not None
                    else None
                ),
            },
            "quiz": {
                **_job_payload(
                    repository.current_job(lecture_id, GenerationKind.QUIZ)
                ),
                "url": quiz.url if quiz is not None else None,
            },
        },
        headers={"Cache-Control": "no-store"},
    )


def _queue_generation(
    request: Request,
    lecture_id: int,
    kind: GenerationKind,
) -> JSONResponse:
    try:
        job = (
            _generation_service(request).queue_outline(lecture_id)
            if kind is GenerationKind.OUTLINE
            else _generation_service(request).queue_quiz(lecture_id)
        )
    except KeyError as error:
        raise HTTPException(404, "lecture was not found") from error
    except GenerationPrerequisiteError as error:
        raise HTTPException(409, str(error)) from error
    return JSONResponse(
        {"kind": kind.value, **_job_payload(job)},
        status_code=202,
        headers={"Cache-Control": "no-store"},
    )


@lecture_router.post("/{lecture_id}/outline")
def queue_outline(request: Request, lecture_id: int) -> JSONResponse:
    return _queue_generation(request, lecture_id, GenerationKind.OUTLINE)


@lecture_router.post("/{lecture_id}/quiz")
def queue_quiz(request: Request, lecture_id: int) -> JSONResponse:
    return _queue_generation(request, lecture_id, GenerationKind.QUIZ)


@google_router.get("/status")
def google_status(request: Request) -> JSONResponse:
    return JSONResponse(
        _google_payload(_google(request).status()),
        headers={"Cache-Control": "no-store"},
    )


@google_router.post("/oauth-client")
async def save_google_oauth_client(
    request: Request,
    client_file: Annotated[UploadFile, File()],
) -> JSONResponse:
    payload = await client_file.read(64 * 1024 + 1)
    if len(payload) > 64 * 1024:
        raise HTTPException(413, "OAuth client file is too large")
    try:
        status = _google(request).save_oauth_client(payload)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return JSONResponse(
        {"configured": status.configured},
        headers={"Cache-Control": "no-store"},
    )


@google_router.post("/test")
def test_google(request: Request) -> JSONResponse:
    return JSONResponse(
        _google_payload(_google(request).test()),
        headers={"Cache-Control": "no-store"},
    )


@google_router.post("/connect", status_code=202)
def connect_google(request: Request) -> JSONResponse:
    thread = threading.Thread(
        target=_google(request).start_interactive,
        name="oms-google-connect",
        daemon=True,
    )
    thread.start()
    return JSONResponse(
        {"state": "connecting"},
        status_code=202,
        headers={"Cache-Control": "no-store"},
    )
