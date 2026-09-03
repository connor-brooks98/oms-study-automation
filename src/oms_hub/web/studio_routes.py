import hashlib
from dataclasses import asdict, replace
from pathlib import Path
from typing import Annotated, Literal, cast

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from oms_hub.files.atomic import sha256_file
from oms_hub.study_generation.domain import NativeQuiz
from oms_hub.study_generation.native_quiz import (
    grade_answer,
    grade_matching_answer,
    image_requirements,
    public_quiz_content,
    serialize_native_quiz,
)
from oms_hub.study_generation.practice_domain import (
    ImportSourceRole,
    ImportSourceSelection,
    MatchingQuestionDraft,
    QuizContentKind,
    QuizWorkflowKind,
)
from oms_hub.study_generation.practice_review import (
    ImageCandidate,
    PracticeReviewService,
    ReviewArtifactUnavailable,
    ReviewQuestion,
)
from oms_hub.study_generation.quiz_images import (
    MAX_QUIZ_IMAGE_BYTES,
    QuizImageError,
    StudioQuizImageService,
)
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.study_generation.studio_domain import (
    StudioQuizReview,
    StudioRun,
    StudioRunState,
    StudioSourceState,
)
from oms_hub.study_generation.studio_repository import StudioRepository
from oms_hub.study_generation.studio_service import StudioService
from oms_hub.web.csrf import require_form_csrf
from oms_hub.web.public_quiz_routes import (
    _player_asset_version,
    practice_question_library_response,
    quiz_library_response,
)

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


class ImportRunSourceInput(BaseModel):
    source_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f-]{36}$")]
    role: ImportSourceRole
    attach_to_notebook: bool = False


class ImportRunInput(BaseModel):
    subject: str = Field(min_length=1, max_length=100)
    exam_number: int = Field(ge=1)
    label: str = Field(min_length=1, max_length=300)
    destination_subject: str = Field(min_length=1, max_length=100)
    destination_exam_number: int = Field(ge=1)
    content_kind: QuizContentKind = QuizContentKind.PRACTICE_QUESTIONS
    sources: list[ImportRunSourceInput] = Field(min_length=1, max_length=50)


class PreviewAnswerSubmission(BaseModel):
    question_id: str = Field(pattern=r"^q[0-9]{1,3}$", max_length=4)
    choice_id: str = Field(pattern=r"^c[0-9]{1,2}$", max_length=3)


class MatchingPreviewAnswerSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["matching"]
    question_id: str = Field(pattern=r"^q[0-9]{1,3}$", max_length=4)
    matches: dict[str, str] = Field(min_length=2, max_length=8)


class QuestionEditInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stem: str | None = Field(default=None, max_length=10_000)
    choices: list[str] | None = Field(default=None, min_length=2, max_length=8)
    correct_index: int | None = Field(default=None, ge=0, le=7)
    rationale: str | None = Field(default=None, max_length=20_000)
    topic: str | None = Field(default=None, max_length=300)
    area: str | None = Field(default=None, max_length=300)
    learning_objective: str | None = Field(default=None, max_length=1_000)


class MatchingPromptEditInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Annotated[str, StringConstraints(pattern=r"^p[1-8]$", max_length=2)]
    label: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=10_000)
    correct_index: int | None = Field(default=None, ge=0, le=7)


class MatchingQuestionEditInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["matching"]
    stem: str = Field(min_length=1, max_length=10_000)
    prompts: list[MatchingPromptEditInput] = Field(min_length=2, max_length=8)
    choices: list[str] = Field(min_length=2, max_length=8)
    rationale: str | None = Field(default=None, max_length=20_000)
    topic: str | None = Field(default=None, max_length=300)
    area: str | None = Field(default=None, max_length=300)
    learning_objective: str | None = Field(default=None, max_length=1_000)


class CandidateSelectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_candidate_id: str | None = Field(default=None, min_length=1, max_length=100)


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


