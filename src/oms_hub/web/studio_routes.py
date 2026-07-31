from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from oms_hub.study_generation.notebook import StoredNotebookLMGateway
from oms_hub.study_generation.notebook_errors import (
    NotebookAuthenticationError,
    NotebookGatewayError,
)
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.study_generation.studio_service import StudioService
from oms_hub.web.csrf import require_form_csrf

router = APIRouter(prefix="/studio")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class StudioRunRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=100)
    exam_number: int = Field(ge=1)
    prompt: str = Field(min_length=1, max_length=50_000)
    source_ids: list[str] = Field(default_factory=list, max_length=100)
    label: str = Field(min_length=1, max_length=300)
    destination_subject: str = Field(min_length=1, max_length=100)
    destination_exam_number: int = Field(ge=1)


def _choices(request: Request) -> tuple[dict[str, object], ...]:
    grouped: dict[str, set[int]] = {}
    for lecture in request.app.state.catalog_repository.list_lectures():
        grouped.setdefault(lecture.subject, set()).add(lecture.exam_number)
    return tuple(
        {"subject": subject, "exams": tuple(sorted(exams))}
        for subject, exams in sorted(grouped.items())
    )


def _validated_subject(request: Request, subject: str, exam_number: int) -> str:
    subject_key = " ".join(subject.casefold().split())
    for choice in _choices(request):
        candidate = str(choice["subject"])
        exams = choice["exams"]
        if (
            " ".join(candidate.casefold().split()) == subject_key
            and isinstance(exams, tuple)
            and exam_number in exams
        ):
            return candidate
    raise HTTPException(422, "select a current course and exam")


@router.get("", response_class=HTMLResponse)
def studio_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="notebook_studio.html",
        context={"courses": _choices(request)},
    )


@router.get("/sources")
def sources(
    request: Request,
    subject_key: str | None = None,
    exam_number: int | None = None,
) -> JSONResponse:
    records = request.app.state.studio_repository.list_sources(subject_key, exam_number)
    return JSONResponse(
        {
            "sources": [
                {
                    "id": item.id,
                    "title": item.title,
                    "type": item.source_type.value,
                    "state": item.state.value,
                    "attempts": item.attempts,
                    "next_attempt_at": item.next_attempt_at,
                    "error": item.error,
                    "remote_source_id": item.remote_source_id,
                    "converted_from_pptx": item.converted_from_pptx,
                }
                for item in records
            ]
        }
    )


@router.get("/runs")
def runs(
    request: Request,
    subject_key: str | None = None,
    exam_number: int | None = None,
) -> JSONResponse:
    records = request.app.state.studio_repository.list_runs(subject_key, exam_number)
    return JSONResponse(
        {
            "runs": [
                {
                    "id": item.id,
                    "label": item.label,
                    "state": item.state.value,
                    "stage": item.stage.value,
                    "attempts": item.attempts,
                    "next_attempt_at": item.next_attempt_at,
                    "error": item.error,
                    "raw_response": item.raw_response,
                    "source_ids": [source.source_id for source in item.sources],
                    "destination_subject": item.destination_subject,
                    "destination_exam_number": item.destination_exam_number,
                    "published_url": (
                        f"/public/quizzes/{item.published_token}" if item.published_token else None
                    ),
                    "attempt_history": [
                        {
                            "attempt_number": attempt.attempt_number,
                            "diagnostic_source": attempt.diagnostic_source,
                            "raw_response": attempt.raw_response,
                            "error": attempt.error,
                            "created_at": attempt.created_at,
                        }
                        for attempt in request.app.state.studio_repository.list_run_attempts(
                            item.id
                        )
                    ],
                }
                for item in records
            ]
        },
        headers={"Cache-Control": "no-store"},
    )


