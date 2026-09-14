"""PDF exports bound to active publications and exact private library scopes."""

import hashlib
import json
from dataclasses import asdict
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import StringConstraints
from pypdf import PdfReader, PdfWriter
from sqlalchemy import select

from oms_hub.files.trusted_paths import trusted_managed_path
from oms_hub.models import PublishedQuizModel
from oms_hub.study_chat.routes import _owner
from oms_hub.study_generation.domain import PublishedQuizRecord
from oms_hub.study_generation.native_quiz import serialize_native_quiz
from oms_hub.study_generation.practice_domain import QuizContentKind
from oms_hub.study_generation.quiz_export import export_reviewed_quiz, render_published_quiz_pdf
from oms_hub.study_generation.quiz_images import (
    MAX_QUIZ_IMAGE_BYTES,
    SanitizedQuizImage,
    sanitize_quiz_image,
)
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.web.gpt_export_routes import _snapshot

router = APIRouter(prefix="/studio/library/exports")
PublicToken = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_Course = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
_Category = Literal["quizzes", "practice_questions"]


def _repository(request: Request) -> GenerationRepository:
    return cast(GenerationRepository, request.app.state.generation_repository)


def _published_snapshot(
    request: Request,
    token: str,
) -> tuple[PublishedQuizRecord, dict[str, SanitizedQuizImage], str]:
    repository = _repository(request)
    published = repository.published_quiz(token)
    if published is None:
        raise HTTPException(404, "Published quiz is unavailable.")
    media = repository.published_quiz_media(token)
    keys = {q.image_ref.key for q in published.quiz.questions if q.image_ref}
    if keys != {item.image_key for item in media}:
        raise ValueError("published image mapping changed")
    images = {}
    for item in media:
        if not trusted_managed_path(
            item.path, request.app.state.settings.data_dir.resolve(), require_regular_file=True
        ):
            raise ValueError("published image unavailable")
        with item.path.open("rb") as stream:
            raw = stream.read(MAX_QUIZ_IMAGE_BYTES + 1)
        image = sanitize_quiz_image(raw)
        if image.payload != raw or image.sha256 != item.sha256:
            raise ValueError("published image changed")
        images[item.image_key] = image
    identity = hashlib.sha256(
        json.dumps(
            [asdict(published), [asdict(item) for item in media]],
            default=str,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return published, images, identity


def _response(payload: bytes, filename: str) -> Response:
    return Response(
        payload,
        media_type="application/pdf",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


def public_quiz_pdf(request: Request, token: str) -> Response:
    """Public token exposes only its published quiz, explanations and media."""
    try:
        published, images, identity = _published_snapshot(request, token)
        payload = render_published_quiz_pdf(
            published.quiz, images, f"Publication {published.token}, version {published.version}."
        )
        if _published_snapshot(request, token)[2] != identity:
            raise ValueError("quiz changed during export")
    except (ValueError, KeyError, OSError):
        raise HTTPException(
            409, "Published quiz or images changed or cannot be exported. No PDF was returned."
        ) from None
    return _response(payload, f"quiz-{token}.pdf")


def _members(request: Request, course: str, exam: int, category: _Category) -> tuple[str, ...]:
    repository = _repository(request)
    kinds = (
        [QuizContentKind.PRACTICE_QUESTIONS.value]
        if category == "practice_questions"
        else [QuizContentKind.LECTURE_QUIZ.value, QuizContentKind.EXAM_REVIEW.value]
    )
    course_key = " ".join(course.casefold().split())
    with repository.database.session() as session:
        # Filter metadata before parsing: an unrelated malformed quiz must not
        # enter this course/exam bundle or block an otherwise valid scope.
        candidates = session.scalars(
            select(PublishedQuizModel).where(
                PublishedQuizModel.active.is_(True),
                PublishedQuizModel.content_kind.in_(kinds),
            )
        ).all()
        members = [
            model
            for model in candidates
            if repository._published_quiz_scope(session, model) == (course_key, exam)
        ]
        members.sort(key=lambda model: repository._published_quiz_order_key(session, model))
        return tuple(model.token for model in members)


@router.get("/pdf")
def exam_pdf(
    request: Request,
    course: _Course,
    exam: Annotated[int, Query(ge=1, le=100)],
    category: _Category,
) -> Response:
    owner = _owner(request)
    try:
        tokens = _members(request, course, exam, category)
        if not tokens:
            raise HTTPException(404, "No published quizzes exist in this course, exam and library.")
        writer = PdfWriter()
        identities: dict[str, str] = {}
        gpt_identities: dict[str, str] = {}
        for token in tokens:
            published, images, identities[token] = _published_snapshot(request, token)
            run = (
                request.app.state.studio_repository.get_run(published.studio_run_id)
                if published.studio_run_id
                else None
            )
            if run and run.backend == "codex_subscription":
                quiz, paths, provenance, gpt_identities[token] = _snapshot(request, run.id, owner)
                if serialize_native_quiz(quiz) != serialize_native_quiz(published.quiz):
                    raise ValueError("reviewed and published payloads differ")
                with TemporaryDirectory(prefix="oms-quiz-pdf-") as folder:
                    pdf = export_reviewed_quiz(quiz, paths, provenance, Path(folder).resolve())[2]
                    payload = pdf.read_bytes()
            else:
                payload = render_published_quiz_pdf(
                    published.quiz,
                    images,
                    f"Publication {published.token}, version {published.version}.",
                )
            writer.append(PdfReader(BytesIO(payload)), outline_item=published.title)
        writer.add_metadata(
            {
                "/Title": f"{course} · Exam {exam} · {category.replace('_', ' ')}",
                "/Subject": f"Exact published library order; {len(tokens)} quizzes",
            }
        )
        buffer = BytesIO()
        writer.write(buffer)
        if _members(request, course, exam, category) != tokens:
            raise ValueError("library membership or order changed during export")
        for token in tokens:
            published, _, identity = _published_snapshot(request, token)
            if identities[token] != identity:
                raise ValueError("published content changed during export")
            if token in gpt_identities:
                run_id = published.studio_run_id
                if run_id is None or _snapshot(request, run_id, owner)[3] != gpt_identities[token]:
                    raise ValueError("reviewed lecture sources changed during export")
    except PermissionError:
        raise HTTPException(403, "A quiz in this scope is not available to this owner.") from None
    except (ValueError, KeyError, OSError):
        raise HTTPException(
            409,
            "A quiz or source in this course/exam changed or cannot be exported. "
            "No quizzes were omitted; no PDF was returned.",
        ) from None
    return _response(buffer.getvalue(), f"exam-{exam}-{category}.pdf")