@router.get("/library/quizzes", response_class=HTMLResponse)
def managed_quiz_library(request: Request) -> HTMLResponse:
    return quiz_library_response(request, management_mode=True)


@router.get("/library/practice-questions", response_class=HTMLResponse)
def managed_practice_question_library(request: Request) -> HTMLResponse:
    return practice_question_library_response(request, management_mode=True)


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
                    "purpose": item.purpose.value,
                    "import_defaults": (
                        {
                            "role": item.import_role.value if item.import_role else None,
                            "attach_to_notebook": item.import_attach_to_notebook,
                        }
                        if item.purpose.value == "local_import"
                        else None
                    ),
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
                    "source_ids": [source.source_id for source in item.sources],
                    "workflow_kind": item.workflow_kind.value,
                    "content_kind": item.content_kind.value,
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
                    "review_url": (
                        f"/studio/runs/{item.id}/review"
                        if item.workflow_kind is QuizWorkflowKind.DIRECT_IMPORT
                        and item.state.value == "awaiting_review"
                        else None
                    ),
                    "attempt_history": [
                        {
                            "attempt_number": attempt.attempt_number,
                            "diagnostic_source": attempt.diagnostic_source,
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
def image_review_page(request: Request, run_id: str) -> RedirectResponse:
    return RedirectResponse(f"/studio/runs/{run_id}/review", status_code=307)


def _practice_review(request: Request) -> PracticeReviewService:
    return cast(PracticeReviewService, request.app.state.practice_review)


def _direct_import_review_run(request: Request, run_id: str) -> StudioRun:
    try:
        run = cast(StudioRepository, request.app.state.studio_repository).get_run(run_id)
    except KeyError as error:
        raise HTTPException(404, "Studio run was not found") from error
    if run.workflow_kind is not QuizWorkflowKind.DIRECT_IMPORT:
        raise HTTPException(404, "imported question review is not available for this run")
    if run.state is not StudioRunState.AWAITING_REVIEW:
        raise HTTPException(409, "imported quiz is not awaiting question review")
    return run


def _review_question_payload(
    question: ReviewQuestion,
    candidates: tuple[ImageCandidate, ...] = (),
    selected_candidate_id: str | None = None,
    *,
    run_id: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": question.draft.question_id,
        "original_identifier": question.draft.original_identifier,
        "stem": question.draft.stem,
        "choices": list(question.draft.choices),
        "rationale": question.draft.rationale,
        "provenance": question.draft.answer_provenance.value if question.draft.answer_provenance else None,  # noqa: E501
        "verification_required": question.draft.verification_required,
        "verified_at": question.draft.verified_at,
        "confidence": question.draft.extraction_confidence,
        "source_refs": [
            {
                "source_id": ref.source_id,
                "segment_key": ref.segment_key,
                "locator": ref.locator,
            }
            for ref in question.draft.source_refs
        ],
        "topic": question.topic,
        "area": question.area,
        "learning_objective": question.learning_objective,
        "selected_candidate_id": selected_candidate_id,
        "image_required": question.draft.image_ref is not None,
        "image_not_needed": question.image_not_needed,
        "image_attached": question.chosen_image is not None,
        "image_preview_url": (
            f"/studio/runs/{run_id}/questions/{question.draft.question_id}/image/preview"
            if run_id is not None and question.chosen_image is not None
            else None
        ),
        "candidates": [
            {
                "candidate_id": item.candidate_id,
                "question_id": item.question_id,
                "source_id": item.source_id,
                "source_title": item.source_title,
                "asset_key": item.asset_key,
                "locator": item.locator,
                "origin": item.origin,
                "media_type": item.media_type,
                "width": item.width,
                "height": item.height,
                "score": item.score,
                "exact_match": item.exact_match,
                "preview_url": (
                    f"/studio/runs/{run_id}/questions/{question.draft.question_id}"
                    f"/candidates/{item.candidate_id}/preview"
                    if run_id is not None
                    else None
                ),
            }
            for item in candidates
        ],
    }
    if isinstance(question.draft, MatchingQuestionDraft):
        payload.update(
            {
                "kind": "matching",
                "prompts": [
                    {
                        "id": item.id,
                        "label": item.label,
                        "text": item.text,
                        "correct_index": item.correct_index,
                    }
                    for item in question.draft.prompts
                ],
            }
        )
    else:
        payload["correct_index"] = question.draft.correct_index
    return payload


def _direct_review_payload(request: Request, run_id: str) -> dict[str, object]:
    service = _practice_review(request)
    questions = service.review(run_id)
    issues = service.issues(run_id)
    blockers = service.blockers(run_id)
    candidates = service.candidates_by_question(run_id, questions)
    return {
        "run_id": run_id,
        "blockers": list(blockers),
        "run_diagnostics": list(service.run_diagnostics(run_id)),
        "issues": [
            {
                "question_id": issue.question_id,
                "original_identifier": issue.original_identifier,
                "display_label": issue.display_label,
                "type": issue.issue_type,
                "code": issue.code,
                "message": issue.message,
                "role": "err" if issue.severity.value == "blocker" else "warn",
            }
            for issue in issues
        ],
        "preview_url": f"/studio/runs/{run_id}/preview" if not blockers else None,
        "publish_url": f"/studio/runs/{run_id}/publication",
        "questions": [
            _review_question_payload(
                question,
                candidates.get(question.draft.question_id, ()),
                question.selected_candidate_id,
                run_id=run_id,
            )
            for question in questions
        ],
    }


@router.post("/runs/{run_id}/run-diagnostics/{code}/acknowledgement")
def acknowledge_run_diagnostic(request: Request, run_id: str, code: str) -> Response:
    require_form_csrf(request, None)
    _direct_import_review_run(request, run_id)
    try:
        _practice_review(request).acknowledge_run_diagnostic(run_id, code)
        return JSONResponse(
            _direct_review_payload(request, run_id),
            headers={"Cache-Control": "no-store"},
        )
    except KeyError as error:
        raise HTTPException(404, "run diagnostic was not found") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


def _review_artifact_unavailable_response(error: ReviewArtifactUnavailable) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": "review_artifact_unavailable",
                "message": str(error),
                "recovery": "Return to Quiz Builder and rerun the import.",
            }
        },
        status_code=409,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/runs/{run_id}/review", response_class=HTMLResponse)