@router.delete("/sources/{source_id}")
def delete_source(request: Request, source_id: str) -> JSONResponse:
    require_form_csrf(request, None)
    repository = request.app.state.studio_repository
    source = repository.get(source_id)
    if source is None:
        raise HTTPException(404, "Studio source was not found")
    if source.remote_notebook_id and source.remote_source_id:
        gateway = cast(StoredNotebookLMGateway, request.app.state.notebook_gateway)
        try:
            gateway.delete_studio_source(
                source.remote_notebook_id,
                source.remote_source_id,
            )
        except NotebookAuthenticationError as error:
            request.app.state.notebook_connection.invalidate(str(error))
            raise HTTPException(409, str(error)) from error
        except NotebookGatewayError as error:
            raise HTTPException(409, str(error)) from error
    deleted = repository.mark_source_deleted(source_id)
    return JSONResponse({"id": deleted.id, "state": deleted.state.value})


@router.post("/runs/{run_id}/rerun", status_code=202)
def rerun(request: Request, run_id: str) -> JSONResponse:
    require_form_csrf(request, None)
    try:
        successor = request.app.state.studio_repository.rerun(run_id)
    except KeyError as error:
        raise HTTPException(404, "Studio run was not found") from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return JSONResponse(
        {"id": successor.id, "state": successor.state.value},
        status_code=202,
    )


@router.delete("/runs/{run_id}/publication")
def unpublish(request: Request, run_id: str) -> JSONResponse:
    require_form_csrf(request, None)
    repository = cast(
        GenerationRepository,
        request.app.state.generation_repository,
    )
    try:
        token = repository.unpublish_studio_quiz(run_id)
    except KeyError as error:
        raise HTTPException(404, "published Studio quiz was not found") from error
    return JSONResponse({"token": token, "state": "unpublished"})


@router.post("/runs", status_code=202)
def queue_run(request: Request, submission: StudioRunRequest) -> JSONResponse:
    require_form_csrf(request, None)
    subject = _validated_subject(
        request,
        submission.subject,
        submission.exam_number,
    )
    destination_subject = _validated_subject(
        request,
        submission.destination_subject,
        submission.destination_exam_number,
    )
    try:
        run = _service(request).queue_run(
            subject,
            submission.exam_number,
            submission.prompt,
            submission.source_ids,
            submission.label,
            destination_subject,
            submission.destination_exam_number,
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return JSONResponse(
        {"id": run.id, "state": run.state.value, "stage": run.stage.value},
        status_code=202,
        headers={"Cache-Control": "no-store"},
    )


def _service(request: Request) -> StudioService:
    return cast(StudioService, request.app.state.studio_service)


@router.post("/sources/file", status_code=202)
def add_file(
    request: Request,
    subject: Annotated[str, Form()],
    exam_number: Annotated[int, Form()],
    title: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    csrf_token: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    canonical_subject = _validated_subject(request, subject, exam_number)
    service = _service(request)
    payload = file.file.read(service.max_file_bytes + 1)
    try:
        source = service.add_file(
            canonical_subject,
            exam_number,
            title,
            file.filename or "source",
            payload,
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return JSONResponse({"id": source.id, "state": source.state.value}, status_code=202)


@router.post("/sources/text", status_code=202)
def add_text(
    request: Request,
    subject: Annotated[str, Form()],
    exam_number: Annotated[int, Form()],
    title: Annotated[str, Form()],
    text: Annotated[str, Form()],
    csrf_token: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    canonical_subject = _validated_subject(request, subject, exam_number)
    try:
        source = _service(request).add_text(
            canonical_subject,
            exam_number,
            title,
            text,
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return JSONResponse({"id": source.id, "state": source.state.value}, status_code=202)


@router.post("/sources/url", status_code=202)
def add_url(
    request: Request,
    subject: Annotated[str, Form()],
    exam_number: Annotated[int, Form()],
    title: Annotated[str, Form()],
    url: Annotated[str, Form()],
    csrf_token: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    canonical_subject = _validated_subject(request, subject, exam_number)
    try:
        source = _service(request).add_url(
            canonical_subject,
            exam_number,
            title,
            url.strip(),
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return JSONResponse({"id": source.id, "state": source.state.value}, status_code=202)
