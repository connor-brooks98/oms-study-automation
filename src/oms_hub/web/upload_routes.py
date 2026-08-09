import hashlib
import json
from io import BytesIO
from pathlib import Path
from typing import Annotated, cast
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from oms_hub.ingestion.domain import (
    StagedUpload,
    UploadKind,
    UploadManifestSlot,
    UploadState,
)
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.ingestion.service import IngestionService
from oms_hub.ingestion.staging import StagingService, UploadRejected
from oms_hub.repositories import CatalogRepository

router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates")
)


@router.get("/uploads/{kind}", response_class=HTMLResponse)
def upload_page(
    kind: UploadKind,
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
            "kind": kind,
            "accept": (
                ".pptx"
                if kind is UploadKind.SLIDES
                else ".txt"
            ),
            "selected_lecture": selected_lecture,
        },
    )


class ChunkCreate(BaseModel):
    kind: UploadKind
    filename: str
    total_size: int = Field(ge=1)
    sha256: str
    manifest_id: str | None = None
    slot_id: str | None = None


class ManifestFile(BaseModel):
    slot_id: str
    filename: str
    size_bytes: int = Field(ge=1)
    sha256: str


class ManifestCreate(BaseModel):
    kind: UploadKind
    files: list[ManifestFile]
    lecture_id: int | None = None


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


@router.post("/uploads/{kind}", status_code=202, response_model=None)
def upload_files(
    kind: UploadKind,
    request: Request,
    files: Annotated[list[UploadFile], File()],
    lecture_id: Annotated[int | None, Form()] = None,
    manifest_id: Annotated[str | None, Form()] = None,
    slot_ids: Annotated[list[str] | None, Form()] = None,
) -> dict[str, str] | JSONResponse:
    if not files:
        raise HTTPException(422, "at least one file is required")
    _require_lecture(request, lecture_id)
    staging = _staging(request)
    created_here = manifest_id is None
    if manifest_id is None:
        slots = []
        for upload in files:
            size_bytes, sha256 = _upload_size_and_hash(upload)
            slots.append(
                ManifestFile(
                    slot_id=str(uuid4()),
                    filename=upload.filename or "",
                    size_bytes=size_bytes,
                    sha256=sha256,
                )
            )
        try:
            created = _create_manifest(staging, kind, slots, lecture_id)
            created_id = created["manifest_id"]
            if not isinstance(created_id, str):
                raise AssertionError("manifest response is missing its identifier")
            manifest_id = created_id
        except HTTPException as error:
            return JSONResponse(
                status_code=422,
                content={
                    "errors": [
                        {
                            "slot_id": slot.slot_id,
                            "filename": slot.filename,
                            "code": "validation_failed",
                            "detail": str(error.detail),
                        }
                        for slot in slots
                        if Path(slot.filename).suffix.casefold()
                        != (".pptx" if kind is UploadKind.SLIDES else ".txt")
                    ]
                    or [
                        {
                            "slot_id": slot.slot_id,
                            "filename": slot.filename,
                            "code": "validation_failed",
                            "detail": str(error.detail),
                        }
                        for slot in slots
                    ],
                },
            )
        slot_ids = [slot.slot_id for slot in slots]
    if slot_ids is None or len(slot_ids) != len(files):
        raise HTTPException(422, "every multipart file needs one manifest slot")
    assert manifest_id is not None
    errors = _stage_multipart_members(staging, manifest_id, slot_ids, files)
    if errors:
        staging.discard_manifest(manifest_id)
        return JSONResponse(status_code=422, content={"errors": errors})
    # Legacy multipart remains a one-shot call. Explicit manifests are
    # finalized only after their chunk siblings have arrived.
    if not created_here:
        return {"manifest_id": manifest_id}
    return _finalize_manifest(request, manifest_id)


@router.post("/api/upload-manifests", status_code=201, response_model=None)
def create_manifest(
    payload: ManifestCreate,
    request: Request,
) -> dict[str, object] | JSONResponse:
    _require_lecture(request, payload.lecture_id)
    try:
        return _create_manifest(
            _staging(request), payload.kind, payload.files, payload.lecture_id
        )
    except HTTPException as error:
        return JSONResponse(
            status_code=422,
            content={
                "errors": [
                    {
                        "slot_id": file.slot_id,
                        "filename": file.filename,
                        "code": "validation_failed",
                        "detail": str(error.detail),
                    }
                    for file in payload.files
                ]
            },
        )


@router.post(
    "/api/upload-manifests/{manifest_id}/finalize",
    status_code=202,
    response_model=None,
)
def finalize_manifest(manifest_id: str, request: Request) -> dict[str, str] | JSONResponse:
    return _finalize_manifest(request, manifest_id)


