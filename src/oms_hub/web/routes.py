from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from oms_hub.domain import (
    LectureKey,
    StepStatus,
    V2StepName,
)
from oms_hub.ingestion.domain import UploadKind
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.models import LecturePassModel
from oms_hub.naming import display_title
from oms_hub.progress import overall_status
from oms_hub.repositories import CatalogRepository, LectureInput
from oms_hub.study_generation.domain import GenerationKind
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.study_generation.service import revision_readiness_problem
from oms_hub.web.csrf import require_form_csrf
from oms_hub.web.lecture_labels import lecture_label
from oms_hub.web.schemas import LectureApi, LecturePassUpdate, StepApi

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter()

_COURSE_HUES = {
    "clinical neuroscience": 290,
    "neuro": 290,
    "neuroscience": 290,
    "msk": 50,
    "opp": 175,
    "epc": 95,
    "heme lymph": 15,
    "heme": 15,
    "cardio": 340,
    "renal": 135,
    "resp": 210,
    "respiratory": 210,
}
_V2_STEP_VALUES = {name.value for name in V2StepName}
_V2_RELEASE_VALUES = {
    name.value for name in V2StepName.first_release()
}


def _repo(request: Request) -> CatalogRepository:
    return CatalogRepository(request.app.state.database)


def _dashboard_context(request: Request) -> dict[str, object]:
    repository = _repo(request)
    ingestion = IngestionRepository(request.app.state.database)
    grouped: OrderedDict[str, dict[int, list[dict[str, object]]]] = (
        OrderedDict()
    )
    review_count = 0
    for lecture in repository.list_lectures():
        needs_review = sum(
            item.name in _V2_STEP_VALUES and item.status == "needs_review"
            for item in lecture.steps
        )
        review_count += needs_review
        key = LectureKey(
            lecture.subject,
            lecture.exam_number,
            lecture.lecture_number,
            lecture.topic,
        )
        v2_steps = {
            step.name: step.status
            for step in lecture.steps
            if step.name in _V2_STEP_VALUES
        }
        status = overall_status(v2_steps)
        release_steps = [
            v2_steps.get(step.value, StepStatus.WAITING.value)
            for step in V2StepName.first_release()
        ]
        release_completed = sum(
            value in {StepStatus.COMPLETE.value, StepStatus.SKIPPED.value}
            for value in release_steps
        )
        current_kinds = {
            revision.kind
            for revision in ingestion.list_current_revisions(lecture.id)
        }
        v2_row = {
            "lecture": lecture,
            "title": display_title(key),
            "status": status.value,
            "status_label": status.value.replace("_", " ").title(),
            "completed": release_completed,
            "total": len(release_steps),
            "percent": round(
                release_completed / len(release_steps) * 100
            ),
            "has_slides": UploadKind.SLIDES in current_kinds,
            "has_transcript": UploadKind.TRANSCRIPTS in current_kinds,
        }
        grouped.setdefault(lecture.subject, OrderedDict()).setdefault(
            lecture.exam_number,
            [],
        ).append(v2_row)
    review_count += len(repository.list_import_issues())
    return {
            "courses": [
                {
                    "name": subject,
                    "hue": _course_hue(subject),
                    "lecture_count": sum(
                        len(lectures) for lectures in exams.values()
                    ),
                    "exams": [
                        {
                            "number": exam_number,
                            "lectures": lectures,
                        }
                        for exam_number, lectures in sorted(exams.items())
                    ],
                }
                for subject, exams in grouped.items()
            ],
            "review_count": review_count,
        }


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context=_dashboard_context(request),
    )


@router.get("/lectures", response_class=HTMLResponse)
def lecture_library(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=_dashboard_context(request),
    )


@router.get("/lectures/exams/{exam_number}/passes", response_class=HTMLResponse)
def exam_passes(request: Request, exam_number: int, subject: str) -> HTMLResponse:
    lectures = _repo(request).list_exam_lectures(subject, exam_number)
    if not lectures:
        raise HTTPException(404, "exam was not found")
    return templates.TemplateResponse(
        request=request,
        name="exam_passes.html",
        context={
            "subject": subject,
            "exam_number": exam_number,
            "lectures": lectures,
            "course_hue": _course_hue(subject),
        },
    )


