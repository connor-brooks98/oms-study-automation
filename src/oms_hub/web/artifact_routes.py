from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from oms_hub.artifacts import (
    ArtifactConflict,
    ArtifactNotFound,
    ArtifactRole,
    ArtifactService,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _service(request: Request) -> ArtifactService:
    return ArtifactService(
        request.app.state.database,
        request.app.state.settings,
    )


@router.get("/artifacts/{revision_id}/{role}")
def artifact(
    request: Request,
    revision_id: int,
    role: ArtifactRole,
) -> Response:
    try:
        resolved = _service(request).resolve(revision_id, role)
    except ArtifactNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ArtifactConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
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
