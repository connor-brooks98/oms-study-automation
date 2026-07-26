import json
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from oms_hub.repositories import CatalogRepository
from oms_hub.tracker_preview import TrackerPreview, TrackerPreviewService

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(prefix="/settings")

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
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={},
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
