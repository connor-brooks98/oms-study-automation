"""Private exports from the same current, reviewed lecture payload as publication."""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select, text

from oms_hub.files.trusted_paths import prepare_trusted_directory, trusted_managed_path
from oms_hub.models import StudioQuizImageRequirementModel, StudioRunArtifactModel
from oms_hub.study_chat.routes import _owner
from oms_hub.study_generation.domain import NativeQuiz
from oms_hub.study_generation.native_quiz import image_requirements, serialize_native_quiz
from oms_hub.study_generation.practice_review import PracticeReviewService
from oms_hub.study_generation.studio_domain import StudioRunState
from oms_hub.study_generation.studio_repository import StudioRepository

router = APIRouter(prefix="/studio")


def _snapshot(
    request: Request, run_id: str, owner: str,
) -> tuple[NativeQuiz, dict[str, Path], dict[str, object], str]:
    repository = cast(StudioRepository, request.app.state.studio_repository)
    review = cast(PracticeReviewService, request.app.state.practice_review)
    with repository.database.session() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        settings = session.scalar(select(StudioRunArtifactModel).where(
            StudioRunArtifactModel.run_id == run_id,
            StudioRunArtifactModel.artifact_key == "gpt:settings"))
        if settings is None or json.loads(settings.payload_json).get("owner_id") != owner:
            raise PermissionError("lecture export unavailable")
        run = repository.get_run(run_id)
        if run.backend != "codex_subscription" or run.state not in {
            StudioRunState.AWAITING_REVIEW, StudioRunState.COMPLETE,
        }:
            raise ValueError("lecture review is not ready")
        quiz = review.to_native_quiz_in_session(session, run_id, title=run.label)
        questions = review.review(run_id)
        response = session.scalar(select(StudioRunArtifactModel).where(
            StudioRunArtifactModel.run_id == run_id,
            StudioRunArtifactModel.artifact_key == "gpt:response"))
        if response is None:
            raise ValueError("lecture provenance unavailable")
        original = {q["id"]: q for q in json.loads(response.payload_json)["quiz"]["questions"]}
        paths: dict[str, Path] = {}
        hashes: dict[str, str] = {}
        for required in image_requirements(quiz):
            media = session.scalar(select(StudioQuizImageRequirementModel).where(
                StudioQuizImageRequirementModel.run_id == run_id,
                StudioQuizImageRequirementModel.image_key == required.key))
            if media is None or not media.asset_path or not media.asset_sha256:
                raise ValueError("lecture image unavailable")
            path = Path(media.asset_path)
            if not trusted_managed_path(path, request.app.state.settings.data_dir.resolve(),
                                        require_regular_file=True):
                raise ValueError("lecture image path unavailable")
            paths[required.key], hashes[required.key] = path, media.asset_sha256
        provenance: dict[str, object] = {
            "questions": {
                native.id: {"objective_ids": original[item.draft.question_id]["objective_ids"],
                            "source_refs": [asdict(ref) for ref in item.draft.source_refs]}
                for native, item in zip(quiz.questions, questions, strict=True)
            },
            "image_sha256": hashes,
        }
        payload = serialize_native_quiz(quiz)
        if run.state == StudioRunState.COMPLETE:
            publication = request.app.state.generation_repository.published_quiz(
                run.published_token)
            if publication is None or serialize_native_quiz(publication.quiz) != payload:
                raise ValueError("published lecture content changed")
        identity = hashlib.sha256(
            json.dumps([payload, provenance], sort_keys=True).encode()).hexdigest()
        return quiz, paths, provenance, identity


@router.get("/runs/{run_id}/exports/{format}")
def export_lecture(
    request: Request, run_id: str, format: Literal["json", "zip", "pdf"],
) -> Response:
    # Generation writes only private local export artifacts; it never dispatches a provider.
    from oms_hub.study_generation.quiz_export import export_reviewed_quiz

    owner = _owner(request)
    try:
        quiz, images, provenance, identity = _snapshot(request, run_id, owner)
        output = (request.app.state.settings.data_dir.resolve() / "gpt-exports"
                  / hashlib.sha256(run_id.encode()).hexdigest())
        if not prepare_trusted_directory(output):
            raise ValueError("export directory unavailable")
        files = export_reviewed_quiz(quiz, images, provenance, output)
        selected = files[("json", "zip", "pdf").index(format)]
        payload = selected.read_bytes()
        if _snapshot(request, run_id, owner)[3] != identity:
            raise ValueError("lecture changed during export")
    except PermissionError:
        raise HTTPException(403, "Lecture export is unavailable.") from None
    except (KeyError, ValueError, OSError):
        raise HTTPException(409,
            "Review current lecture sources, answers and images before export.") from None
    return Response(payload, media_type={"json": "application/json", "zip": "application/zip",
        "pdf": "application/pdf"}[format], headers={"Cache-Control": "private, no-store",
        "Content-Disposition": f'attachment; filename="{selected.name}"'})