@router.delete("/api/upload-manifests/{manifest_id}", status_code=204)
def cancel_manifest(manifest_id: str, request: Request) -> None:
    _staging(request).discard_manifest(manifest_id)


@router.get("/api/upload-batches/{batch_id}")
def batch_status(batch_id: str, request: Request) -> JSONResponse:
    # This is an idempotent opportunistic hook.  Runtime startup/periodic
    # ownership is supplied by lane B/integration through collect_staging().
    _ingestion(request).collect_staging()
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
        session = (
            _staging(request).begin_manifest_chunks(payload.manifest_id, payload.slot_id)
            if payload.manifest_id is not None and payload.slot_id is not None
            else _staging(request).begin_chunks(
                payload.kind,
                payload.filename,
                payload.total_size,
                payload.sha256,
            )
        )
    except UploadRejected as error:
        raise HTTPException(422, str(error)) from error
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
        received = _staging(request).append_chunk(
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
    # A manifest-owned chunk is now staged but not durable. Legacy chunk
    # callers retain their historical one-file finalization behaviour.
    if _staging(request)._manifest_root(staged.batch_id).is_dir():
        return {"manifest_id": staged.batch_id, "item_id": staged.item_id}
    batch = _repository(request).get_batch(staged.batch_id)
    inferred_kind = (
        UploadKind.TRANSCRIPTS
        if staged.original_filename.casefold().endswith(".txt")
        else UploadKind.SLIDES
    )
    if batch is None:
        # Older chunk sessions had no manifest and no durable batch until now.
        _repository(request).create_batch(inferred_kind, staged.batch_id)
    _repository(request).add_item(batch.kind if batch else inferred_kind, staged)
    _assign_or_match(request, staged.item_id, lecture_id)
    return {"batch_id": staged.batch_id, "item_id": staged.item_id}


def _upload_size_and_hash(upload: UploadFile) -> tuple[int, str]:
    stream = upload.file
    stream.seek(0)
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(1024 * 1024):
        size += len(chunk)
        digest.update(chunk)
    stream.seek(0)
    return size, digest.hexdigest()


def _create_manifest(
    staging: StagingService,
    kind: UploadKind,
    files: list[ManifestFile],
    lecture_id: int | None,
) -> dict[str, object]:
    try:
        manifest = staging.begin_manifest(
            kind,
            [
                UploadManifestSlot(
                    id=file.slot_id,
                    filename=file.filename,
                    size_bytes=file.size_bytes,
                    sha256=file.sha256,
                )
                for file in files
            ],
            lecture_id,
        )
    except UploadRejected as error:
        raise HTTPException(422, str(error)) from error
    return {
        "manifest_id": manifest.id,
        "slots": [
            {"slot_id": slot.id, "filename": slot.filename}
            for slot in manifest.slots
        ],
    }


def _stage_multipart_members(
    staging: StagingService,
    manifest_id: str,
    slot_ids: list[str],
    files: list[UploadFile],
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for slot_id, upload in zip(slot_ids, files, strict=True):
        try:
            staging.stage_manifest_file(manifest_id, slot_id, upload.file)
        except UploadRejected as error:
            errors.append(
                {
                    "slot_id": slot_id,
                    "filename": upload.filename or "",
                    "code": "validation_failed",
                    "detail": str(error),
                }
            )
    return errors


def _finalize_manifest(
    request: Request,
    manifest_id: str,
) -> dict[str, str] | JSONResponse:
    staging = _staging(request)
    try:
        manifest = staging.get_manifest(manifest_id)
        _require_lecture(request, manifest.lecture_id)
        staged = staging.manifest_uploads(manifest_id)
    except UploadRejected as error:
        try:
            payload = json.loads(str(error))
        except json.JSONDecodeError:
            raise HTTPException(422, str(error)) from error
        return JSONResponse(status_code=422, content=payload)
    service = _ingestion(request)
    decisions = (
        {}
        if manifest.lecture_id is not None
        else {
            item.item_id: service.decide_staged(
                manifest.kind, item.path, item.original_filename
            )
            for item in staged
        }
    )
    batch_id = str(uuid4())
    moved: list[StagedUpload] = []
    try:
        moved = staging.promote_manifest(manifest_id, batch_id)
        _repository(request).finalize_batch(
            manifest.kind,
            batch_id,
            moved,
            lecture_id=manifest.lecture_id,
            decisions=decisions,
        )
    except Exception:
        if moved:
            staging.revert_promoted_manifest(manifest_id, batch_id, moved)
        raise
    staging.discard_manifest(manifest_id)
    if manifest.lecture_id is not None:
        for _ in moved:
            service._complete_match_steps(manifest.lecture_id, manifest.kind)
    else:
        for decision in decisions.values():
            if decision.lecture_id is not None:
                service._complete_match_steps(decision.lecture_id, manifest.kind)
    return {"batch_id": batch_id}


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
