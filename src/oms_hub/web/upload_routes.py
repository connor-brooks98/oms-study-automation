from io import BytesIO
from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from oms_hub.ingestion.domain import UploadKind, UploadState
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.ingestion.staging import StagingService, UploadRejected

router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates")
)


@router.get("/uploads/{kind}", response_class=HTMLResponse)
def upload_page(
    kind: UploadKind,
    request: Request,
) -> HTMLResponse:
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
        },
    )


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


@router.post("/uploads/{kind}", status_code=202)
def upload_files(
    kind: UploadKind,
    request: Request,
    files: Annotated[list[UploadFile], File()],
) -> dict[str, str]:
    if not files:
        raise HTTPException(422, "at least one file is required")
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
            request.app.state.ingestion_service.match_item(
                staged.item_id
            )
    except UploadRejected as error:
        repository.set_batch_state(batch.id, UploadState.FAILED)
        raise HTTPException(422, str(error)) from error
    return {"batch_id": batch.id}


@router.get("/api/upload-batches/{batch_id}")
def batch_status(batch_id: str, request: Request) -> JSONResponse:
    batch = _repository(request).get_batch(batch_id)
    if batch is None:
        raise HTTPException(404, "upload batch not found")
    return JSONResponse(batch.public_dict())


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
) -> dict[str, str]:
    try:
        staged = _staging(request).finalize_chunks(session_id)
    except UploadRejected as error:
        raise HTTPException(422, str(error)) from error
    batch = _repository(request).get_batch(staged.batch_id)
    if batch is None:
        raise HTTPException(409, "chunk upload batch is missing")
    _repository(request).add_item(batch.kind, staged)
    request.app.state.ingestion_service.match_item(staged.item_id)
    return {"batch_id": staged.batch_id, "item_id": staged.item_id}
