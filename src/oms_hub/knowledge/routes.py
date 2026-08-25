"""Unregistered, authenticated-by-application routes for knowledge views."""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from oms_hub.knowledge.models import EvidenceUnit
from oms_hub.knowledge.service import (
    DependencyProvenanceUnavailable,
    EvidenceView,
    KnowledgeIntegrityError,
    KnowledgeNotFoundError,
    KnowledgeService,
    PreviewUnavailableError,
    UnsupportedRevisionState,
)
from oms_hub.providers.contracts import RetrievalScope, TruthMode

__all__ = ["build_knowledge_router"]

_REVISION_ID = re.compile(r"sr_[a-z2-7]{26}\Z")


def build_knowledge_router(container: Any) -> APIRouter:
    """Build a router without registering it on the production application."""
    router = APIRouter(prefix="/api/v1/knowledge")
    service = container if isinstance(container, KnowledgeService) else None

    def current(request: Request) -> KnowledgeService:
        if service is not None:
            return service
        candidate = getattr(request.app.state, "knowledge_service", container)
        return candidate if isinstance(candidate, KnowledgeService) else KnowledgeService(candidate)

    @router.get("/scopes/{course_id}")
    def scope_sources(
        request: Request,
        course_id: str,
        exam_id: str | None = None,
        lecture_ids: tuple[str, ...] = Query(()),
        truth_mode: TruthMode = TruthMode.COURSE_ONLY,
        source_revision_ids: tuple[str, ...] = Query(()),
    ) -> JSONResponse:
        scope = RetrievalScope(course_id, exam_id, lecture_ids, truth_mode, source_revision_ids)
        try:
            view = current(request).get_scope_sources(scope)
        except Exception as error:
            return _error(error)
        return _json(
            {
                "scope": asdict(view.scope),
                "evidence": [_evidence_payload(unit) for unit in view.evidence],
            }
        )

    @router.get("/revisions/{revision_id}")
    def index_input(request: Request, revision_id: str) -> JSONResponse:
        if not _REVISION_ID.fullmatch(revision_id):
            return _json({"detail": "source revision ID is malformed"}, 422)
        try:
            view = current(request).resolve_index_input(revision_id)
        except Exception as error:
            return _error(error)
        return _json(
            {
                "source_document_id": view.source_document_id,
                "source_revision_id": view.source_revision_id,
                "source_family": view.source_family,
                "revision_state": view.revision_state.value,
                "authority_class": view.authority_class.value,
                "course_id": view.course_id,
                "exam_id": view.exam_id,
                "lecture_id": view.lecture_id,
                "pptx": _artifact_payload(view.pptx),
                "pdf": _artifact_payload(view.pdf),
                "evidence": [_evidence_payload(unit) for unit in view.evidence_units],
                "assets": [
                    {
                        "asset_id": asset.asset_id,
                        "media_type": asset.media_type,
                        "sha256": asset.sha256,
                        "locator": asdict(asset.locator),
                    }
                    for asset in view.assets
                ],
            }
        )

    @router.get("/evidence/{evidence_id}")
    def evidence(request: Request, evidence_id: str) -> JSONResponse:
        try:
            view = current(request).resolve_evidence(evidence_id)
        except Exception as error:
            return _error(error)
        return _json(_evidence_view_payload(view))

    @router.post("/revisions/{revision_id}/rebuild")
    def rebuild(request: Request, revision_id: str) -> JSONResponse:
        if not _REVISION_ID.fullmatch(revision_id):
            return _json({"detail": "source revision ID is malformed"}, 422)
        try:
            current(request).mark_dependents_stale(revision_id)
        except Exception as error:
            return _error(error)
        return _json({"detail": "rebuild is unavailable"}, 409)

    return router


def _json(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={"Cache-Control": "private, no-store"},
    )


def _error(error: Exception) -> JSONResponse:
    if isinstance(error, (KnowledgeNotFoundError,)):
        status = 404
    elif isinstance(
        error,
        (
            KnowledgeIntegrityError,
            PreviewUnavailableError,
            DependencyProvenanceUnavailable,
            UnsupportedRevisionState,
        ),
    ):
        status = 409
    else:
        status = 422
    return _json({"detail": str(error)}, status)


def _artifact_payload(artifact: Any) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "role": artifact.role.value,
        "sha256": artifact.sha256,
        "media_type": artifact.media_type,
    }


def _evidence_payload(unit: EvidenceUnit) -> dict[str, Any]:
    return {
        "evidence_id": unit.evidence_id,
        "source_revision_id": unit.source_revision_id,
        "authority_class": unit.authority_class.value,
        "locator": {"kind": unit.locator.kind.value, "value": unit.locator.value},
        "excerpt": unit.normalized_text,
    }


def _evidence_view_payload(view: EvidenceView) -> dict[str, Any]:
    return {
        "evidence_id": view.evidence_id,
        "source_revision_id": view.source_revision_id,
        "authority_class": view.authority_class.value,
        "locator": {"kind": view.locator.kind.value, "value": view.locator.value},
        "excerpt": view.excerpt,
        "preview": asdict(view.preview),
    }
