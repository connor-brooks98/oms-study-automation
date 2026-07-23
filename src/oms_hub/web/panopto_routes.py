from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from oms_hub.panopto.pipeline import TranscriptPipeline, validate_raw_caption
from oms_hub.panopto.prompt import PromptError, PromptLoader
from oms_hub.panopto.repository import PanoptoRepository
from oms_hub.repositories import CatalogRepository
from oms_hub.security.secret_store import SecretStore

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(prefix="/panopto")


def _repository(request: Request) -> PanoptoRepository:
    return cast(PanoptoRepository, request.app.state.panopto_repository)


def _prompt(request: Request) -> PromptLoader:
    return cast(PromptLoader, request.app.state.panopto_prompt)


def _pipeline(request: Request) -> TranscriptPipeline:
    return cast(TranscriptPipeline, request.app.state.panopto_pipeline)


def _secrets(request: Request) -> SecretStore:
    return cast(SecretStore, request.app.state.secrets)


def _secret_present(request: Request, key: str) -> bool:
    try:
        return bool(_secrets(request).get(key))
    except Exception:
        return False


@router.get("/setup", response_class=HTMLResponse)
def setup(request: Request) -> HTMLResponse:
    connection = _repository(request).connection()
    try:
        current_prompt = _prompt(request).inspect()
        prompt_state = "Approved" if (
            current_prompt.sha256 == connection.approved_prompt_sha256
        ) else "Changed or not approved"
        current_hash = current_prompt.sha256
    except PromptError:
        prompt_state = "Not readable"
        current_hash = None
    return templates.TemplateResponse(
        request=request,
        name="panopto_setup.html",
        context={
            "connection": connection,
            "client_id_configured": bool(
                request.app.state.settings.panopto_client_id
            ),
            "panopto_credential": _secret_present(
                request, "panopto-client-secret"
            ),
            "openai_credential": _secret_present(request, "openai-api-key"),
            "prompt_path": _prompt(request).path,
            "prompt_state": prompt_state,
            "current_hash": current_hash,
        },
    )


@router.post("/prompt/initialize")
def initialize_prompt(request: Request) -> RedirectResponse:
    try:
        _prompt(request).initialize()
    except OSError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return RedirectResponse("/panopto/setup", status_code=303)


@router.post("/prompt/approve")
def approve_prompt(request: Request) -> RedirectResponse:
    try:
        prompt = _prompt(request).inspect()
    except PromptError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    _repository(request).approve_prompt(prompt.sha256, str(_prompt(request).path))
    _prompt(request).approved_sha256 = prompt.sha256
    return RedirectResponse("/panopto/setup", status_code=303)


@router.post("/acceptance/validate")
def validate_acceptance(request: Request) -> RedirectResponse:
    settings = request.app.state.settings
    try:
        session = request.app.state.panopto_client.get_session(
            settings.panopto_acceptance_session_id
        )
        if session.content_language != "English_USA" or not session.caption_download_url:
            raise ValueError("Acceptance session has no English (United States) captions")
        payload = request.app.state.panopto_client.download_captions(
            session.caption_download_url,
            settings.panopto_max_caption_bytes,
        )
        validate_raw_caption(payload, settings.panopto_max_caption_bytes)
    except Exception as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    _repository(request).mark_acceptance_validated(datetime.now(UTC))
    return RedirectResponse("/panopto/setup", status_code=303)


@router.post("/enable")
def enable(request: Request) -> RedirectResponse:
    connection = _repository(request).connection()
    try:
        current_prompt = _prompt(request).current()
    except PromptError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    ready = (
        bool(request.app.state.settings.panopto_client_id)
        and _secret_present(request, "panopto-client-secret")
        and _secret_present(request, "openai-api-key")
        and bool(connection.acceptance_validated_at)
        and current_prompt.sha256 == connection.approved_prompt_sha256
    )
    if not ready:
        raise HTTPException(status_code=409, detail="Complete every Panopto setup step first")
    _repository(request).set_enabled(True)
    return RedirectResponse("/panopto/setup", status_code=303)


@router.post("/pause")
def pause(request: Request) -> RedirectResponse:
    _repository(request).set_enabled(False)
    return RedirectResponse("/panopto/setup", status_code=303)


@router.post("/scan")
def scan(request: Request) -> RedirectResponse:
    try:
        request.app.state.panopto_discovery.poll(datetime.now(UTC))
    except Exception as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return RedirectResponse("/panopto/setup", status_code=303)


@router.get("/review", response_class=HTMLResponse)
def review(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="panopto_review.html",
        context={
            "recordings": _repository(request).list_review_recordings(),
            "jobs": _repository(request).list_review_jobs(),
            "lectures": CatalogRepository(request.app.state.database).list_lectures(),
        },
    )


@router.post("/review/{recording_id}/remap")
def remap(
    recording_id: int,
    request: Request,
    lecture_id: int = Form(),
) -> RedirectResponse:
    try:
        _repository(request).remap_recording(recording_id, lecture_id)
    except KeyError as error:
        raise HTTPException(
            status_code=409,
            detail="Recording or lecture was not found",
        ) from error
    return RedirectResponse("/panopto/review", status_code=303)

@router.post("/jobs/{job_id}/retry")
def retry(job_id: int, request: Request) -> RedirectResponse:
    try:
        _pipeline(request).retry_job(job_id)
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return RedirectResponse("/panopto/review", status_code=303)
