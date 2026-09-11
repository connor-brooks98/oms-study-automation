import threading
from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from oms_hub.anki.prompt_catalog import AnkiPromptCatalogService
from oms_hub.llm.codex_session import CodexSessionClient
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


class ManagedModelUpdate(BaseModel):
    model: str = Field(min_length=1, max_length=200)


class ManagedLoginCancel(BaseModel):
    login_id: str = Field(min_length=1, max_length=200)


class LectureObjective(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=10000)


class GptLectureQueue(BaseModel):
    label: str = Field(min_length=1, max_length=300)
    objectives: list[LectureObjective] = Field(min_length=1, max_length=500)
    require_images: bool = True


def _private_owner(request: Request, *, mutation: bool = True) -> str:
    from oms_hub.web.csrf import require_form_csrf

    owner = getattr(request.state, "study_owner_id", None)
    if not isinstance(owner, str) or not owner:
        raise HTTPException(403, "private study owner required")
    if mutation:
        require_form_csrf(request, None)
    return owner


def _codex(request: Request) -> CodexSessionClient:
    client = getattr(request.app.state, "codex_session", None)
    if client is None:
        raise HTTPException(409, "Configure the managed Codex executable and verified pin first.")
    return cast(CodexSessionClient, client)


@router.post("/codex/login")
def codex_login(request: Request) -> JSONResponse:
    from dataclasses import asdict

    from oms_hub.llm.codex_session import SessionError

    _private_owner(request)
    try:
        challenge = _codex(request).start_login()
    except SessionError as error:
        raise HTTPException(409, str(error)) from error
    return JSONResponse(asdict(challenge), headers={"Cache-Control": "no-store"})


@router.post("/codex/cancel")
def codex_cancel(request: Request, body: ManagedLoginCancel) -> JSONResponse:
    from oms_hub.llm.codex_session import SessionError

    _private_owner(request)
    try:
        _codex(request).cancel_login(body.login_id)
    except SessionError as error:
        raise HTTPException(409, str(error)) from error
    return JSONResponse({"cancelled": True}, headers={"Cache-Control": "no-store"})


@router.post("/codex/status")
def codex_status(request: Request) -> JSONResponse:
    from dataclasses import asdict

    _private_owner(request)
    status = _codex(request).status()
    return JSONResponse(asdict(status) | {"selected_model": request.app.state.codex_model},
        headers={"Cache-Control": "no-store"})


@router.post("/codex/model")
def codex_model(request: Request, body: ManagedModelUpdate) -> JSONResponse:
    _private_owner(request)
    status = _codex(request).status()
    if not status.account_connected or body.model not in status.model_ids:
        raise HTTPException(409, "Choose a model advertised by the connected managed account.")
    request.app.state.study_ai_settings.save(codex_model=body.model)
    request.app.state.codex_model = body.model
    for name in ("gpt_lecture_service", "gpt_transcript_cleaner", "study_chat_service"):
        service = getattr(request.app.state, name, None)
        if service is not None:
            service.model = body.model
    return JSONResponse({"model": body.model}, headers={"Cache-Control": "no-store"})


@lecture_router.post("/{lecture_id}/gpt-quiz")
def queue_gpt_quiz(request: Request, lecture_id: int, body: GptLectureQueue) -> JSONResponse:
    from sqlalchemy.exc import IntegrityError

    owner = _private_owner(request)
    _codex(request)
    if getattr(request.app.state, "gpt_lecture_worker", None) is None:
        raise HTTPException(409, "The GPT lecture worker is not configured.")
    try:
        run = request.app.state.gpt_lecture_service.queue(lecture_id, owner_id=owner,
            label=body.label, objectives=tuple((o.id, o.text) for o in body.objectives),
            require_images=body.require_images)
    except KeyError as error:
        raise HTTPException(404, "lecture was not found") from error
    except (ValueError, GenerationPrerequisiteError, IntegrityError) as error:
        message = ("This quiz label is already queued."
            if isinstance(error, IntegrityError) else str(error))
        raise HTTPException(409, message) from error
    return JSONResponse({"run_id": run.id, "state": run.state.value,
        "review_url": f"/lectures/gpt-runs/{run.id}"}, status_code=202,
        headers={"Cache-Control": "no-store"})


def _gpt_run_payload(request: Request, run_id: str) -> dict[str, object]:
    import json

    owner = _private_owner(request, mutation=False)
    repository = request.app.state.studio_repository
    settings = repository.run_artifact(run_id, "gpt:settings")
    if settings is None or json.loads(settings.payload_json).get("owner_id") != owner:
        raise HTTPException(404, "GPT quiz run was not found")
    run = repository.get_run(run_id)
    return {"run_id": run.id, "state": run.state.value, "error": run.error,
        "diagnostic_source": run.diagnostic_source,
        "review_url": f"/studio/runs/{run_id}/review"}


@router.get("/codex/runs/{run_id}")
def gpt_run_status(request: Request, run_id: str) -> JSONResponse:
    return JSONResponse(_gpt_run_payload(request, run_id), headers={"Cache-Control": "no-store"})


@router.post("/codex/runs/{run_id}/{action}")
def gpt_run_control(request: Request, run_id: str, action: str) -> JSONResponse:
    owner = _private_owner(request)
    try:
        request.app.state.studio_repository.control_gpt_run(run_id, owner_id=owner, action=action)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return gpt_run_status(request, run_id)


@lecture_router.get("/gpt-runs/{run_id}")
def gpt_run_page(request: Request, run_id: str) -> HTMLResponse:
    from oms_hub.web.studio_routes import templates

    _gpt_run_payload(request, run_id)
    return templates.TemplateResponse(request=request, name="gpt_run.html",
        context={"run_id": run_id}, headers={"Cache-Control": "no-store"})