def practice_review_page(request: Request, run_id: str) -> Response:
    try:
        run = cast(StudioRepository, request.app.state.studio_repository).get_run(run_id)
    except KeyError as error:
        raise HTTPException(404, "Studio run was not found") from error
    if run.workflow_kind is QuizWorkflowKind.NOTEBOOK_GENERATION:
        review = _require_review(request, run_id)
        return templates.TemplateResponse(
            request=request,
            name="studio_quiz_images.html",
            context={"run": review.run},
            headers={"Cache-Control": "no-store"},
        )
    _direct_import_review_run(request, run_id)
    try:
        _practice_review(request).review(run_id)
    except ReviewArtifactUnavailable as error:
        return _review_artifact_unavailable_response(error)
    return templates.TemplateResponse(
        request=request,
        name="studio_quiz_review.html",
        context={"run": run, "review_data_url": f"/studio/runs/{run_id}/review/data"},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/runs/{run_id}/review/data")
def practice_review_data(request: Request, run_id: str) -> JSONResponse:
    _direct_import_review_run(request, run_id)
    try:
        return JSONResponse(
            _direct_review_payload(request, run_id), headers={"Cache-Control": "no-store"}
        )
    except ReviewArtifactUnavailable as error:
        return _review_artifact_unavailable_response(error)


@router.patch("/runs/{run_id}/questions/{question_id}")
def update_practice_question(
    request: Request,
    run_id: str,
    question_id: str,
    submission: QuestionEditInput | MatchingQuestionEditInput,
) -> Response:
    require_form_csrf(request, None)
    _direct_import_review_run(request, run_id)
    try:
        _practice_review(request).update_question(
            run_id, question_id, submission.model_dump(exclude_unset=True)
        )
        return JSONResponse(
            _direct_review_payload(request, run_id),
            headers={"Cache-Control": "no-store"},
        )
    except ReviewArtifactUnavailable as error:
        return _review_artifact_unavailable_response(error)
    except KeyError as error:
        raise HTTPException(404, "Studio question was not found") from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post("/runs/{run_id}/questions/{question_id}/verify-answer")
def verify_practice_answer(request: Request, run_id: str, question_id: str) -> Response:
    require_form_csrf(request, None)
    _direct_import_review_run(request, run_id)
    try:
        _practice_review(request).verify_generated_answer(run_id, question_id)
        return JSONResponse(
            _direct_review_payload(request, run_id),
            headers={"Cache-Control": "no-store"},
        )
    except ReviewArtifactUnavailable as error:
        return _review_artifact_unavailable_response(error)
    except KeyError as error:
        raise HTTPException(404, "Studio question was not found") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.post("/runs/{run_id}/questions/{question_id}/image-selection")
def select_practice_image_candidate(
    request: Request,
    run_id: str,
    question_id: str,
    submission: CandidateSelectionInput,
) -> Response:
    require_form_csrf(request, None)
    _direct_import_review_run(request, run_id)
    if submission.image_candidate_id is None:
        try:
            question = _practice_review(request).question(run_id, question_id)
        except ReviewArtifactUnavailable as error:
            return _review_artifact_unavailable_response(error)
        except KeyError as error:
            raise HTTPException(404, "Studio question was not found") from error
        if question.draft.image_ref is not None:
            raise HTTPException(409, "this question still requires an image")
        return JSONResponse(
            _direct_review_payload(request, run_id),
            headers={"Cache-Control": "no-store"},
        )
    try:
        _practice_review(request).select_image_candidate(
            run_id,
            question_id,
            submission.image_candidate_id,
        )
        return JSONResponse(
            _direct_review_payload(request, run_id),
            headers={"Cache-Control": "no-store"},
        )
    except ReviewArtifactUnavailable as error:
        return _review_artifact_unavailable_response(error)
    except KeyError as error:
        raise HTTPException(404, "Studio question was not found") from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post("/runs/{run_id}/questions/{question_id}/image")
def upload_practice_question_image(
    request: Request,
    run_id: str,
    question_id: str,
    file: Annotated[UploadFile, File()],
    csrf_token: Annotated[str | None, Form()] = None,
) -> Response:
    require_form_csrf(request, csrf_token)
    _direct_import_review_run(request, run_id)
    payload = file.file.read(MAX_QUIZ_IMAGE_BYTES + 1)
    try:
        _practice_review(request).upload_image(
            run_id,
            question_id,
            file.filename or "question-image",
            payload,
        )
        return JSONResponse(
            _direct_review_payload(request, run_id),
            headers={"Cache-Control": "no-store"},
        )
    except ReviewArtifactUnavailable as error:
        return _review_artifact_unavailable_response(error)
    except KeyError as error:
        raise HTTPException(404, "Studio question was not found") from error
    except (QuizImageError, ValueError) as error:
        raise HTTPException(422, str(error)) from error


@router.get("/runs/{run_id}/questions/{question_id}/image/preview")
def preview_practice_question_image(
    request: Request, run_id: str, question_id: str
) -> Response:
    _direct_import_review_run(request, run_id)
    try:
        question = _practice_review(request).question(run_id, question_id)
        if question.chosen_image is None:
            raise KeyError(question_id)
        image = cast(
            StudioRepository, request.app.state.studio_repository
        ).import_review_image(run_id, question.chosen_image.key)
    except ReviewArtifactUnavailable as error:
        return _review_artifact_unavailable_response(error)
    except KeyError as error:
        raise HTTPException(404, "quiz image was not found") from error
    return FileResponse(
        image.path,
        media_type=image.media_type,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/runs/{run_id}/questions/{question_id}/candidates/{candidate_id}/preview")
def preview_practice_image_candidate(
    request: Request,
    run_id: str,
    question_id: str,
    candidate_id: str,
) -> Response:
    _direct_import_review_run(request, run_id)
    try:
        path, media_type = _practice_review(request).candidate_preview(
            run_id,
            question_id,
            candidate_id,
        )
    except ReviewArtifactUnavailable as error:
        return _review_artifact_unavailable_response(error)
    except KeyError as error:
        raise HTTPException(404, "image candidate was not found") from error
    return FileResponse(
        path,
        media_type=media_type,
        filename="source-image",
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store"},
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
    try:
        run = cast(StudioRepository, request.app.state.studio_repository).get_run(run_id)
    except KeyError as error:
        raise HTTPException(404, "Studio run was not found") from error
    if run.workflow_kind is QuizWorkflowKind.DIRECT_IMPORT:
        require_form_csrf(request, None)
        _direct_import_review_run(request, run_id)
        try:
            _practice_review(request).set_image_not_needed(
                run_id, question_id, enabled
            )
        except ReviewArtifactUnavailable as error:
            return _review_artifact_unavailable_response(error)
        except KeyError as error:
            raise HTTPException(404, "Studio question was not found") from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return JSONResponse(
            _direct_review_payload(request, run_id),
            headers={"Cache-Control": "no-store"},
        )
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


def _direct_preview_quiz(request: Request, run_id: str) -> tuple[StudioRun, NativeQuiz]:
    run = _direct_import_review_run(request, run_id)
    try:
        quiz = _practice_review(request).to_native_quiz(run_id, title=run.label)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return run, quiz


def _preview_version(quiz: NativeQuiz) -> str:
    return f"preview:{hashlib.sha256(serialize_native_quiz(quiz).encode('utf-8')).hexdigest()}"


def _direct_preview_image_urls(
    request: Request,
    run: StudioRun,
    quiz: NativeQuiz,
) -> dict[str, tuple[str, str, int | None, int | None]]:
    repository = cast(StudioRepository, request.app.state.studio_repository)
    urls: dict[str, tuple[str, str, int | None, int | None]] = {}
    for image_key in {
        question.image_ref.key for question in quiz.questions if question.image_ref is not None
    }:
        try:
            image = repository.import_review_image(run.id, image_key)
        except KeyError as error:
            raise HTTPException(404, "quiz image was not found") from error
        urls[image_key] = (
            f"/studio/runs/{run.id}/preview/media/{image_key}",
            "Question image",
            image.width,
            image.height,
        )
    return urls


def _preview_image_urls(
    review: StudioQuizReview,
) -> dict[str, tuple[str, str, int | None, int | None]]:
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
            requirement.image.width if requirement.image is not None else None,
            requirement.image.height if requirement.image is not None else None,
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
def preview_quiz_page(request: Request, run_id: str) -> Response:
    try:
        run = cast(StudioRepository, request.app.state.studio_repository).get_run(run_id)
    except KeyError as error:
        raise HTTPException(404, "Studio run was not found") from error
    if run.workflow_kind is QuizWorkflowKind.DIRECT_IMPORT:
        try:
            direct_run, quiz = _direct_preview_quiz(request, run_id)
        except ReviewArtifactUnavailable as error:
            return _review_artifact_unavailable_response(error)
        return templates.TemplateResponse(
            request=request,
            name="studio_quiz_preview.html",
            context={
                "run": direct_run,
                "content_url": f"/studio/runs/{run_id}/preview/content",
                "answer_url": f"/studio/runs/{run_id}/preview/answer",
                "publish_url": f"/studio/runs/{run_id}/publication",
                "player_asset_version": _player_asset_version(),
                "preview_version": _preview_version(quiz),
            },
            headers={"Cache-Control": "no-store"},
        )
    review = _resolved_review(request, run_id)
    return templates.TemplateResponse(
        request=request,
        name="studio_quiz_preview.html",
        context={
            "run": review.run,
            "content_url": f"/studio/runs/{run_id}/preview/content",
            "answer_url": f"/studio/runs/{run_id}/preview/answer",
            "publish_url": f"/studio/runs/{run_id}/publication",
            "player_asset_version": _player_asset_version(),
            "preview_version": _preview_version(_replace_overridden_image_refs(review)),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/runs/{run_id}/preview/content")
def preview_quiz_content(request: Request, run_id: str) -> JSONResponse:
    try:
        run = cast(StudioRepository, request.app.state.studio_repository).get_run(run_id)
    except KeyError as error:
        raise HTTPException(404, "Studio run was not found") from error
    if run.workflow_kind is QuizWorkflowKind.DIRECT_IMPORT:
        try:
            direct_run, quiz = _direct_preview_quiz(request, run_id)
        except ReviewArtifactUnavailable as error:
            return _review_artifact_unavailable_response(error)
        return JSONResponse(
            {
                "token": f"preview-{run_id}",
                "version": _preview_version(quiz),
                "course": direct_run.destination_subject,
                "exam_number": direct_run.destination_exam_number,
                **public_quiz_content(quiz, _direct_preview_image_urls(request, direct_run, quiz)),
            },
            headers={"Cache-Control": "no-store"},
        )
    review = _resolved_review(request, run_id)
    quiz = _replace_overridden_image_refs(review)
    return JSONResponse(
        {
            "token": f"preview-{run_id}",
            "version": _preview_version(quiz),
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
) -> Response:
    try:
        run = cast(StudioRepository, request.app.state.studio_repository).get_run(run_id)
    except KeyError as error:
        raise HTTPException(404, "Studio run was not found") from error
    if run.workflow_kind is QuizWorkflowKind.DIRECT_IMPORT:
        try:
            direct_run, quiz = _direct_preview_quiz(request, run_id)
        except ReviewArtifactUnavailable as error:
            return _review_artifact_unavailable_response(error)
        active_keys = {
            question.image_ref.key for question in quiz.questions if question.image_ref is not None
        }
        if image_key not in active_keys:
            raise HTTPException(404, "quiz image was not found")
        try:
            image = cast(StudioRepository, request.app.state.studio_repository).import_review_image(
                direct_run.id,
                image_key,
            )
        except KeyError as error:
            raise HTTPException(404, "quiz image was not found") from error
        return FileResponse(
            image.path,
            media_type=image.media_type,
            headers={"Cache-Control": "no-store"},
        )
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
    submission: PreviewAnswerSubmission | MatchingPreviewAnswerSubmission,
) -> Response:
    try:
        run = cast(StudioRepository, request.app.state.studio_repository).get_run(run_id)
    except KeyError as error:
        raise HTTPException(404, "Studio run was not found") from error
    if run.workflow_kind is QuizWorkflowKind.DIRECT_IMPORT:
        try:
            _, quiz = _direct_preview_quiz(request, run_id)
        except ReviewArtifactUnavailable as error:
            return _review_artifact_unavailable_response(error)
        try:
            if isinstance(submission, MatchingPreviewAnswerSubmission):
                return JSONResponse(
                    asdict(
                        grade_matching_answer(
                            quiz, submission.question_id, submission.matches
                        )
                    ),
                    headers={"Cache-Control": "no-store"},
                )
            feedback = grade_answer(quiz, submission.question_id, submission.choice_id)
        except KeyError as error:
            raise HTTPException(404, "quiz question was not found") from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return JSONResponse(
            {
                "correct": feedback.correct,
                "correct_choice_id": feedback.correct_choice_id,
                "rationale": feedback.rationale,
            },
            headers={"Cache-Control": "no-store"},
        )
    review = _resolved_review(request, run_id)
    quiz = _replace_overridden_image_refs(review)
    try:
        if isinstance(submission, MatchingPreviewAnswerSubmission):
            return JSONResponse(
                asdict(
                    grade_matching_answer(
                        quiz, submission.question_id, submission.matches
                    )
                ),
                headers={"Cache-Control": "no-store"},
            )
        feedback = grade_answer(
            quiz,
            submission.question_id,
            submission.choice_id,
        )
    except KeyError as error:
        raise HTTPException(404, "quiz question was not found") from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
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
    try:
        queued = repository.queue_source_delete(source_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return JSONResponse(
        {"id": queued.id, "state": queued.state.value},
        status_code=202 if queued.state is StudioSourceState.DELETING else 200,
    )


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


@router.delete("/runs/{run_id}", status_code=204)
def remove_run_from_history(request: Request, run_id: str) -> Response:
    require_form_csrf(request, None)
    try:
        request.app.state.studio_repository.hide_run(run_id)
    except KeyError as error:
        raise HTTPException(404, "Studio run was not found") from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return Response(status_code=204)


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


@router.post("/import/runs", status_code=202)
def queue_import_run(request: Request, submission: ImportRunInput) -> JSONResponse:
    require_form_csrf(request, None)
    subject = _validated_subject(request, submission.subject, submission.exam_number)
    destination_subject = _validated_subject(
        request,
        submission.destination_subject,
        submission.destination_exam_number,
    )
    try:
        run = _service(request).queue_import_run(
            subject,
            submission.exam_number,
            submission.label,
            destination_subject,
            submission.destination_exam_number,
            submission.content_kind,
            tuple(
                ImportSourceSelection(
                    source.source_id,
                    source.role,
                    source.attach_to_notebook,
                )
                for source in submission.sources
            ),
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return JSONResponse(
        {"id": run.id, "state": run.state.value, "stage": run.stage.value},
        status_code=202,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/import/sources/file", status_code=202)
def add_import_file(
    request: Request,
    subject: Annotated[str, Form()],
    exam_number: Annotated[int, Form()],
    title: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    role: Annotated[ImportSourceRole, Form()] = ImportSourceRole.QUESTIONS,
    attach_to_notebook: Annotated[bool, Form()] = False,
    csrf_token: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    canonical_subject = _validated_subject(request, subject, exam_number)
    service = _service(request)
    payload = file.file.read(service.max_file_bytes + 1)
    try:
        source = service.add_import_file(
            canonical_subject,
            exam_number,
            title,
            file.filename or "source",
            payload,
            role=role,
            attach_to_notebook=attach_to_notebook,
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return JSONResponse({"id": source.id, "state": source.state.value}, status_code=202)


@router.post("/import/sources/text", status_code=202)
def add_import_text(
    request: Request,
    subject: Annotated[str, Form()],
    exam_number: Annotated[int, Form()],
    title: Annotated[str, Form()],
    text: Annotated[str, Form()],
    role: Annotated[ImportSourceRole, Form()] = ImportSourceRole.QUESTIONS,
    attach_to_notebook: Annotated[bool, Form()] = False,
    csrf_token: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    canonical_subject = _validated_subject(request, subject, exam_number)
    try:
        source = _service(request).add_import_text(
            canonical_subject,
            exam_number,
            title,
            text,
            role=role,
            attach_to_notebook=attach_to_notebook,
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return JSONResponse({"id": source.id, "state": source.state.value}, status_code=202)


@router.post("/import/sources/url", status_code=202)
def add_import_url(
    request: Request,
    subject: Annotated[str, Form()],
    exam_number: Annotated[int, Form()],
    title: Annotated[str, Form()],
    url: Annotated[str, Form()],
    role: Annotated[ImportSourceRole, Form()] = ImportSourceRole.QUESTIONS,
    attach_to_notebook: Annotated[bool, Form()] = False,
    csrf_token: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    canonical_subject = _validated_subject(request, subject, exam_number)
    try:
        source = _service(request).add_import_url(
            canonical_subject,
            exam_number,
            title,
            url,
            role=role,
            attach_to_notebook=attach_to_notebook,
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return JSONResponse({"id": source.id, "state": source.state.value}, status_code=202)


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
