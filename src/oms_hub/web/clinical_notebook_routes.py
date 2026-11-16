"""Private Study Hub controls for the shared Clinical Notebook source API."""

import json
from contextlib import ExitStack
from pathlib import Path
from typing import cast

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from oms_hub.artifacts import ArtifactError, ArtifactRole, ArtifactService
from oms_hub.clinical_notebook_service import ClinicalNotebookService, Material
from oms_hub.ingestion.domain import UploadKind
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.repositories import CatalogRepository
from oms_hub.web.csrf import require_form_csrf

router = APIRouter(prefix="/settings/clinical-notebook")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def service(request: Request) -> ClinicalNotebookService:
    return cast(ClinicalNotebookService, request.app.state.clinical_notebook)


def owner(request: Request) -> str:
    value = getattr(request.state, "study_owner_id", None)
    if not isinstance(value, str) or not value:
        raise HTTPException(403, "Private Hub sign-in required.")
    return value


def response(data: object, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        data, status_code=status_code, headers={"Cache-Control": "private, no-store"}
    )


def exam_key(request: Request, lecture_id: int) -> str:
    lecture = CatalogRepository(request.app.state.database).get_lecture(lecture_id)
    if lecture is None:
        raise HTTPException(404, "Lecture not found.")
    return json.dumps([lecture.subject, lecture.exam_number], separators=(",", ":"))


@router.get("", response_class=HTMLResponse)
def page(request: Request) -> HTMLResponse:
    owner(request)
    return templates.TemplateResponse(
        request=request,
        name="clinical_notebook.html",
        context={},
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/status")
def status(request: Request, lecture_id: int | None = None) -> JSONResponse:
    identity = owner(request)
    exam = exam_key(request, lecture_id) if lecture_id is not None else None
    return response(
        {
            **service(request).status(identity, exam),
            "jobs": service(request).jobs(identity, lecture_id=lecture_id),
        }
    )


@router.post("/connect")
def connect(
    request: Request,
    endpoint: str = Form(),
    token: str = Form(),
    label: str = Form(),
    csrf_token: str = Form(),
) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    service(request).configure(owner(request), endpoint, token, label)
    return response({"configured": True})


@router.post("/disconnect")
def disconnect(request: Request, csrf_token: str = Form()) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    service(request).disconnect(owner(request))
    return response({"configured": False})


@router.post("/notebooks")
def notebooks(request: Request, csrf_token: str = Form()) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    return response({"items": service(request).notebooks(owner(request))})


@router.post("/lectures/{lecture_id}/destination")
def save_destination(
    request: Request, lecture_id: int, notebook_id: str = Form(), csrf_token: str = Form()
) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    service(request).save_destination(owner(request), exam_key(request, lecture_id), notebook_id)
    return response({"saved": True})


@router.post("/lectures/{lecture_id}/send")
def send(
    request: Request,
    lecture_id: int,
    csrf_token: str = Form(),
    slides: bool = Form(True),
    transcript: bool = Form(True),
) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    identity = owner(request)
    exam = exam_key(request, lecture_id)
    database = request.app.state.database
    revisions = IngestionRepository(database).list_current_revisions(lecture_id)
    artifacts = ArtifactService(database, request.app.state.settings)
    try:
        with ExitStack() as stack:
            materials = []
            for revision in revisions:
                if revision.state != "current":
                    continue
                is_slides = revision.kind == UploadKind.SLIDES
                if (is_slides and not slides) or (not is_slides and not transcript):
                    continue
                artifact = artifacts.resolve(
                    revision.id, ArtifactRole.PDF if is_slides else ArtifactRole.CLEANED
                )
                name = artifact.path.name
                if not is_slides and Path(name).suffix.lower() not in {".txt", ".md"}:
                    name = f"{artifact.path.stem}.txt"
                materials.append(
                    Material(
                        name,
                        stack.enter_context(artifact.path.open("rb")),
                        "slides" if is_slides else "transcript",
                        revision.id,
                        revision.derived_sha256,
                    )
                )
            requested_roles = {
                role
                for role, selected in (("slides", slides), ("transcript", transcript))
                if selected
            }
            if {material.role for material in materials} != requested_roles:
                raise HTTPException(
                    409,
                    "A selected material is no longer available. "
                    "Reload and choose materials again.",
                )
            job = service(request).enqueue(identity, exam, lecture_id, materials)
    except ArtifactError as error:
        raise HTTPException(
            409, "Selected lecture materials are not ready. Check the lecture files."
        ) from error
    return response({"job_id": job}, 202)


@router.post("/jobs/{job_id}/retry")
def retry(request: Request, job_id: str, csrf_token: str = Form()) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    service(request).resume(owner(request), job_id)
    return response({"job_id": job_id}, 202)


@router.post("/jobs/{job_id}/check")
def check(request: Request, job_id: str, csrf_token: str = Form()) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    service(request).check(owner(request), job_id)
    return response({"job_id": job_id})