@router.get("/lectures/{lecture_id}", response_class=HTMLResponse)
def lecture_detail(request: Request, lecture_id: int) -> HTMLResponse:
    repository = _repo(request)
    lecture = repository.get_lecture(lecture_id)
    if lecture is None:
        return HTMLResponse("Lecture not found", status_code=404)
    key = LectureKey(
        lecture.subject,
        lecture.exam_number,
        lecture.lecture_number,
        lecture.topic,
    )
    v2_steps = {
        step.name: step.status
        for step in lecture.steps
        if step.name in _V2_STEP_VALUES
    }
    status = overall_status(v2_steps)
    step_by_name = {
        step.name: step
        for step in lecture.steps
        if step.name in _V2_RELEASE_VALUES
    }
    release_steps = [
        step_by_name[name.value]
        for name in V2StepName.first_release()
        if name.value in step_by_name
    ]
    completed = sum(
        step.status in {StepStatus.COMPLETE.value, StepStatus.SKIPPED.value}
        for step in release_steps
    )
    revisions = {
        revision.kind: revision
        for revision in IngestionRepository(
            request.app.state.database
        ).list_current_revisions(lecture_id)
    }
    slide_revision = revisions.get(UploadKind.SLIDES)
    transcript_revision = revisions.get(UploadKind.TRANSCRIPTS)
    generation = GenerationRepository(request.app.state.database)
    outline = generation.current_outline(lecture_id)
    quiz = generation.current_quiz(lecture_id)
    subject_lectures = sorted(
        (item for item in repository.list_lectures() if item.subject == lecture.subject),
        key=lambda item: (item.exam_number, item.lecture_number, item.id),
    )
    position = next(index for index, item in enumerate(subject_lectures) if item.id == lecture.id)
    return templates.TemplateResponse(
        request=request,
        name="lecture.html",
        context={
            "lecture": lecture,
            "title": display_title(key),
            "status": status.value,
            "status_label": status.value.replace("_", " ").title(),
            "progress_percent": round(
                completed / len(release_steps) * 100
            )
            if release_steps
            else 0,
            "release_steps": release_steps,
            "slide_revision": slide_revision,
            "slide_problem": revision_readiness_problem(slide_revision),
            "transcript_revision": transcript_revision,
            "transcript_problem": revision_readiness_problem(
                transcript_revision
            ),
            "course_hue": _course_hue(lecture.subject),
            "outline_output": outline,
            "quiz_output": quiz,
            "outline_job": generation.current_job(
                lecture_id,
                GenerationKind.OUTLINE,
            ),
            "quiz_job": generation.current_job(
                lecture_id,
                GenerationKind.QUIZ,
            ),
            "previous_lecture": subject_lectures[position - 1] if position else None,
            "next_lecture": (
                subject_lectures[position + 1]
                if position + 1 < len(subject_lectures)
                else None
            ),
            "pass_resources": repository.list_pass_resources(),
        },
    )


@router.get("/review", response_class=HTMLResponse)
def review(request: Request) -> HTMLResponse:
    repository = _repo(request)
    proposed_revisions = IngestionRepository(
        request.app.state.database
    ).list_proposed_revisions()
    proposed_revision_labels = {
        revision.id: lecture_label(lecture.subject, lecture.lecture_number)
        for revision in proposed_revisions
        if (lecture := repository.get_lecture(revision.lecture_id)) is not None
    }
    lectures = [
        item
        for item in repository.list_lectures()
        if any(
            step.name in _V2_STEP_VALUES and step.status == "needs_review"
            for step in item.steps
        )
    ]
    return templates.TemplateResponse(
        request=request,
        name="review.html",
        context={
            "lectures": lectures,
            "lecture_hues": {lecture.id: _course_hue(lecture.subject) for lecture in lectures},
            "import_issues": repository.list_import_issues(),
            "proposed_revisions": proposed_revisions,
            "proposed_revision_labels": proposed_revision_labels,
        },
    )


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


def _pass_payload(lecture_pass: LecturePassModel) -> dict[str, int | str | None]:
    return {
        "position": lecture_pass.position,
        "completed_on": lecture_pass.completed_on,
        "resource": lecture_pass.resource,
    }


@router.patch("/api/lectures/{lecture_id}/passes/{position}")
def update_lecture_pass(
    request: Request,
    lecture_id: int,
    position: int,
    update: LecturePassUpdate,
) -> JSONResponse:
    require_form_csrf(request, None)
    if not update.model_fields_set:
        raise HTTPException(422, "at least one pass field is required")

    repository = _repo(request)
    update_completed = "completed" in update.model_fields_set
    completed_on = (
        datetime.now(
            ZoneInfo(request.app.state.settings.timezone)
        ).date().isoformat()
        if update_completed and update.completed
        else None
    )
    update_resource = "resource" in update.model_fields_set
    resource = (
        update.resource.strip() or None
        if update_resource and update.resource
        else None
    )
    if resource and resource.casefold() == "other":
        raise HTTPException(422, "resource cannot be Other")
    try:
        saved = repository.update_pass(
            lecture_id,
            position,
            update_completed=update_completed,
            completed_on=completed_on,
            update_resource=update_resource,
            resource=resource,
        )
    except KeyError as error:
        raise HTTPException(404, "lecture pass was not found") from error
    return JSONResponse(
        _pass_payload(saved),
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/lectures/{lecture_id}/passes")
def add_lecture_pass(request: Request, lecture_id: int) -> JSONResponse:
    require_form_csrf(request, None)
    try:
        lecture_pass = _repo(request).append_pass(lecture_id)
    except KeyError as error:
        raise HTTPException(404, "lecture was not found") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return JSONResponse(
        _pass_payload(lecture_pass),
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


def _course_hue(subject: str) -> int:
    normalized = " ".join(
        subject.casefold().replace("&", " ").replace("/", " ").split()
    )
    for name, hue in _COURSE_HUES.items():
        if name in normalized or normalized in name:
            return hue
    return 255
