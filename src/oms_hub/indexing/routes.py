"""Unregistered indexing administration routes for later Sol-0 wiring."""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from oms_hub.indexing.models import IndexJob
from oms_hub.indexing.reconciliation import ReconciliationConflict

_REVISION_ID = re.compile(r"sr_[a-z2-7]{26}\Z")


def build_indexing_router(container: Any) -> APIRouter:
    router = APIRouter(prefix="/api/v1/indexing")

    def current(request: Request) -> Any:
        candidate = getattr(request.app.state, "index_reconciler", None)
        if candidate is None:
            candidate = getattr(container, "index_reconciler", container)
        return candidate

    @router.get("/health")
    def health(request: Request) -> JSONResponse:
        return _json(asdict(current(request).health()))

    @router.get("/revisions/{revision_id}")
    def revision(request: Request, revision_id: str) -> JSONResponse:
        if not _REVISION_ID.fullmatch(revision_id):
            return _json({"detail": "source revision ID is malformed"}, 422)
        try:
            return _json(asdict(current(request).revision_status(revision_id)))
        except Exception as error:  # narrow translation boundary
            return _error(error)

    @router.post("/stores/{store_id}/reconcile")
    async def reconcile(request: Request, store_id: str, apply: bool = False) -> JSONResponse:
        if not _uuid(store_id):
            return _json({"detail": "store ID is malformed"}, 422)
        try:
            report = await current(request).reconcile_store(store_id, apply=apply)
        except Exception as error:  # narrow translation boundary
            return _error(error)
        return _json(asdict(report))

    @router.post("/revisions/{revision_id}/rebuild")
    def rebuild(request: Request, revision_id: str) -> JSONResponse:
        if not _REVISION_ID.fullmatch(revision_id):
            return _json({"detail": "source revision ID is malformed"}, 422)
        try:
            job = current(request).rebuild_revision(revision_id)
        except Exception as error:  # narrow translation boundary
            return _error(error)
        return _json(_job_payload(job), 202)

    @router.delete("/revisions/{revision_id}")
    def delete(request: Request, revision_id: str) -> JSONResponse:
        if not _REVISION_ID.fullmatch(revision_id):
            return _json({"detail": "source revision ID is malformed"}, 422)
        try:
            job = current(request).delete_revision(revision_id)
        except Exception as error:  # narrow translation boundary
            return _error(error)
        return _json(_job_payload(job), 202)

    return router


def _uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _job_payload(job: IndexJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "store_id": job.store_id,
        "source_revision_id": job.source_revision_id,
        "operation_kind": job.operation_kind,
        "state": job.state.value,
    }


def _json(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={"Cache-Control": "private, no-store"},
    )


def _error(error: Exception) -> JSONResponse:
    if isinstance(error, KeyError):
        status = 404
    elif isinstance(error, ReconciliationConflict):
        status = 409
    elif isinstance(error, ValueError):
        status = 422
    else:
        raise error
    return _json({"detail": str(error)}, status)


__all__ = ["build_indexing_router"]
