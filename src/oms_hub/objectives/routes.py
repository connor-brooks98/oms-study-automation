"""Unregistered API routes for the objective review workflow."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from oms_hub.objectives.extraction import MAX_SOURCE_REVISIONS
from oms_hub.objectives.service import ObjectiveProposalRecord

__all__ = ["build_objective_router"]


class ExtractRequest(BaseModel):
    source_revision_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_SOURCE_REVISIONS,
    )


class MergeRequest(BaseModel):
    target_objective_id: str = Field(min_length=1, max_length=200)


def build_objective_router(container: Any) -> APIRouter:
    """Build owned routes without registering them on the production app."""
    router = APIRouter(prefix="/api/v1/objectives")

    def current(request: Request) -> Any:
        return getattr(request.app.state, "objective_service", container)

    @router.post("/extract")
    def extract(request: Request, body: ExtractRequest) -> JSONResponse:
        try:
            records = current(request).extract(body.source_revision_ids)
        except (KeyError, ValueError) as error:
            return _error(error)
        return _json({"objectives": [_record_payload(record) for record in records]})

    @router.get("")
    def list_objectives(request: Request) -> JSONResponse:
        records = current(request).list_proposals()
        return _json({"objectives": [_record_payload(record) for record in records]})

    @router.post("/{objective_id}/approve")
    def approve(request: Request, objective_id: str) -> JSONResponse:
        try:
            record = current(request).approve(objective_id)
        except (KeyError, ValueError) as error:
            return _error(error)
        return _json(_record_payload(record))

    @router.post("/{objective_id}/merge")
    def merge(request: Request, objective_id: str, body: MergeRequest) -> JSONResponse:
        try:
            record = current(request).merge(objective_id, body.target_objective_id)
        except (KeyError, ValueError) as error:
            return _error(error)
        return _json(_record_payload(record))

    @router.post("/{objective_id}/retire")
    def retire(request: Request, objective_id: str) -> JSONResponse:
        try:
            record = current(request).retire(objective_id)
        except (KeyError, ValueError) as error:
            return _error(error)
        return _json(_record_payload(record))

    return router


def _json(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={"Cache-Control": "private, no-store"},
    )


def _error(error: KeyError | ValueError) -> JSONResponse:
    if isinstance(error, KeyError):
        return _json({"detail": "objective proposal was not found"}, 404)
    return _json({"detail": str(error)}, 409)


def _record_payload(record: ObjectiveProposalRecord) -> dict[str, Any]:
    proposal = record.proposal
    return {
        "objective_id": proposal.proposal_id,
        "observable_verb": proposal.observable_verb,
        "concept": proposal.concept,
        "description": proposal.description,
        "course_id": proposal.course_id,
        "exam_id": proposal.exam_id,
        "lecture_ids": proposal.lecture_ids,
        "source_revision_ids": proposal.source_revision_ids,
        "evidence_ids": proposal.evidence_ids,
        "suggested_links": [
            {
                "edge_type": link.edge_type.value,
                "target_concept": link.target_concept,
            }
            for link in proposal.suggested_links
        ],
        "status": record.disposition.value,
        "approved_objective_id": record.approved_objective_id,
        "merged_into_id": record.merged_into_id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
