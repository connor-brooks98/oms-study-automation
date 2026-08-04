from dataclasses import replace
from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from oms_hub.files.atomic import sha256_file
from oms_hub.study_generation.domain import NativeQuiz
from oms_hub.study_generation.native_quiz import (
    grade_answer,
    image_requirements,
    public_quiz_content,
)
from oms_hub.study_generation.notebook import StoredNotebookLMGateway
from oms_hub.study_generation.notebook_errors import (
    NotebookAuthenticationError,
    NotebookGatewayError,
)
from oms_hub.study_generation.quiz_images import (
    MAX_QUIZ_IMAGE_BYTES,
    QuizImageError,
    StudioQuizImageService,
)
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.study_generation.studio_domain import StudioQuizReview
from oms_hub.study_generation.studio_repository import StudioRepository
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


class PreviewAnswerSubmission(BaseModel):
    question_id: str = Field(pattern=r"^q[0-9]{1,3}$", max_length=4)
    choice_id: str = Field(pattern=r"^c[0-9]{1,2}$", max_length=3)


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
                    "image_review_url": (
                        f"/studio/runs/{item.id}/images"
                        if item.state.value == "awaiting_images"
                        else None
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


def _image_service(request: Request) -> StudioQuizImageService:
    return cast(
        StudioQuizImageService,
        request.app.state.studio_quiz_image_service,
    )


def _review_payload(review: StudioQuizReview) -> dict[str, object]:
    question_by_id = {question.id: question for question in review.quiz.questions}
    number_by_id = {
        question.id: number
        for number, question in enumerate(review.quiz.questions, start=1)
    }
    return {
        "run_id": review.run.id,
        "label": review.run.label,
        "state": review.run.state.value,
        "resolved": review.resolved,
        "preview_url": (
            f"/studio/runs/{review.run.id}/preview" if review.resolved else None
        ),
        "requirements": [
            {
                "image_key": requirement.image_key,
                "source_title": requirement.source_title,
                "locator": requirement.locator,
                "description": requirement.description,
                "uploaded": requirement.image is not None,
                "width": requirement.image.width if requirement.image else None,
                "height": requirement.image.height if requirement.image else None,
                "original_filename": (
                    requirement.image.original_filename if requirement.image else None
                ),
                "questions": [
                    {
                        "id": question_id,
                        "number": number_by_id[question_id],
                        "stem": question_by_id[question_id].stem,
                        "overridden": question_id in review.overridden_question_ids,
                    }
                    for question_id in requirement.question_ids
                ],
            }
            for requirement in review.requirements
        ],
    }


def _require_review(request: Request, run_id: str) -> StudioQuizReview:
    repository = cast(StudioRepository, request.app.state.studio_repository)
    try:
        return repository.quiz_review(run_id)
    except KeyError as error:
        raise HTTPException(404, "Studio run was not found") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.get("/runs/{run_id}/images", response_class=HTMLResponse)
def image_review_page(request: Request, run_id: str) -> HTMLResponse:
    review = _require_review(request, run_id)
    return templates.TemplateResponse(
        request=request,
        name="studio_quiz_images.html",
        context={"run": review.run},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/runs/{run_id}/image-review")
def image_review_status(request: Request, run_id: str) -> JSONResponse:
    return JSONResponse(
        _review_payload(_require_review(request, run_id)),
        headers={"Cache-Control": "no-store"},
    )


@router.post("/runs/{run_id}/images/{image_key}")
def upload_quiz_image(
    request: Request,
    run_id: str,
    image_key: str,
    file: Annotated[UploadFile, File()],
    csrf_token: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    payload = file.file.read(MAX_QUIZ_IMAGE_BYTES + 1)
    try:
        image = _image_service(request).upload(
            run_id,
            image_key,
            file.filename or "image",
            payload,
        )
        review = request.app.state.studio_repository.quiz_review(run_id)
    except KeyError as error:
        raise HTTPException(404, "Studio image requirement was not found") from error
    except QuizImageError as error:
        raise HTTPException(422, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return JSONResponse(
        {
            "image_key": image_key,
            "media_type": image.media_type,
            "width": image.width,
            "height": image.height,
            "original_filename": image.original_filename,
            "resolved": review.resolved,
        },
        headers={"Cache-Control": "no-store"},
    )


def _set_override(
    request: Request,
    run_id: str,
    question_id: str,
    enabled: bool,
) -> JSONResponse:
    require_form_csrf(request, None)
    try:
        review = request.app.state.studio_repository.set_image_override(
            run_id,
            question_id,
            enabled,
        )
    except KeyError as error:
        raise HTTPException(404, "Studio run was not found") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return JSONResponse(
        _review_payload(review),
        headers={"Cache-Control": "no-store"},
    )


@router.put("/runs/{run_id}/questions/{question_id}/image-override")
def set_image_override(
    request: Request,
    run_id: str,
    question_id: str,
) -> JSONResponse:
    return _set_override(request, run_id, question_id, True)


@router.delete("/runs/{run_id}/questions/{question_id}/image-override")
def clear_image_override(
    request: Request,
    run_id: str,
    question_id: str,
) -> JSONResponse:
    return _set_override(request, run_id, question_id, False)


def _resolved_review(request: Request, run_id: str) -> StudioQuizReview:
    review = _require_review(request, run_id)
    if not review.resolved:
        raise HTTPException(
            409,
            "quiz images are still required: " + ", ".join(review.unresolved_keys),
        )
    return review


def _preview_image_urls(review: StudioQuizReview) -> dict[str, tuple[str, str]]:
    active_keys = {
        requirement.key
        for requirement in image_requirements(
            _replace_overridden_image_refs(review)
        )
    }
    return {
        requirement.image_key: (
            f"/studio/runs/{review.run.id}/preview/media/{requirement.image_key}",
            requirement.description,
        )
        for requirement in review.requirements
        if requirement.image_key in active_keys and requirement.image is not None
    }


def _replace_overridden_image_refs(review: StudioQuizReview) -> NativeQuiz:
    return replace(
        review.quiz,
        questions=tuple(
            replace(question, image_ref=None)
            if question.id in review.overridden_question_ids
            else question
            for question in review.quiz.questions
        ),
    )


@router.get("/runs/{run_id}/preview", response_class=HTMLResponse)
def preview_quiz_page(request: Request, run_id: str) -> HTMLResponse:
    review = _resolved_review(request, run_id)
    return templates.TemplateResponse(
        request=request,
        name="studio_quiz_preview.html",
        context={
            "run": review.run,
            "content_url": f"/studio/runs/{run_id}/preview/content",
            "answer_url": f"/studio/runs/{run_id}/preview/answer",
            "publish_url": f"/studio/runs/{run_id}/publication",
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/runs/{run_id}/preview/content")
def preview_quiz_content(request: Request, run_id: str) -> JSONResponse:
    review = _resolved_review(request, run_id)
    quiz = _replace_overridden_image_refs(review)
    return JSONResponse(
        {
            "token": f"preview-{run_id}",
            "version": 1,
            "course": review.run.destination_subject,
            "exam_number": review.run.destination_exam_number,
            **public_quiz_content(quiz, _preview_image_urls(review)),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/runs/{run_id}/preview/media/{image_key}")
def preview_quiz_media(
    request: Request,
    run_id: str,
    image_key: str,
) -> FileResponse:
    review = _resolved_review(request, run_id)
    urls = _preview_image_urls(review)
    requirement = next(
        (
            item
            for item in review.requirements
            if item.image_key == image_key and image_key in urls
        ),
        None,
    )
    if requirement is None or requirement.image is None:
        raise HTTPException(404, "quiz image was not found")
    image = requirement.image
    if not image.path.is_file() or sha256_file(image.path) != image.sha256:
        raise HTTPException(404, "quiz image was not found")
    return FileResponse(
        image.path,
        media_type=image.media_type,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/runs/{run_id}/preview/answer")
def preview_quiz_answer(
    request: Request,
    run_id: str,
    submission: PreviewAnswerSubmission,
) -> JSONResponse:
    review = _resolved_review(request, run_id)
    quiz = _replace_overridden_image_refs(review)
    try:
        feedback = grade_answer(
            quiz,
            submission.question_id,
            submission.choice_id,
        )
    except KeyError as error:
        raise HTTPException(404, "quiz question was not found") from error
    return JSONResponse(
        {
            "correct": feedback.correct,
            "correct_choice_id": feedback.correct_choice_id,
            "rationale": feedback.rationale,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/runs/{run_id}/publication")
def publish_reviewed_quiz(request: Request, run_id: str) -> JSONResponse:
    require_form_csrf(request, None)
    repository = cast(
        GenerationRepository,
        request.app.state.generation_repository,
    )
    try:
        published = repository.publish_reviewed_studio_quiz(run_id)
    except KeyError as error:
        raise HTTPException(404, "Studio run was not found") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return JSONResponse(
        {
            "token": published.token,
            "published_url": f"/public/quizzes/{published.token}",
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


@router.post("/sources/image-url", status_code=202)
def add_image_url(
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
        source = _service(request).add_image_url(
            canonical_subject,
            exam_number,
            title,
            url,
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
