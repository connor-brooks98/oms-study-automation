from typing import Annotated, Literal, cast
from urllib.parse import urlparse

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from oms_hub.canvas.classifier import classify_attachment
from oms_hub.canvas.domain import CanvasAttachment, DownloadDisposition, ReviewState, SourceKind
from oms_hub.canvas.matcher import match_attachment
from oms_hub.canvas.pairing import PairingService
from oms_hub.canvas.repository import CanvasRepository
from oms_hub.repositories import CatalogRepository

router = APIRouter(prefix="/api/canvas", tags=["canvas-companion"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PairRequest(StrictModel):
    code: str = Field(pattern=r"^\d{6}$")
    extension_id: str = Field(min_length=1, max_length=200)


class PairResponse(StrictModel):
    bearer: str


class HeartbeatRequest(StrictModel):
    state: Literal["connected", "scanning", "canvas_login_required", "error"]
    error: str | None = Field(default=None, max_length=1000)
    scan_complete: bool = False
    item_count: int = Field(default=0, ge=0)
    new_count: int = Field(default=0, ge=0)


class AttachmentRequest(StrictModel):
    course_id: str = Field(max_length=100)
    course_name: str = Field(max_length=300)
    course_code: str = Field(max_length=200)
    module_id: str = Field(max_length=100)
    module_title: str = Field(max_length=500)
    item_id: str = Field(max_length=100)
    item_title: str = Field(max_length=500)
    item_type: str = Field(max_length=50)
    page_url: str = Field(max_length=1024)
    page_title: str = Field(max_length=500)
    file_id: str = Field(max_length=100)
    filename: str = Field(max_length=500)
    content_type: str = Field(max_length=200)
    size: int = Field(ge=0)
    modified_at: str = Field(max_length=100)
    download_url: str = Field(max_length=1024)
    evidence_text: str = Field(default="", max_length=500)

    @field_validator("download_url")
    @classmethod
    def lmu_download_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.hostname != "lmunet.instructure.com":
            raise ValueError("download URL must use the LMU Canvas origin")
        return value

    def domain_value(self) -> CanvasAttachment:
        return CanvasAttachment(**self.model_dump())


class DiscoveryRequest(StrictModel):
    items: list[AttachmentRequest] = Field(max_length=500)


class DiscoveryResponse(StrictModel):
    dispositions: list[DownloadDisposition]


class DownloadCompleteRequest(StrictModel):
    source_item_id: int = Field(gt=0)
    download_id: int = Field(ge=0)
    path: str = Field(min_length=1, max_length=1024)


def _repository(request: Request) -> CanvasRepository:
    return cast(CanvasRepository, request.app.state.canvas_repository)


def _pairing(request: Request) -> PairingService:
    return cast(PairingService, request.app.state.canvas_pairing)


def _authenticate(request: Request, authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Canvas companion bearer required")
    try:
        _pairing(request).verify(authorization.removeprefix("Bearer "))
    except PermissionError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error


def _require_json(request: Request) -> None:
    length = request.headers.get("content-length")
    if length and int(length) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="request body is too large")
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise HTTPException(status_code=415, detail="application/json is required")


@router.post("/pair", response_model=PairResponse)
def pair(value: PairRequest, request: Request) -> PairResponse:
    _require_json(request)
    try:
        bearer = _pairing(request).exchange(value.code, value.extension_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return PairResponse(bearer=bearer)


@router.post("/heartbeat")
def heartbeat(
    value: HeartbeatRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    _require_json(request)
    _authenticate(request, authorization)
    _repository(request).heartbeat(
        value.state,
        value.error,
        scan_complete=value.scan_complete,
        item_count=value.item_count,
        new_count=value.new_count,
    )
    return {"status": "ok"}


@router.get("/config")
def config(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    _authenticate(request, authorization)
    repository = _repository(request)
    connection = repository.connection()
    return {
        "courses": [
            {
                "course_id": item.course_id,
                "course_name": item.course_name,
                "enabled": item.enabled,
            }
            for item in repository.list_course_mappings()
            if item.enabled
        ],
        "scan_minutes": request.app.state.settings.canvas_scan_minutes,
        "auto_process": connection.auto_process,
        "inbox_relative_path": "OMSStudyHub/CanvasInbox",
        "scan_requested": repository.consume_scan_request(),
    }


@router.post("/discover", response_model=DiscoveryResponse)
def discover(
    value: DiscoveryRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> DiscoveryResponse:
    _require_json(request)
    _authenticate(request, authorization)
    repository = _repository(request)
    catalog = CatalogRepository(request.app.state.database)
    mappings = {item.course_id: item for item in repository.list_course_mappings()}
    lectures = catalog.list_lectures()
    dispositions: list[DownloadDisposition] = []
    connection = repository.connection()
    for request_item in value.items:
        item = request_item.domain_value()
        mapping = mappings.get(item.course_id)
        if mapping is None or not mapping.enabled:
            raise HTTPException(status_code=422, detail="Canvas course is not mapped")
        classification = classify_attachment(item)
        match = match_attachment(item, mapping.subject, lectures)
        stored = repository.ingest_metadata(item, classification, match)
        if classification.kind is SourceKind.IGNORE:
            action, reason, filename = "skip", classification.reason, None
        elif stored.review_state is ReviewState.NEEDS_REVIEW:
            action, reason, filename = "review", match.reason, None
        elif not connection.auto_process:
            action, reason, filename = "review", "Discovery-only mode is enabled", None
        elif not stored.created:
            action, reason, filename = "skip", "source revision already recorded", None
        else:
            safe_name = item.filename.replace("/", "-").replace("\\", "-")
            action = "download"
            reason = "new high-confidence Canvas source"
            filename = f"{stored.source_item_id}/{stored.revision_id}/{safe_name}"
        dispositions.append(
            DownloadDisposition(stored.source_item_id, action, reason, filename)
        )
    return DiscoveryResponse(dispositions=dispositions)


@router.post("/download-complete", status_code=501)
def download_complete(
    value: DownloadCompleteRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    _require_json(request)
    _authenticate(request, authorization)
    return {"status": "ingestion is not installed yet"}
