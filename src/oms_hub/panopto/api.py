from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from oms_hub.canvas.pairing import PairingService
from oms_hub.panopto.browser_domain import (
    BrowserCommandKind,
    BrowserRecording,
    TranscriptExtraction,
)
from oms_hub.panopto.browser_service import (
    PanoptoBrowserService,
    validate_viewer_url,
)
from oms_hub.panopto.pipeline import TranscriptValidationError, validate_raw_caption
from oms_hub.panopto.repository import PanoptoRepository

router = APIRouter(prefix="/api/panopto", tags=["panopto-companion"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HeartbeatRequest(StrictModel):
    state: Literal[
        "panopto_login_required",
        "connected",
        "scanning",
        "waiting_for_transcript",
        "needs_review",
        "error",
    ]
    error: str | None = Field(default=None, max_length=1000)


class RecordingRequest(StrictModel):
    session_id: UUID
    name: str = Field(min_length=1, max_length=500)
    created_utc: datetime
    duration_seconds: float = Field(ge=0, le=24 * 60 * 60)
    folder_name: str = Field(default="", max_length=300)
    viewer_url: str = Field(max_length=1024)

    @field_validator("viewer_url")
    @classmethod
    def lmu_viewer_url(cls, value: str, info: ValidationInfo) -> str:
        session_id = info.data.get("session_id")
        if session_id is not None:
            validate_viewer_url(value, str(session_id))
        return value

    def domain_value(self) -> BrowserRecording:
        return BrowserRecording(
            str(self.session_id),
            self.name,
            self.created_utc,
            self.duration_seconds,
            self.folder_name,
            self.viewer_url,
        )


class DiscoveryRequest(StrictModel):
    command_id: UUID
    recordings: list[RecordingRequest] = Field(max_length=100)


class TranscriptRequest(StrictModel):
    command_id: UUID
    recording_id: int = Field(gt=0)
    session_id: UUID
    viewer_url: str = Field(max_length=1024)
    language: str = Field(max_length=60)
    line_count: int = Field(gt=0, le=100_000)
    complete: bool
    text: str

    @field_validator("viewer_url")
    @classmethod
    def lmu_viewer_url(cls, value: str, info: ValidationInfo) -> str:
        session_id = info.data.get("session_id")
        if session_id is not None:
            validate_viewer_url(value, str(session_id))
        return value


class ResultRequest(StrictModel):
    command_id: UUID
    status: Literal["complete", "failed"]
    reason_code: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,80}$")


class AcceptanceRequest(StrictModel):
    command_id: UUID
    session_id: UUID
    viewer_url: str = Field(max_length=1024)
    language: str = Field(max_length=60)
    line_count: int = Field(gt=0, le=100_000)
    complete: bool
    text: str

    @field_validator("viewer_url")
    @classmethod
    def lmu_viewer_url(cls, value: str, info: ValidationInfo) -> str:
        session_id = info.data.get("session_id")
        if session_id is not None:
            validate_viewer_url(value, str(session_id))
        return value


def _authenticate(request: Request, authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="OMS companion bearer required")
    pairing = cast(PairingService, request.app.state.canvas_pairing)
    try:
        pairing.verify(authorization.removeprefix("Bearer "))
    except PermissionError as error:
        raise HTTPException(status_code=401, detail="Invalid OMS companion bearer") from error


def _repository(request: Request) -> PanoptoRepository:
    return cast(PanoptoRepository, request.app.state.panopto_repository)


def _service(request: Request) -> PanoptoBrowserService:
    return cast(PanoptoBrowserService, request.app.state.panopto_browser)


def _require_running_command(request: Request, command_id: UUID) -> None:
    if _repository(request).get_running_browser_command(str(command_id)) is None:
        raise HTTPException(
            status_code=409,
            detail="Panopto browser command is not running",
        )


@router.post("/heartbeat")
def heartbeat(
    value: HeartbeatRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    _authenticate(request, authorization)
    _repository(request).heartbeat(
        value.state,
        datetime.now(UTC),
        value.error,
    )
    return {"status": "ok"}


@router.get("/command", response_model=None)
def command(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Response | dict[str, object]:
    _authenticate(request, authorization)
    now = datetime.now(UTC)
    repository = _repository(request)
    repository.recover_stale_browser_commands(now)
    claimed = repository.claim_browser_command(now)
    if claimed is None:
        return Response(status_code=204)
    return {
        "id": claimed.id,
        "kind": claimed.kind.value,
        "payload": claimed.payload,
    }


@router.post("/discover")
def discover(
    value: DiscoveryRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    _authenticate(request, authorization)
    _require_running_command(request, value.command_id)
    dispositions = _service(request).process_discovery(
        str(value.command_id),
        [item.domain_value() for item in value.recordings],
        datetime.now(UTC),
    )
    return {
        "dispositions": [
            {
                "recording_id": item.recording_id,
                "session_id": item.session_id,
                "action": item.action,
                "viewer_url": item.viewer_url,
                "reason": item.reason,
            }
            for item in dispositions
        ]
    }


@router.post("/transcript")
def transcript(
    value: TranscriptRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, int]:
    _authenticate(request, authorization)
    if len(value.text.encode("utf-8")) > request.app.state.settings.panopto_max_caption_bytes:
        raise HTTPException(status_code=413, detail="Transcript payload is too large")
    _require_running_command(request, value.command_id)
    try:
        revision_id = _service(request).ingest_extraction(
            TranscriptExtraction(
                str(value.command_id),
                value.recording_id,
                str(value.session_id),
                value.viewer_url,
                value.language,
                value.line_count,
                value.complete,
                value.text,
            )
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"revision_id": revision_id}


@router.post("/acceptance")
def acceptance(
    value: AcceptanceRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    _authenticate(request, authorization)
    if len(value.text.encode("utf-8")) > request.app.state.settings.panopto_max_caption_bytes:
        raise HTTPException(status_code=413, detail="Transcript payload is too large")
    command = _repository(request).get_running_browser_command(str(value.command_id))
    expected = {
        "session_id": str(value.session_id),
        "viewer_url": value.viewer_url,
    }
    if (
        command is None
        or command.kind is not BrowserCommandKind.ACCEPTANCE
        or command.payload != expected
    ):
        raise HTTPException(status_code=409, detail="Acceptance command does not match")
    if not value.complete or value.language != "English_USA":
        raise HTTPException(status_code=409, detail="English transcript is not complete")
    try:
        validate_raw_caption(
            value.text.encode("utf-8"),
            request.app.state.settings.panopto_max_caption_bytes,
        )
    except TranscriptValidationError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    _repository(request).mark_acceptance_validated(datetime.now(UTC))
    return {"status": "validated"}


@router.post("/result")
def result(
    value: ResultRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    _authenticate(request, authorization)
    now = datetime.now(UTC)
    repository = _repository(request)
    if value.status == "complete":
        repository.complete_browser_command(str(value.command_id), now)
        repository.heartbeat("connected", now)
    else:
        reason = value.reason_code or "browser_command_failed"
        repository.fail_browser_command(str(value.command_id), now, reason)
        state = (
            "panopto_login_required"
            if reason == "panopto_login_required"
            else "error"
        )
        repository.heartbeat(state, now, reason)
    return {"status": "ok"}
