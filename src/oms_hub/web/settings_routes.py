import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, cast
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from oms_hub.llm.domain import (
    DiagnosticSource,
    LLMRequestError,
    ProviderName,
)
from oms_hub.llm.repository import LLMSettingsRepository
from oms_hub.llm.service import SECRET_KEYS, LLMService
from oms_hub.repositories import CatalogRepository
from oms_hub.security.secret_store import SecretStore
from oms_hub.study_generation.domain import PromptKind
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.tracker_preview import TrackerPreview, TrackerPreviewService
from oms_hub.web.llm_schemas import (
    ActiveProviderUpdate,
    CredentialUpdate,
    ModelUpdate,
)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(prefix="/settings")
logger = logging.getLogger(__name__)

_MAX_TRACKER_BYTES = 25 * 1024 * 1024


def _service(request: Request) -> TrackerPreviewService:
    return TrackerPreviewService(
        CatalogRepository(request.app.state.database),
        request.app.state.settings.data_dir / "tracker-previews",
    )


def _preview_courses(preview: TrackerPreview) -> tuple[dict[str, object], ...]:
    grouped: dict[str, dict[int, list[object]]] = {}
    for lecture in preview.detected:
        grouped.setdefault(lecture.subject, {}).setdefault(
            lecture.exam_number,
            [],
        ).append(lecture)
    return tuple(
        {
            "name": subject,
            "lecture_count": sum(len(lectures) for lectures in exams.values()),
            "exams": tuple(
                {
                    "number": exam_number,
                    "lectures": tuple(exams[exam_number]),
                }
                for exam_number in sorted(exams)
            ),
        }
        for subject, exams in sorted(grouped.items())
    )


@router.get("", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    labels = {
        ProviderName.OPENAI: "OpenAI",
        ProviderName.GEMINI: "Google Gemini",
        ProviderName.ANTHROPIC: "Anthropic Claude",
    }
    preferences = _llm_settings(request).list()
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "providers": tuple(
                {
                    "name": preference.provider.value,
                    "label": labels[preference.provider],
                    "model": preference.model,
                    "active": preference.active,
                    "configured": _llm_service(
                        request
                    ).credential_configured(preference.provider),
                    "last_test_state": preference.last_test_state,
                    "last_tested_at": preference.last_tested_at,
                    "diagnostic_source": preference.diagnostic_source,
                    "diagnostic_message": preference.diagnostic_message,
                    "http_status": preference.http_status,
                    "provider_request_id": (
                        preference.provider_request_id
                    ),
                }
                for preference in preferences
            ),
            "active_provider": _llm_settings(
                request
            ).active().provider.value,
            "prompt_settings": tuple(
                {
                    "kind": kind.value,
                    "label": (
                        "Lecture outline prompt"
                        if kind is PromptKind.OUTLINE
                        else "Lecture quiz prompt"
                    ),
                    "path": GenerationRepository(
                        request.app.state.database
                    ).prompt_path(kind)
                    or "",
                }
                for kind in PromptKind
            ),
        },
    )


@router.post("/ai/{provider}/credential")
def save_ai_credential(
    request: Request,
    provider: str,
    update: CredentialUpdate,
) -> JSONResponse:
    selected = _provider(provider)
    secrets = cast(SecretStore, request.app.state.secrets)
    if update.credential.strip():
        secrets.set(SECRET_KEYS[selected], update.credential)
        _llm_settings(request).clear_test(selected)
    return _no_store(
        {
            "provider": selected.value,
            "configured": _llm_service(request).credential_configured(
                selected
            ),
        }
    )


