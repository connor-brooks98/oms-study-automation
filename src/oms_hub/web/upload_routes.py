import asyncio
from io import BytesIO
from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from oms_hub.ingestion.domain import UploadKind, UploadState
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.ingestion.service import IngestionService
from oms_hub.ingestion.staging import StagingService, UploadRejected
from oms_hub.repositories import CatalogRepository
from oms_hub.web.csrf import require_form_csrf

router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates")
)


@router.get("/uploads", response_class=HTMLResponse)
def upload_page(
    request: Request,
    lecture_id: int | None = None,
) -> HTMLResponse:
    selected_lecture = None
    if lecture_id is not None:
        selected_lecture = _catalog(request).get_lecture(lecture_id)
        if selected_lecture is None:
            raise HTTPException(404, "lecture was not found")
    return templates.TemplateResponse(
        request=request,
        name="uploads.html",
        context={
            "selected_lecture": selected_lecture,
        },
    )


@router.get("/uploads/{kind}")
def legacy_upload_page(
    kind: UploadKind,
    lecture_id: int | None = None,
) -> RedirectResponse:
    destination = "/uploads"
    if lecture_id is not None:
        destination += f"?lecture_id={lecture_id}"
    return RedirectResponse(destination, status_code=307)


class ChunkCreate(BaseModel):
    kind: UploadKind
    filename: str
    total_size: int = Field(ge=1)
    sha256: str


def _repository(request: Request) -> IngestionRepository:
    return cast(
        IngestionRepository,
        request.app.state.ingestion_repository,
    )


def _staging(request: Request) -> StagingService:
    return cast(StagingService, request.app.state.upload_staging)


def _ingestion(request: Request) -> IngestionService:
    return cast(IngestionService, request.app.state.ingestion_service)


def _catalog(request: Request) -> CatalogRepository:
    return cast(CatalogRepository, request.app.state.catalog_repository)


@router.post("/uploads/{kind}", status_code=202)
def upload_files(
    kind: UploadKind,
    request: Request,
    files: Annotated[list[UploadFile], File()],
    csrf_token: Annotated[str | None, Form()] = None,
    lecture_id: Annotated[int | None, Form()] = None,
) -> dict[str, str]:
    require_form_csrf(request, csrf_token)
    if not files:
        raise HTTPException(422, "at least one file is required")
    _require_lecture(request, lecture_id)
    try:
        _staging(request).prevalidate_files(
            kind,
            ((upload.filename or "", upload.file) for upload in files),
        )
    except UploadRejected as error:
        raise HTTPException(422, str(error)) from error
    batch = _staging(request).begin_batch(kind)
    repository = _repository(request)
    repository.create_batch(kind, batch.id)
    try:
        for upload in files:
            staged = _staging(request).stage_file(
                batch,
                upload.filename or "",
                upload.file,
            )
            repository.add_item(kind, staged)
            _assign_or_match(request, staged.item_id, lecture_id)
    except UploadRejected as error:
        repository.set_batch_state(batch.id, UploadState.FAILED)
        raise HTTPException(422, str(error)) from error
    return {"batch_id": batch.id}


@router.get("/api/upload-batches/{batch_id}")
def batch_status(batch_id: str, request: Request) -> JSONResponse:
    batch = _repository(request).get_batch(batch_id)
    if batch is None:
        raise HTTPException(404, "upload batch not found")
    payload = batch.public_dict()
    items = cast(list[dict[str, object]], payload["items"])
    for item in items:
        if item["state"] != UploadState.AWAITING_CONFIRMATION.value:
            continue
        lecture_id = item.get("lecture_id")
        if not isinstance(lecture_id, int):
            continue
        lecture = _catalog(request).get_lecture(lecture_id)
        if lecture is None:
            continue
        item["duplicate_warning"] = {
            "subject": lecture.subject,
            "lecture_number": lecture.lecture_number,
            "topic": lecture.topic,
        }
    return JSONResponse(payload)


def _decision_payload(item_id: str, state: UploadState) -> dict[str, str]:
    return {"item_id": item_id, "state": state.value}


@router.post("/api/upload-items/{item_id}/confirm")
def confirm_transcript(
    item_id: str,
    request: Request,
) -> dict[str, str]:
    try:
        item = _ingestion(request).confirm_processing(item_id)
    except KeyError as error:
        raise HTTPException(404, "upload item not found") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return _decision_payload(item.id, item.state)


@router.post("/api/upload-items/{item_id}/discard")
def discard_transcript(
    item_id: str,
    request: Request,
) -> dict[str, str]:
    try:
        item = _ingestion(request).discard_item(item_id)
    except KeyError as error:
        raise HTTPException(404, "upload item not found") from error
    except (OSError, UploadRejected, ValueError) as error:
        raise HTTPException(409, str(error)) from error
    return _decision_payload(item.id, item.state)


@router.post("/api/upload-chunks", status_code=201)
def create_chunk_session(
    payload: ChunkCreate,
    request: Request,
) -> dict[str, object]:
    try:
        session = _staging(request).begin_chunks(
            payload.kind,
            payload.filename,
            payload.total_size,
            payload.sha256,
        )
    except UploadRejected as error:
        raise HTTPException(422, str(error)) from error
    _repository(request).create_batch(payload.kind, session.batch_id)
    return {
        "session_id": session.id,
        "batch_id": session.batch_id,
        "received": session.received,
    }


@router.put("/api/upload-chunks/{session_id}")
async def append_chunk(
    session_id: str,
    offset: int,
    request: Request,
) -> dict[str, int]:
    body = await request.body()
    try:
        received = await asyncio.to_thread(
            _staging(request).append_chunk,
            session_id,
            offset,
            BytesIO(body),
        )
    except UploadRejected as error:
        raise HTTPException(422, str(error)) from error
    return {"received": received}


@router.post("/api/upload-chunks/{session_id}/finalize", status_code=202)
def finalize_chunks(
    session_id: str,
    request: Request,
    lecture_id: int | None = None,
) -> dict[str, str]:
    _require_lecture(request, lecture_id)
    try:
        staged = _staging(request).finalize_chunks(session_id)
    except UploadRejected as error:
        raise HTTPException(422, str(error)) from error
    batch = _repository(request).get_batch(staged.batch_id)
    if batch is None:
        raise HTTPException(409, "chunk upload batch is missing")
    _repository(request).add_item(batch.kind, staged)
    _assign_or_match(request, staged.item_id, lecture_id)
    return {"batch_id": staged.batch_id, "item_id": staged.item_id}


def _require_lecture(request: Request, lecture_id: int | None) -> None:
    if lecture_id is not None and _catalog(request).get_lecture(lecture_id) is None:
        raise HTTPException(404, "lecture was not found")


def _assign_or_match(
    request: Request,
    item_id: str,
    lecture_id: int | None,
) -> None:
    service = _ingestion(request)
    if lecture_id is None:
        service.match_item(item_id)
    else:
        service.assign(item_id, lecture_id)
