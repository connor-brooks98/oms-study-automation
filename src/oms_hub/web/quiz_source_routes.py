"""Private, graded-attempt source evidence; never part of the public quiz API."""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select

from oms_hub.document_processing.domain import SourceSnapshot
from oms_hub.files.trusted_paths import trusted_managed_path
from oms_hub.models import StudioRunModel, StudySessionQuestionModel
from oms_hub.study_chat.contracts import UuidId
from oms_hub.study_chat.routes import _owner
from oms_hub.study_generation.gpt_lecture import quiz_instruction_documents
from oms_hub.study_generation.service import GptLectureService
from oms_hub.study_generation.studio_repository import StudioRepository
from oms_hub.study_progress.routes import _sessions
from oms_hub.study_progress.sessions import _owned
from oms_hub.web.gpt_export_routes import _snapshot

router = APIRouter(prefix="/study/sessions")
_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
_UNAVAILABLE = "Source preview unavailable for this quiz."
_STALE = "Source preview unavailable: the saved question or lecture sources have changed."


@dataclass(frozen=True)
class Evidence:
    rows: tuple[dict[str, object], ...]
    sources: tuple[SourceSnapshot, ...]
    identity: str


def _evidence(request: Request, session_id: str, attempt_id: str, owner: str) -> Evidence:
    repository = cast(StudioRepository, request.app.state.studio_repository)
    sessions = _sessions(request)
    with repository.database.session() as session:
        _owned(session, session_id, owner)
        attempt = session.get(StudySessionQuestionModel, attempt_id)
        if attempt is None or attempt.session_id != session_id:
            raise PermissionError("Attempt unavailable")
        if attempt.selected_answer_json is None or attempt.bank_attempt_id is None:
            raise HTTPException(
                409, "Answer this question before opening its sources.", headers=_HEADERS
            )
        publication = sessions._publication(session, owner, attempt.quiz_token)
        sessions._unchanged(attempt, publication)
        question_id = attempt.question_id
        if question_id not in {question.id for question in publication.quiz.questions}:
            raise ValueError("Question unavailable")
        run = session.scalar(
            select(StudioRunModel).where(
                StudioRunModel.published_token == publication.token,
                StudioRunModel.backend == "codex_subscription",
            )
        )
        if run is None:
            return Evidence((), (), "legacy")
        run_id = run.id
    quiz, _, provenance, identity = _snapshot(request, run_id, owner)
    if question_id not in {question.id for question in quiz.questions}:
        raise ValueError("Question evidence unavailable")
    service = cast(GptLectureService, request.app.state.gpt_lecture_service)
    inputs = service.load_inputs(repository.get_run(run_id))
    documents = {document.source_id: document for document in quiz_instruction_documents(inputs)}
    bindings = {binding.snapshot.id: binding.snapshot for binding in inputs.bindings}
    questions = cast(dict[str, dict[str, object]], provenance["questions"])
    refs = cast(list[dict[str, str]], questions[question_id]["source_refs"])
    if not refs or len(refs) > 50:
        return Evidence((), (), identity)
    rows, sources = [], []
    for index, ref in enumerate(refs):
        document = documents[ref["source_id"]]
        segment = next(
            segment for segment in document.segments if segment.key == ref["segment_key"]
        )
        source = bindings[document.source_id]
        if segment.locator.label != ref["locator"]:
            raise ValueError("Source locator changed")
        if not trusted_managed_path(
            source.path, request.app.state.settings.data_dir.resolve(), require_regular_file=True
        ):
            raise ValueError("Source path unavailable")
        url = f"/study/sessions/{session_id}/sources/{attempt_id}/{index}/original"
        # Only native PDF pages have a reliable browser page address. A reading PDF
        # reflowed from Word/text is not the source and must not invent page citations.
        if document.source_format == "pdf" and segment.locator.page_number:
            url += f"#page={segment.locator.page_number}"
        rows.append(
            {
                "source_id": document.source_id,
                "segment_key": segment.key,
                "title": source.title[:300],
                "locator": segment.locator.label[:300],
                "location": (f"Slide {segment.locator.slide_number}"
                             if segment.locator.slide_number is not None
                             else f"Page {segment.locator.page_number}"
                             if segment.locator.page_number is not None else "Source excerpt"),
                "excerpt": segment.text[:2000],
                "truncated": len(segment.text) > 2000,
                "original_url": url,
                "link_label": "Open source PDF"
                if document.source_format == "pdf"
                else "Download source",
            }
        )
        sources.append(source)
    if _snapshot(request, run_id, owner)[3] != identity:
        raise ValueError("Source review changed")
    with repository.database.session() as session:
        _owned(session, session_id, owner)
        current = session.get(StudySessionQuestionModel, attempt_id)
        if current is None or current.session_id != session_id:
            raise PermissionError("Attempt unavailable")
        sessions._unchanged(current, sessions._publication(session, owner, current.quiz_token))
        if current.question_id != question_id:
            raise ValueError("Question changed during source lookup")
    return Evidence(tuple(rows), tuple(sources), identity)


@router.get("/{session_id}/sources/{attempt_id}")
def question_sources(request: Request, session_id: UuidId, attempt_id: UuidId) -> JSONResponse:
    owner = _owner(request)
    try:
        evidence = _evidence(request, session_id, attempt_id, owner)
    except PermissionError:
        raise HTTPException(403, "Session sources unavailable.", headers=_HEADERS) from None
    except (KeyError, ValueError, OSError, StopIteration):
        return JSONResponse(
            {"available": False, "message": _STALE, "sources": []}, headers=_HEADERS
        )
    return JSONResponse(
        {
            "available": bool(evidence.rows),
            "sources": evidence.rows,
            "message": "" if evidence.rows else _UNAVAILABLE,
        },
        headers=_HEADERS,
    )


@router.get("/{session_id}/sources/{attempt_id}/{source_index}/original")
def source_original(
    request: Request, session_id: UuidId, attempt_id: UuidId, source_index: int
) -> Response:
    owner = _owner(request)
    try:
        evidence = _evidence(request, session_id, attempt_id, owner)
        if not 0 <= source_index < len(evidence.sources):
            raise ValueError("Source unavailable")
        source = evidence.sources[source_index]
        if source.path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("Source is too large for preview")
        payload = source.path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != source.sha256:
            raise ValueError("Source changed")
        current = _evidence(request, session_id, attempt_id, owner)
        if current != evidence:
            raise ValueError("Source changed during read")
    except PermissionError:
        raise HTTPException(403, "Session sources unavailable.", headers=_HEADERS) from None
    except (KeyError, ValueError, OSError, StopIteration):
        raise HTTPException(409, _STALE, headers=_HEADERS) from None
    suffix = Path(source.path).suffix.lower()
    # Only trusted PDF bytes render inline; other originals are opaque downloads.
    pdf = source.media_type == "application/pdf" and payload.startswith(b"%PDF-")
    safe_suffix = suffix if suffix in {".pdf", ".pptx", ".docx", ".txt", ".md", ".rtf"} else ".bin"
    disposition = "inline" if pdf else "attachment"
    return Response(
        payload,
        media_type="application/pdf" if pdf else "application/octet-stream",
        headers=_HEADERS
        | {"Content-Disposition": f'{disposition}; filename="lecture-source{safe_suffix}"'},
    )
