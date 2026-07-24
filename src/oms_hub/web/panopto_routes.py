from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from keyring.errors import KeyringError

from oms_hub.panopto.browser_service import PanoptoBrowserService
from oms_hub.panopto.pipeline import TranscriptPipeline
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


def _service(request: Request) -> PanoptoBrowserService:
    return cast(PanoptoBrowserService, request.app.state.panopto_browser)


def _secrets(request: Request) -> SecretStore:
    return cast(SecretStore, request.app.state.secrets)


def _secret_present(request: Request, key: str) -> bool:
    try:
        return bool(_secrets(request).get(key))
    except KeyringError:
        return False


@router.get("/setup", response_class=HTMLResponse)
def setup(request: Request) -> RedirectResponse:
    del request
    return RedirectResponse("/setup?detail=panopto", status_code=307)


@router.post("/browser/check")
def check_browser(request: Request) -> RedirectResponse:
    _service(request).queue_connection_check(datetime.now(UTC))
    return RedirectResponse("/setup?detail=panopto", status_code=303)


@router.post("/browser/acceptance")
def acceptance(request: Request) -> RedirectResponse:
    _service(request).queue_connection_test(datetime.now(UTC))
    return RedirectResponse("/setup?detail=panopto", status_code=303)


@router.post("/prompt/initialize")
def initialize_prompt(request: Request) -> RedirectResponse:
    try:
        _prompt(request).initialize()
    except OSError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return RedirectResponse("/setup?detail=panopto", status_code=303)


@router.post("/prompt/approve")
def approve_prompt(request: Request) -> RedirectResponse:
    try:
        prompt = _prompt(request).inspect()
    except PromptError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    _repository(request).approve_prompt(prompt.sha256, str(_prompt(request).path))
    _prompt(request).approved_sha256 = prompt.sha256
    return RedirectResponse("/setup?detail=panopto", status_code=303)


@router.post("/enable")
def enable(request: Request) -> RedirectResponse:
    connection = _repository(request).connection()
    try:
        prompt = _prompt(request).inspect()
    except PromptError:
        prompt = None
    ready = (
        connection.state == "connected"
        and _secret_present(request, "openai-api-key")
        and bool(connection.acceptance_validated_at)
        and prompt is not None
        and prompt.sha256 == connection.approved_prompt_sha256
    )
    if not ready:
        raise HTTPException(
            status_code=409,
            detail="Complete every Panopto setup step first",
        )
    _repository(request).set_enabled(True)
    return RedirectResponse("/setup?detail=panopto", status_code=303)


@router.post("/pause")
def pause(request: Request) -> RedirectResponse:
    _repository(request).set_enabled(False)
    return RedirectResponse("/setup?detail=panopto", status_code=303)


@router.post("/scan")
def scan(request: Request) -> RedirectResponse:
    _service(request).queue_manual_scan(datetime.now(UTC))
    return RedirectResponse("/setup?detail=panopto", status_code=303)


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
    _service(request).queue_manual_scan(datetime.now(UTC))
    return RedirectResponse("/panopto/review", status_code=303)


@router.post("/jobs/{job_id}/retry")
def retry(job_id: int, request: Request) -> RedirectResponse:
    try:
        _pipeline(request).retry_job(job_id)
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return RedirectResponse("/panopto/review", status_code=303)
