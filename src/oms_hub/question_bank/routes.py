from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from oms_hub.anki.index import AnkiIndex
from oms_hub.question_bank.anki_links import (
    AnkiCandidate,
    TagRule,
    match_qid_tags,
    parse_candidate_query,
    preview_candidate_notes,
)
from oms_hub.question_bank.contracts import ImportPreview, ImportReceipt, QuestionKey
from oms_hub.question_bank.imports import preview_import
from oms_hub.question_bank.native_review import reviewable_drafts, stage_native_review
from oms_hub.question_bank.repository import BankRepository
from oms_hub.study_generation.studio_repository import StudioRepository
from oms_hub.web.csrf import require_form_csrf

templates = Jinja2Templates(directory=str(Path(__file__).parents[1] / "web" / "templates"))
_MAX_UPLOAD = 10 * 1024 * 1024


def _owner(request: Request) -> str:
    owner = getattr(request.state, "study_owner_id", None)
    if not isinstance(owner, str) or not owner:
        raise HTTPException(401, "Owner sign-in required")
    return owner


async def _upload(file: UploadFile) -> bytes:
    try:
        raw = await file.read(_MAX_UPLOAD + 1)
        if len(raw) > _MAX_UPLOAD:
            raise HTTPException(413, "Import exceeds 10 MiB")
        return raw
    finally:
        await file.close()


