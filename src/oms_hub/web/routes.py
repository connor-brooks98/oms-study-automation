from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from oms_hub.checklist import ChecklistService
from oms_hub.canvas.repository import CanvasRepository
from oms_hub.domain import LectureKey, LectureStepName, StepStatus
from oms_hub.naming import display_title
from oms_hub.panopto.repository import PanoptoRepository
from oms_hub.repositories import CatalogRepository, LectureInput
from oms_hub.web.schemas import LectureApi, StepApi

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter()


def _repo(request: Request) -> CatalogRepository:
    return CatalogRepository(request.app.state.database)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    repository = _repo(request)
    rows: list[dict[str, object]] = []
    review_count = 0
    for lecture in repository.list_lectures():
        completed = sum(
            item.status in {"complete", "skipped"}
            for item in lecture.steps
        )
        needs_review = sum(
            item.status == "needs_review" for item in lecture.steps
        )
        review_count += needs_review
        key = LectureKey(
            lecture.subject,
            lecture.exam_number,
            lecture.lecture_number,
            lecture.topic,
        )
        rows.append(
            {
                "lecture": lecture,
                "title": display_title(key),
                "completed": completed,
                "total": len(lecture.steps),
                "needs_review": needs_review,
            }
        )
    review_count += len(repository.list_review_events())
    review_count += len(repository.list_import_issues())
    canvas_repository = CanvasRepository(request.app.state.database)
    review_count += len(canvas_repository.list_review_items())
    review_count += len(canvas_repository.list_proposed_revisions())
    panopto_repository = PanoptoRepository(
        request.app.state.database,
        request.app.state.settings.panopto_tenant_url,
    )
    panopto_review_count = panopto_repository.pending_review_count()
    review_count += panopto_review_count
    input_tokens, output_tokens, cost_microusd = panopto_repository.usage_totals()
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "rows": rows,
            "review_count": review_count,
            "canvas_connection": canvas_repository.connection(),
            "panopto_connection": panopto_repository.connection(),
            "panopto_review_count": panopto_review_count,
            "panopto_input_tokens": input_tokens,
            "panopto_output_tokens": output_tokens,
            "panopto_cost_usd": cost_microusd / 1_000_000,
        },
    )


@router.get("/lectures/{lecture_id}", response_class=HTMLResponse)
def lecture_detail(request: Request, lecture_id: int) -> HTMLResponse:
    lecture = _repo(request).get_lecture(lecture_id)
    if lecture is None:
        return HTMLResponse("Lecture not found", status_code=404)
    key = LectureKey(
        lecture.subject,
        lecture.exam_number,
        lecture.lecture_number,
        lecture.topic,
    )
    return templates.TemplateResponse(
        request=request,
        name="lecture.html",
        context={"lecture": lecture, "title": display_title(key)},
    )


@router.get("/review", response_class=HTMLResponse)
def review(request: Request) -> HTMLResponse:
    repository = _repo(request)
    lectures = [
        item
        for item in repository.list_lectures()
        if any(step.status == "needs_review" for step in item.steps)
    ]
    return templates.TemplateResponse(
        request=request,
        name="review.html",
        context={
            "lectures": lectures,
            "external_events": repository.list_review_events(),
            "import_issues": repository.list_import_issues(),
        },
    )


@router.post("/lectures/{lecture_id}/steps/{step_name}")
def update_step(
    lecture_id: int,
    step_name: LectureStepName,
    request: Request,
    status: StepStatus = Form(),
    detail: str = Form(default=""),
) -> RedirectResponse:
    ChecklistService(_repo(request)).transition(
        lecture_id,
        step_name,
        status,
        detail or None,
    )
    return RedirectResponse(f"/lectures/{lecture_id}", status_code=303)


@router.post("/lectures/{lecture_id}/metadata")
def update_lecture_metadata(
    lecture_id: int,
    request: Request,
    subject: str = Form(),
    exam_number: int = Form(),
    lecture_number: int = Form(),
    topic: str = Form(),
    lecturer: str = Form(default=""),
    exam_date: str = Form(default=""),
) -> RedirectResponse:
    _repo(request).update_lecture(
        lecture_id,
        LectureInput(
            subject.strip(),
            exam_number,
            lecture_number,
            topic.strip(),
            lecturer.strip(),
            exam_date or None,
        ),
    )
    return RedirectResponse(f"/lectures/{lecture_id}", status_code=303)


@router.post("/review/import-issues/{issue_id}/resolve")
def resolve_import_issue(
    issue_id: int,
    request: Request,
    subject: str = Form(),
    exam_number: int = Form(),
    lecture_number: int = Form(),
    topic: str = Form(),
    lecturer: str = Form(default=""),
) -> RedirectResponse:
    repository = _repo(request)
    repository.upsert_lecture(
        LectureInput(
            subject=subject.strip(),
            exam_number=exam_number,
            lecture_number=lecture_number,
            topic=topic.strip(),
            lecturer=lecturer.strip(),
            exam_date=None,
        )
    )
    repository.resolve_import_issue(issue_id)
    return RedirectResponse("/review", status_code=303)


@router.get("/api/lectures", response_model=list[LectureApi])
def lecture_api(request: Request) -> list[LectureApi]:
    return [
        LectureApi(
            id=lecture.id,
            subject=lecture.subject,
            exam_number=lecture.exam_number,
            lecture_number=lecture.lecture_number,
            topic=lecture.topic,
            lecturer=lecture.lecturer,
            exam_date=lecture.exam_date,
            scheduled_start_utc=lecture.scheduled_start_utc,
            campus=lecture.campus,
            steps=[
                StepApi(
                    name=step.name,
                    status=step.status,
                    detail=step.detail,
                )
                for step in lecture.steps
            ],
        )
        for lecture in _repo(request).list_lectures()
    ]