@router.post("/ai/{provider}/model")
def save_ai_model(
    request: Request,
    provider: str,
    update: ModelUpdate,
) -> JSONResponse:
    selected = _provider(provider)
    try:
        preference = _llm_settings(request).set_model(
            selected,
            update.model,
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    _llm_settings(request).clear_test(selected)
    return _no_store(
        {
            "provider": selected.value,
            "model": preference.model,
        }
    )


@router.post("/ai/active")
def save_active_ai_provider(
    request: Request,
    update: ActiveProviderUpdate,
) -> JSONResponse:
    if not _llm_service(request).credential_configured(update.provider):
        raise HTTPException(
            409,
            f"Configure the {update.provider.value.title()} credential first",
        )
    preference = _llm_settings(request).set_active(update.provider)
    return _no_store(
        {
            "provider": preference.provider.value,
            "model": preference.model,
        }
    )


@router.post("/ai/{provider}/test")
def test_ai_connection(
    request: Request,
    provider: str,
) -> JSONResponse:
    selected = _provider(provider)
    correlation_id = str(uuid4())
    tested_at = datetime.now(UTC).isoformat()
    preference = _llm_settings(request).get(selected)
    try:
        connection = _llm_service(request).test_connection(selected)
    except LLMRequestError as error:
        message = str(error)
        _llm_settings(request).record_test(
            selected,
            state="failed",
            tested_at=tested_at,
            diagnostic_source=error.source.value,
            diagnostic_message=message,
            http_status=error.http_status,
            provider_request_id=error.provider_request_id,
        )
        logger.info(
            "LLM connection test failed correlation=%s provider=%s "
            "model=%s source=%s status=%s request_id=%s",
            correlation_id,
            selected.value,
            preference.model,
            error.source.value,
            error.http_status,
            error.provider_request_id,
        )
        return _no_store(
            {
                "provider": selected.value,
                "state": "failed",
                "tested_at": tested_at,
                "correlation_id": correlation_id,
                "provider_request_id": error.provider_request_id,
                "diagnostic": _diagnostic(
                    error.source,
                    message,
                    error.http_status,
                ),
            }
        )
    except Exception as error:  # noqa: BLE001 - HTTP boundary is sanitized
        message = "Study Hub could not complete the connection test"
        _llm_settings(request).record_test(
            selected,
            state="failed",
            tested_at=tested_at,
            diagnostic_source=DiagnosticSource.STUDY_HUB.value,
            diagnostic_message=message,
        )
        logger.error(
            "LLM connection test internal failure correlation=%s provider=%s "
            "model=%s exception_type=%s",
            correlation_id,
            selected.value,
            preference.model,
            type(error).__name__,
        )
        return _no_store(
            {
                "provider": selected.value,
                "state": "failed",
                "tested_at": tested_at,
                "correlation_id": correlation_id,
                "provider_request_id": None,
                "diagnostic": _diagnostic(
                    DiagnosticSource.STUDY_HUB,
                    message,
                    None,
                ),
            }
        )
    _llm_settings(request).record_test(
        selected,
        state="connected",
        tested_at=tested_at,
        provider_request_id=connection.request_id,
    )
    logger.info(
        "LLM connection test succeeded correlation=%s provider=%s "
        "model=%s request_id=%s",
        correlation_id,
        selected.value,
        connection.model,
        connection.request_id,
    )
    return _no_store(
        {
            "provider": selected.value,
            "state": "connected",
            "tested_at": tested_at,
            "correlation_id": correlation_id,
            "provider_request_id": connection.request_id,
            "diagnostic": None,
        }
    )


@router.post("/tracker/preview")
async def preview_tracker(
    request: Request,
    workbook: Annotated[UploadFile, File()],
) -> Response:
    filename = Path(workbook.filename or "").name
    if Path(filename).suffix.casefold() != ".xlsx":
        raise HTTPException(415, "tracker must be an .xlsx workbook")
    incoming_root = request.app.state.settings.data_dir / "tracker-previews" / "incoming"
    incoming_root.mkdir(parents=True, exist_ok=True)
    incoming = incoming_root / f"{uuid4()}.xlsx"
    size = 0
    try:
        with incoming.open("xb") as output:
            while chunk := await workbook.read(1024 * 1024):
                size += len(chunk)
                if size > _MAX_TRACKER_BYTES:
                    raise HTTPException(413, "tracker workbook is too large")
                output.write(chunk)
        preview = _service(request).preview(incoming, source_name=filename)
        if "application/json" in request.headers.get("accept", "").casefold():
            return JSONResponse(preview.public_dict())
        courses = _preview_courses(preview)
        return templates.TemplateResponse(
            request=request,
            name="tracker_preview.html",
            context={
                "preview": preview,
                "courses": courses,
                "course_count": len(courses),
                "exam_count": len(
                    {
                        (lecture.subject, lecture.exam_number)
                        for lecture in preview.detected
                    }
                ),
            },
        )
    finally:
        incoming.unlink(missing_ok=True)


@router.post("/tracker/apply")
def apply_tracker(
    request: Request,
    preview_id: Annotated[str, Form()],
) -> RedirectResponse:
    try:
        _service(request).apply(preview_id)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as error:
        raise HTTPException(409, str(error)) from error
    return RedirectResponse("/", status_code=303)


def _provider(value: str) -> ProviderName:
    try:
        return ProviderName(value)
    except ValueError as error:
        raise HTTPException(404, "AI provider was not found") from error


def _llm_service(request: Request) -> LLMService:
    return cast(LLMService, request.app.state.llm_service)


def _llm_settings(request: Request) -> LLMSettingsRepository:
    return cast(
        LLMSettingsRepository,
        request.app.state.llm_settings,
    )


def _no_store(payload: dict[str, object]) -> JSONResponse:
    return JSONResponse(
        payload,
        headers={"Cache-Control": "no-store"},
    )


def _diagnostic(
    source: DiagnosticSource,
    message: str,
    http_status: int | None,
) -> dict[str, object]:
    next_actions = {
        DiagnosticSource.STUDY_HUB: (
            "Check the Study Hub service log using the correlation ID."
        ),
        DiagnosticSource.NETWORK: (
            "Check the NUC internet, DNS, firewall, and system clock."
        ),
        DiagnosticSource.AUTHENTICATION: (
            "Replace this provider credential and test again."
        ),
        DiagnosticSource.MODEL: (
            "Choose a model available to this provider account."
        ),
        DiagnosticSource.QUOTA: (
            "Check provider billing, quota, and rate limits."
        ),
        DiagnosticSource.SERVICE: (
            "Check the provider status page and try again."
        ),
    }
    return {
        "source": source.value,
        "message": message,
        "http_status": http_status,
        "next_action": next_actions[source],
    }
