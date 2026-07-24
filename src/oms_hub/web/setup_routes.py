import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from keyring.errors import KeyringError

from oms_hub.canvas.repository import CanvasRepository
from oms_hub.panopto.browser_service import PanoptoBrowserService
from oms_hub.panopto.prompt import PromptError, PromptLoader
from oms_hub.panopto.repository import PanoptoRepository
from oms_hub.security.secret_store import SecretStore
from oms_hub.web.canvas_routes import SUBJECT_FIELDS

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter()


def _canvas(request: Request) -> CanvasRepository:
    return cast(CanvasRepository, request.app.state.canvas_repository)


def _panopto(request: Request) -> PanoptoRepository:
    return cast(PanoptoRepository, request.app.state.panopto_repository)


def _prompt(request: Request) -> PromptLoader:
    return cast(PromptLoader, request.app.state.panopto_prompt)


def _browser(request: Request) -> PanoptoBrowserService:
    return cast(PanoptoBrowserService, request.app.state.panopto_browser)


def _secret_present(request: Request, key: str) -> bool:
    store = cast(SecretStore, request.app.state.secrets)
    try:
        return bool(store.get(key))
    except KeyringError:
        return False


def _prompt_status(request: Request) -> tuple[str, str]:
    connection = _panopto(request).connection()
    try:
        current = _prompt(request).inspect()
        state = (
            "approved"
            if current.sha256 == connection.approved_prompt_sha256
            else "changed"
        )
    except PromptError:
        state = "not_readable"
    return state, str(_prompt(request).path)


def _status_snapshot(request: Request) -> dict[str, object]:
    canvas = _canvas(request).connection()
    panopto = _panopto(request).connection()
    latest = _panopto(request).latest_browser_request()
    prompt_state, prompt_path = _prompt_status(request)
    return {
        "canvas": {
            "state": canvas.state,
            "paired": bool(canvas.credential_fingerprint),
            "automatic": canvas.auto_process,
            "last_activity": canvas.last_heartbeat or canvas.last_successful_scan,
            "error": canvas.last_error,
        },
        "panopto": {
            "state": panopto.state,
            "enabled": panopto.enabled,
            "tested": bool(panopto.acceptance_validated_at),
            "last_activity": panopto.last_successful_poll,
            "request_state": latest.state if latest else None,
            "progress": latest.progress if latest else None,
            "error": (latest.error_code if latest else None) or panopto.last_error,
        },
        "openai": {
            "state": (
                "configured"
                if _secret_present(request, "openai-api-key")
                else "not_configured"
            ),
        },
        "prompt": {
            "state": prompt_state,
            "path": prompt_path,
        },
    }


def _likely_icloud_roots() -> list[str]:
    profile = Path(os.path.expandvars(r"%USERPROFILE%"))
    candidates = [
        profile / "iCloudDrive",
        profile / "iCloudDrive" / "iCloud~com~apple~CloudDocs",
    ]
    return [str(item) for item in candidates if item.exists()]


@router.get("/setup", response_class=HTMLResponse)
def setup(request: Request, detail: str | None = None) -> HTMLResponse:
    canvas_repository = _canvas(request)
    canvas_connection = canvas_repository.connection()
    mappings = canvas_repository.list_course_mappings()
    mapped = {item.subject: item.course_id for item in mappings}
    active = {item.subject: item.enabled for item in mappings}
    panopto_connection = _panopto(request).connection()
    prompt_state, prompt_path = _prompt_status(request)
    checks = [
        ("Extension paired", bool(canvas_connection.credential_fingerprint)),
        ("Eight courses mapped", len(mappings) == 8),
        ("Local study folder confirmed", bool(canvas_connection.study_root)),
        (
            "iCloud staging folder confirmed",
            bool(canvas_connection.icloud_staging_root),
        ),
        ("Discovery scan completed", bool(canvas_connection.last_successful_scan)),
        ("Discovery preview confirmed", canvas_connection.discovery_confirmed),
        ("Automatic processing enabled", canvas_connection.auto_process),
    ]
    return templates.TemplateResponse(
        request=request,
        name="setup.html",
        context={
            "detail": detail if detail in {"canvas", "panopto"} else None,
            "snapshot": _status_snapshot(request),
            "canvas_connection": canvas_connection,
            "canvas_checks": checks,
            "subject_fields": SUBJECT_FIELDS,
            "candidates": canvas_repository.list_course_candidates(),
            "mapped": mapped,
            "active": active,
            "icloud_roots": _likely_icloud_roots(),
            "study_default": (
                canvas_connection.study_root or r"%USERPROFILE%\Documents\OMS II"
            ),
            "panopto_connection": panopto_connection,
            "panopto_home": (
                f"{request.app.state.settings.panopto_tenant_url}"
                "/Panopto/Pages/Home.aspx"
            ),
            "openai_credential": _secret_present(request, "openai-api-key"),
            "prompt_path": prompt_path,
            "prompt_state": prompt_state,
        },
    )


@router.post("/setup/panopto/test")
def test_panopto_connection(request: Request) -> dict[str, str]:
    request_id = _browser(request).queue_connection_test(datetime.now(UTC))
    return {"request_id": request_id}


@router.post("/setup/panopto/scan")
def scan_panopto_now(request: Request) -> dict[str, str]:
    request_id = _browser(request).queue_manual_scan(datetime.now(UTC))
    return {"request_id": request_id}


@router.get("/api/setup/status")
def setup_status(request: Request) -> dict[str, object]:
    return _status_snapshot(request)


@router.get("/api/setup/events")
def setup_events(
    request: Request,
    once: bool = Query(default=False),
) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        previous: str | None = None
        while True:
            current = json.dumps(
                _status_snapshot(request),
                separators=(",", ":"),
                sort_keys=True,
            )
            if current != previous:
                yield f"event: status\ndata: {current}\n\n"
                previous = current
            else:
                yield f": heartbeat {uuid.uuid4().hex[:8]}\n\n"
            if once or await request.is_disconnected():
                break
            await asyncio.sleep(2)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
