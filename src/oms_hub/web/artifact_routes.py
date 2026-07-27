from pathlib import Path
from typing import cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from oms_hub.artifacts import (
    ArtifactConflict,
    ArtifactNotFound,
    ArtifactRole,
    ArtifactService,
    ResolvedArtifact,
)
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.naming import sanitize_filename
from oms_hub.repositories import CatalogRepository

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _service(request: Request) -> ArtifactService:
    return ArtifactService(
        request.app.state.database,
        request.app.state.settings,
    )


def _resolve(
    request: Request,
    revision_id: int,
    role: ArtifactRole,
) -> ResolvedArtifact:
    try:
        return _service(request).resolve(revision_id, role)
    except ArtifactNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ArtifactConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/artifacts/{revision_id}/{role}")
def artifact(
    request: Request,
    revision_id: int,
    role: ArtifactRole,
) -> Response:
    resolved = _resolve(request, revision_id, role)
    headers = {"Cache-Control": "private, no-store"}
    if resolved.text:
        try:
            content = resolved.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise HTTPException(
                status_code=409,
                detail="artifact text is not readable UTF-8",
            ) from error
        return templates.TemplateResponse(
            request=request,
            name="artifact_text.html",
            context={
                "title": resolved.path.name,
                "content": content,
                "download_url": (
                    f"/artifacts/{revision_id}/cleaned/download"
                    if role is ArtifactRole.CLEANED
                    else None
                ),
            },
            headers=headers,
        )
    headers["Content-Disposition"] = (
        f'{resolved.disposition}; filename="{resolved.path.name}"'
    )
    return FileResponse(
        resolved.path,
        media_type=resolved.media_type,
        headers=headers,
    )


@router.get("/artifacts/{revision_id}/cleaned/download")
def download_cleaned_transcript(
    request: Request,
    revision_id: int,
) -> FileResponse:
    resolved = _resolve(
        request,
        revision_id,
        ArtifactRole.CLEANED,
    )
    try:
        resolved.path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise HTTPException(
            status_code=409,
            detail="artifact text is not readable UTF-8",
        ) from error
    repository = cast(
        IngestionRepository,
        request.app.state.ingestion_repository,
    )
    catalog = cast(
        CatalogRepository,
        request.app.state.catalog_repository,
    )
    revision = repository.get_study_revision(revision_id)
    lecture = catalog.get_lecture(revision.lecture_id)
    if lecture is None:
        raise HTTPException(
            status_code=409,
            detail="artifact lecture was not found",
        )
    filename = (
        sanitize_filename(
            f"{lecture.subject} - Lecture "
            f"{lecture.lecture_number:02d} - {lecture.topic} - Transcript"
        )
        + ".txt"
    )
    return FileResponse(
        resolved.path,
        media_type="text/plain; charset=utf-8",
        filename=filename,
        content_disposition_type="attachment",
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/review/replacements/{revision_id}/approve")
def approve_replacement(
    request: Request,
    revision_id: int,
) -> RedirectResponse:
    try:
        _service(request).approve(revision_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="revision not found") from error
    except (ArtifactConflict, ArtifactNotFound) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return RedirectResponse("/review", status_code=303)


@router.post("/review/replacements/{revision_id}/keep")
def keep_replacement(
    request: Request,
    revision_id: int,
) -> RedirectResponse:
    try:
        _service(request).keep_current(revision_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="revision not found") from error
    except ArtifactConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return RedirectResponse("/review", status_code=303)
