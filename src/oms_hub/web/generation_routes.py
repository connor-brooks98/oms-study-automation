import threading
from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from oms_hub.anki.prompt_catalog import AnkiPromptCatalogService
from oms_hub.routing import expanded_path
from oms_hub.study_generation.domain import (
    GenerationJob,
    GenerationKind,
    PromptKind,
)
from oms_hub.study_generation.notebook_connection import (
    NotebookConnectionService,
    NotebookConnectionStatus,
)
from oms_hub.study_generation.path_picker import PromptDirectoryPicker, PromptPathPicker
from oms_hub.study_generation.prompts import PromptConfigurationError, PromptFileService
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.study_generation.service import (
    GenerationPrerequisiteError,
    GenerationService,
)
from oms_hub.transcripts.prompt import PromptError as TranscriptPromptError
from oms_hub.transcripts.prompt import PromptLoader as TranscriptPromptLoader

router = APIRouter(prefix="/settings/generation")
anki_prompt_router = APIRouter(prefix="/settings/anki/prompts")


class PromptPathUpdate(BaseModel):
    path: Annotated[str, Field(min_length=1, max_length=2048)]


@anki_prompt_router.post("/directory")
def save_anki_prompt_directory(
    request: Request,
    update: PromptPathUpdate,
) -> JSONResponse:
    try:
        _repository(request).set_anki_prompt_directory(update.path)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return JSONResponse(
        {"path": update.path.strip()},
        headers={"Cache-Control": "no-store"},
    )


@anki_prompt_router.post("/directory/select")
def select_anki_prompt_directory(request: Request) -> JSONResponse:
    picker = cast(PromptDirectoryPicker, request.app.state.prompt_directory_picker)
    try:
        selected = picker.select_directory()
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    return JSONResponse(
        {
            "path": str(selected) if selected is not None else None,
            "selected": selected is not None,
        },
        headers={"Cache-Control": "no-store"},
    )


@anki_prompt_router.post("/directory/test")
def test_anki_prompt_directory(request: Request) -> JSONResponse:
    catalog = cast(
        AnkiPromptCatalogService,
        request.app.state.anki_prompt_catalog,
    ).catalog()
    payload = catalog.payload()
    payload["state"] = "valid" if catalog.ready else "invalid"
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


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


def _notebook(request: Request) -> NotebookConnectionService:
    return cast(
        NotebookConnectionService,
        request.app.state.notebook_connection,
    )


def _notebook_payload(
    status: NotebookConnectionStatus,
) -> dict[str, object]:
    return {
        "state": status.state,
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
    if selected is PromptKind.TRANSCRIPT:
        active_prompt = cast(
            TranscriptPromptLoader,
            request.app.state.transcript_prompt,
        )
        active_prompt.path = expanded_path(Path(update.path.strip()))
    return JSONResponse(
        {"kind": selected.value, "path": update.path.strip()},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/prompts/{kind}/test")
def test_prompt_path(request: Request, kind: str) -> JSONResponse:
    selected = _kind(kind)
    if selected is PromptKind.TRANSCRIPT:
        configured = _repository(request).prompt_path(selected)
        try:
            transcript_prompt = TranscriptPromptLoader(
                expanded_path(Path(configured)) if configured else None,
                None,
            ).inspect()
        except TranscriptPromptError as error:
            return JSONResponse(
                {
                    "kind": selected.value,
                    "state": "invalid",
                    "message": str(error),
                },
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {
                "kind": selected.value,
                "state": "valid",
                "path": configured,
                "sha256": transcript_prompt.sha256,
                "modified_at": None,
            },
            headers={"Cache-Control": "no-store"},
        )
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


@router.post("/prompts/{kind}/select")
def select_prompt_path(request: Request, kind: str) -> JSONResponse:
    selected_kind = _kind(kind)
    picker = cast(PromptPathPicker, request.app.state.prompt_path_picker)
    try:
        selected_path = picker.select()
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    return JSONResponse(
        {
            "kind": selected_kind.value,
            "path": str(selected_path) if selected_path is not None else None,
            "selected": selected_path is not None,
        },
        headers={"Cache-Control": "no-store"},
    )


notebook_router = APIRouter(prefix="/settings/notebook")
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


@notebook_router.get("/status")
def notebook_status(request: Request) -> JSONResponse:
    return JSONResponse(
        _notebook_payload(_notebook(request).status()),
        headers={"Cache-Control": "no-store"},
    )


@notebook_router.post("/test")
def test_notebook(request: Request) -> JSONResponse:
    return JSONResponse(
        _notebook_payload(_notebook(request).test()),
        headers={"Cache-Control": "no-store"},
    )


@notebook_router.post("/connect", status_code=202)
def connect_notebook(request: Request) -> JSONResponse:
    thread = threading.Thread(
        target=_notebook(request).start_interactive,
        name="oms-notebook-connect",
        daemon=True,
    )
    thread.start()
    connecting = NotebookConnectionStatus(
        "connecting",
        "Complete Google sign-in in the browser window.",
    )
    return JSONResponse(
        _notebook_payload(connecting),
        status_code=202,
        headers={"Cache-Control": "no-store"},
    )