def create_question_bank_router(
    repository: BankRepository,
    *,
    studio: StudioRepository | None = None,
    anki_index: AnkiIndex | None = None,
    tag_rules: tuple[TagRule, ...] = (),
) -> APIRouter:
    router = APIRouter(prefix="/question-bank")

    def load(import_id: str, owner: str) -> tuple[ImportPreview, ImportReceipt]:
        try:
            return repository.get_import(import_id=import_id, learner_id=owner)
        except ValueError as error:
            raise HTTPException(404, "Import not found") from error

    def candidates(
        preview: ImportPreview,
    ) -> tuple[tuple[QuestionKey, ...], tuple[AnkiCandidate, ...]]:
        keys = tuple(
            dict.fromkeys(
                QuestionKey(
                    source=preview.envelope.source,
                    product=preview.envelope.product,
                    question_id=row.question_id,
                )
                for row in preview.envelope.rows
            )
        )
        notes = anki_index.list_notes() if anki_index is not None else ()
        return keys, match_qid_tags(keys, notes, tag_rules)

    def page(
        request: Request,
        preview: ImportPreview | None = None,
        receipt: ImportReceipt | None = None,
        error: str | None = None,
        status: int = 200,
    ) -> HTMLResponse:
        context: dict[str, object] = {"preview": preview, "receipt": receipt, "error": error}
        if preview is not None:
            keys, links = candidates(preview)
            matched = {link.key for link in links}
            available = reviewable_drafts(preview, receipt.import_id if receipt else "preview")
            issues: dict[int, list[str]] = {}
            for issue in preview.issues:
                issues.setdefault(issue.row, []).append(issue.code)
            rows = preview.envelope.rows
            context.update(
                keys=keys,
                links=links,
                unmatched=tuple(k for k in keys if k not in matched),
                mapping_configured=anki_index is not None and bool(tag_rules),
                available=available,
                issues=issues,
                has_content=any(row.question is not None for row in rows),
                counts={
                    "rows": len(rows),
                    "qids": len(keys),
                    "attempts": len(
                        {(r.question_id, r.attempt_id) for r in rows if r.attempt_id is not None}
                    ),
                    "unknown": sum(r.result == "unknown" for r in rows),
                    "held": sum(
                        i.code in {"invalid_question", "missing_image"} for i in preview.issues
                    ),
                    "duplicates": sum(i.code == "duplicate_row" for i in preview.issues),
                    "conflicts": sum(i.code == "duplicate_conflict" for i in preview.issues),
                    "topics": sum(len(r.topics) for r in rows),
                },
            )
        return templates.TemplateResponse(
            request=request, name="question_bank_import.html", context=context, status_code=status
        )

    @router.get("", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        _owner(request)
        return page(request)

    @router.get("/anki-candidates", response_class=HTMLResponse)
    def candidate_form(request: Request) -> HTMLResponse:
        _owner(request)
        return templates.TemplateResponse(
            request=request, name="question_bank_import.html", context={"manual_candidates": True}
        )

    @router.post("/anki-candidates", response_class=HTMLResponse)
    def candidate_preview(
        request: Request,
        query: Annotated[str, Form()],
        csrf_token: Annotated[str | None, Form()] = None,
    ) -> HTMLResponse:
        _owner(request)
        require_form_csrf(request, csrf_token)
        context: dict[str, object] = {"manual_candidates": True}
        status = 200
        try:
            parse_candidate_query(query)
            context["candidate_query"] = query
            if anki_index is None or anki_index.snapshot_id() is None:
                context["candidate_error"] = "Local note index unavailable. No notes were checked."
                status = 503
            else:
                context["candidate_notes"] = preview_candidate_notes(query, anki_index)
        except ValueError as error:
            context["candidate_error"] = str(error)
            status = 422
        return templates.TemplateResponse(
            request=request, name="question_bank_import.html", context=context, status_code=status
        )

    @router.post("/imports/preview", response_class=HTMLResponse)
    async def preview(
        request: Request,
        file: Annotated[UploadFile, File()],
        csrf_token: Annotated[str | None, Form()] = None,
    ) -> HTMLResponse:
        _owner(request)
        require_form_csrf(request, csrf_token)
        raw = await _upload(file)
        try:
            checked = preview_import(raw)
        except ValueError as error:
            return page(
                request, error="Invalid normalized JSON v1: " + str(error)[:1000], status=422
            )
        return page(request, checked)

    @router.post("/imports/confirm")
    async def confirm(
        request: Request,
        file: Annotated[UploadFile, File()],
        digest: Annotated[str, Form()],
        csrf_token: Annotated[str | None, Form()] = None,
    ) -> Response:
        owner = _owner(request)
        require_form_csrf(request, csrf_token)
        raw = await _upload(file)
        try:
            checked = preview_import(raw)
        except ValueError as error:
            return page(
                request, error="Invalid normalized JSON v1: " + str(error)[:1000], status=422
            )
        if checked.digest != digest:
            return page(
                request, checked, error="File changed. Preview the selected file again.", status=409
            )
        try:
            receipt = repository.commit_import(checked, learner_id=owner, expected_digest=digest)
        except ValueError as error:
            return page(request, checked, error=str(error)[:1000], status=409)
        if receipt.conflicts:
            return page(
                request,
                checked,
                error="Import conflict: " + receipt.conflicts[0].detail,
                status=409,
            )
        return RedirectResponse(f"/question-bank/imports/{receipt.import_id}", status_code=303)

    @router.get("/imports/{import_id}", response_class=HTMLResponse)
    def receipt(request: Request, import_id: str) -> HTMLResponse:
        checked, stored = load(import_id, _owner(request))
        return page(request, checked, stored)

    @router.post("/imports/{import_id}/review")
    def review(
        request: Request,
        import_id: str,
        subject: Annotated[str, Form()],
        exam_number: Annotated[int, Form()],
        label: Annotated[str, Form()],
        row_numbers: Annotated[list[int] | None, Form()] = None,
        csrf_token: Annotated[str | None, Form()] = None,
    ) -> Response:
        owner = _owner(request)
        require_form_csrf(request, csrf_token)
        checked, stored = load(import_id, owner)
        if studio is None:
            raise HTTPException(503, "Question review is not configured")
        try:
            ref = stage_native_review(
                repository,
                studio,
                import_id=import_id,
                learner_id=owner,
                subject=subject,
                exam_number=exam_number,
                label=label,
                row_numbers=tuple(row_numbers) if row_numbers is not None else (),
            )
        except ValueError as error:
            return page(request, checked, stored, error=str(error), status=422)
        return RedirectResponse(ref.review_url, status_code=303)

    @router.get("/imports/{import_id}/anki-export", response_class=PlainTextResponse)
    def export(request: Request, import_id: str) -> PlainTextResponse:
        checked, _ = load(import_id, _owner(request))
        _, links = candidates(checked)
        ids = sorted({int(link.note_id) for link in links})
        if not ids:
            raise HTTPException(409, "No exact note matches to export")
        return PlainTextResponse(
            " OR ".join(f"nid:{note_id}" for note_id in ids) + "\n",
            headers={"Content-Disposition": 'attachment; filename="anki-candidates.txt"'},
        )

    return router
