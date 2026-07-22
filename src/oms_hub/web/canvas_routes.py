import os
import uuid
from pathlib import Path
from typing import cast

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from oms_hub.canvas.domain import CourseMappingInput
from oms_hub.canvas.pairing import PairingService
from oms_hub.canvas.pipeline import CanvasPipeline
from oms_hub.canvas.repository import CanvasRepository
from oms_hub.repositories import CatalogRepository

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(prefix="/canvas")

SUBJECT_FIELDS = {
    "Neuro": "course_neuro",
    "MSK": "course_msk",
    "OPP": "course_opp",
    "EPC": "course_epc",
    "Heme/Lymph": "course_heme",
    "Cardio": "course_cardio",
    "Renal": "course_renal",
    "Resp": "course_resp",
}


def _repo(request: Request) -> CanvasRepository:
    return cast(CanvasRepository, request.app.state.canvas_repository)


def _pairing(request: Request) -> PairingService:
    return cast(PairingService, request.app.state.canvas_pairing)


def _pipeline(request: Request) -> CanvasPipeline:
    return cast(CanvasPipeline, request.app.state.canvas_pipeline)


def _write_probe(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    probe = path / f".oms-write-test-{uuid.uuid4().hex}"
    try:
        probe.write_text("ok", encoding="utf-8")
    finally:
        probe.unlink(missing_ok=True)


def _likely_icloud_roots() -> list[str]:
    profile = Path(os.path.expandvars(r"%USERPROFILE%"))
    candidates = [
        profile / "iCloudDrive",
        profile / "iCloudDrive" / "iCloud~com~apple~CloudDocs",
    ]
    return [str(item) for item in candidates if item.exists()]


@router.get("/setup", response_class=HTMLResponse)
def setup(request: Request) -> HTMLResponse:
    repository = _repo(request)
    connection = repository.connection()
    mappings = repository.list_course_mappings()
    checks = [
        ("Extension paired", bool(connection.credential_fingerprint)),
        ("Eight courses mapped", len(mappings) == 8),
        ("Local study folder confirmed", bool(connection.study_root)),
        ("iCloud staging folder confirmed", bool(connection.icloud_staging_root)),
        ("Discovery scan completed", bool(connection.last_successful_scan)),
        ("Discovery preview confirmed", connection.discovery_confirmed),
        ("Automatic processing enabled", connection.auto_process),
    ]
    mapped = {item.subject: item.course_id for item in mappings}
    return templates.TemplateResponse(
        request=request,
        name="canvas_setup.html",
        context={
            "connection": connection,
            "checks": checks,
            "subject_fields": SUBJECT_FIELDS,
            "candidates": repository.list_course_candidates(),
            "mapped": mapped,
            "icloud_roots": _likely_icloud_roots(),
            "study_default": connection.study_root or r"%USERPROFILE%\Documents\OMS II",
        },
    )


@router.post("/pair-code")
def create_pair_code(request: Request) -> RedirectResponse:
    code = _pairing(request).create_code()
    response = RedirectResponse("/canvas/setup", status_code=303)
    response.set_cookie(
        "canvas_pair_code",
        code.value,
        max_age=300,
        httponly=True,
        samesite="strict",
    )
    return response


@router.post("/mappings")
async def save_mappings(request: Request) -> RedirectResponse:
    form = await request.form()
    candidates = {item["course_id"]: item for item in _repo(request).list_course_candidates()}
    values: list[CourseMappingInput] = []
    for subject, field in SUBJECT_FIELDS.items():
        course_id = str(form.get(field, "")).strip()
        candidate = candidates.get(course_id)
        if candidate is None:
            raise HTTPException(status_code=409, detail=f"Choose a discovered course for {subject}")
        values.append(
            CourseMappingInput(
                course_id,
                candidate["course_name"],
                candidate["course_code"],
                subject,
            )
        )
    _repo(request).replace_course_mappings(values)
    return RedirectResponse("/canvas/setup", status_code=303)


@router.post("/paths")
def save_paths(
    request: Request,
    study_root: str = Form(),
    icloud_root: str = Form(),
) -> RedirectResponse:
    local = Path(os.path.expandvars(study_root.strip()))
    cloud = Path(os.path.expandvars(icloud_root.strip()))
    _write_probe(local)
    _write_probe(cloud)
    _repo(request).set_setup(study_root=str(local), icloud_staging_root=str(cloud))
    return RedirectResponse("/canvas/setup", status_code=303)


@router.post("/scan-now")
def scan_now(request: Request) -> RedirectResponse:
    _repo(request).request_scan()
    return RedirectResponse("/canvas/setup", status_code=303)


@router.post("/confirm-preview")
def confirm_preview(request: Request) -> RedirectResponse:
    connection = _repo(request).connection()
    if not connection.last_successful_scan or connection.last_scan_item_count < 1:
        raise HTTPException(
            status_code=409,
            detail="Complete a discovery scan that finds at least one item first",
        )
    _repo(request).set_setup(discovery_confirmed=True)
    return RedirectResponse("/canvas/setup", status_code=303)


@router.post("/enable")
def enable(request: Request) -> RedirectResponse:
    repository = _repo(request)
    connection = repository.connection()
    ready = (
        bool(connection.credential_fingerprint)
        and len(repository.list_course_mappings()) == 8
        and bool(connection.study_root)
        and bool(connection.icloud_staging_root)
        and bool(connection.last_successful_scan)
        and connection.last_scan_item_count > 0
        and connection.discovery_confirmed
    )
    if not ready:
        raise HTTPException(status_code=409, detail="Complete every setup step first")
    repository.set_setup(auto_process=True)
    return RedirectResponse("/canvas/setup", status_code=303)


@router.post("/disable")
def disable(request: Request) -> RedirectResponse:
    _repo(request).set_setup(auto_process=False)
    return RedirectResponse("/canvas/setup", status_code=303)


@router.post("/revoke")
def revoke(request: Request) -> RedirectResponse:
    _pairing(request).revoke()
    return RedirectResponse("/canvas/setup", status_code=303)


@router.get("/review", response_class=HTMLResponse)
def canvas_review(request: Request) -> HTMLResponse:
    repository = _repo(request)
    catalog = CatalogRepository(request.app.state.database)
    lectures = catalog.list_lectures()
    return templates.TemplateResponse(
        request=request,
        name="canvas_review.html",
        context={
            "items": repository.list_review_items(),
            "proposed": repository.list_proposed_revisions(),
            "lectures": lectures,
        },
    )


@router.post("/revisions/{revision_id}/approve")
def approve(revision_id: int, request: Request) -> RedirectResponse:
    try:
        _pipeline(request).approve_replacement(revision_id)
    except (KeyError, ValueError, OSError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return RedirectResponse("/canvas/review", status_code=303)


@router.post("/revisions/{revision_id}/keep")
def keep(revision_id: int, request: Request) -> RedirectResponse:
    try:
        _pipeline(request).keep_current(revision_id)
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return RedirectResponse("/canvas/review", status_code=303)


@router.post("/sources/{source_item_id}/remap")
def remap(
    source_item_id: int,
    request: Request,
    lecture_id: int = Form(),
) -> RedirectResponse:
    try:
        _pipeline(request).remap_source(source_item_id, lecture_id)
    except KeyError as error:
        raise HTTPException(status_code=409, detail="Source or lecture was not found") from error
    return RedirectResponse("/canvas/review", status_code=303)
