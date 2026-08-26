from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from oms_hub.indexing.models import IndexJob, IndexState
from oms_hub.indexing.reconciliation import (
    FindingKind,
    IndexHealth,
    ReconciliationConflict,
    ReconciliationFinding,
    ReconciliationReport,
    RevisionIndexView,
)
from oms_hub.indexing.routes import build_indexing_router

REVISION = "sr_" + ("a" * 26)
STORE_ID = "11111111-1111-1111-1111-111111111111"


class _Reconciler:
    def __init__(self) -> None:
        self.reconcile_calls: list[tuple[str, bool]] = []
        self.rebuild_calls: list[str] = []
        self.delete_calls: list[str] = []

    def health(self) -> IndexHealth:
        return IndexHealth(
            provider="gemini",
            configured=True,
            sdk_version="2.14.0",
            model="gemini-3.7-flash",
            embedding_model="models/gemini-embedding-2",
            ready=True,
            last_contract_smoke=None,
            store_count=1,
            ready_document_count=2,
            failed_document_count=0,
            indexed_byte_count=123,
            index_token_count=None,
            query_token_count=None,
            estimated_cost=None,
        )

    def revision_status(self, revision_id: str) -> RevisionIndexView:
        return RevisionIndexView(
            source_revision_id=revision_id,
            store_id=STORE_ID,
            state=IndexState.READY,
            input_count=2,
            indexed_byte_count=123,
        )

    async def reconcile_store(
        self, store_id: str, *, apply: bool = False
    ) -> ReconciliationReport:
        self.reconcile_calls.append((store_id, apply))
        return ReconciliationReport(
            store_id=store_id,
            applied=apply,
            findings=(
                ReconciliationFinding(
                    FindingKind.LOCAL_MISSING_REMOTE,
                    source_revision_id=REVISION,
                    input_key="pdf",
                ),
            ),
            repaired_input_count=1 if apply else 0,
            indexed_byte_count=123,
        )

    def rebuild_revision(self, revision_id: str) -> IndexJob:
        self.rebuild_calls.append(revision_id)
        return IndexJob(
            id="22222222-2222-2222-2222-222222222222",
            store_id=STORE_ID,
            source_revision_id=revision_id,
            operation_kind="rebuild",
            state=IndexState.DELETING,
        )

    def delete_revision(self, revision_id: str) -> IndexJob:
        self.delete_calls.append(revision_id)
        return IndexJob(
            id="33333333-3333-3333-3333-333333333333",
            store_id=STORE_ID,
            source_revision_id=revision_id,
            operation_kind="delete",
            state=IndexState.DELETING,
        )


def _client(reconciler: _Reconciler) -> TestClient:
    app = FastAPI()
    app.include_router(build_indexing_router(SimpleNamespace(index_reconciler=reconciler)))
    return TestClient(app)


def test_health_and_revision_routes_serialize_only_allowlisted_fields() -> None:
    reconciler = _Reconciler()
    client = _client(reconciler)

    health = client.get("/api/v1/indexing/health")
    revision = client.get(f"/api/v1/indexing/revisions/{REVISION}")

    assert health.status_code == 200
    assert health.json() == asdict(reconciler.health())
    assert set(health.json()) == {
        "provider",
        "configured",
        "sdk_version",
        "model",
        "embedding_model",
        "ready",
        "last_contract_smoke",
        "store_count",
        "ready_document_count",
        "failed_document_count",
        "indexed_byte_count",
        "index_token_count",
        "query_token_count",
        "estimated_cost",
    }
    assert revision.status_code == 200
    assert revision.json()["source_revision_id"] == REVISION
    assert revision.headers["cache-control"] == "private, no-store"


def test_mutation_routes_require_explicit_targets_and_return_durable_jobs() -> None:
    reconciler = _Reconciler()
    client = _client(reconciler)

    dry_run = client.post(f"/api/v1/indexing/stores/{STORE_ID}/reconcile")
    applied = client.post(f"/api/v1/indexing/stores/{STORE_ID}/reconcile?apply=true")
    rebuilt = client.post(f"/api/v1/indexing/revisions/{REVISION}/rebuild")
    deleted = client.delete(f"/api/v1/indexing/revisions/{REVISION}")

    assert dry_run.status_code == 200 and dry_run.json()["applied"] is False
    assert applied.status_code == 200 and applied.json()["repaired_input_count"] == 1
    assert reconciler.reconcile_calls == [(STORE_ID, False), (STORE_ID, True)]
    assert rebuilt.status_code == 202
    assert rebuilt.json()["operation_kind"] == "rebuild"
    assert deleted.status_code == 202
    assert deleted.json()["operation_kind"] == "delete"
    assert reconciler.rebuild_calls == [REVISION]
    assert reconciler.delete_calls == [REVISION]


def test_routes_reject_malformed_revision_and_store_ids_without_service_calls() -> None:
    reconciler = _Reconciler()
    client = _client(reconciler)

    assert client.get("/api/v1/indexing/revisions/not-a-revision").status_code == 422
    assert client.post("/api/v1/indexing/stores/not-a-uuid/reconcile").status_code == 422
    assert reconciler.reconcile_calls == []
    assert reconciler.rebuild_calls == []


def test_routes_translate_declared_lookup_conflict_and_validation_errors() -> None:
    class FailingReconciler(_Reconciler):
        def revision_status(self, revision_id: str) -> RevisionIndexView:
            raise KeyError(revision_id)

        def rebuild_revision(self, revision_id: str) -> IndexJob:
            raise ReconciliationConflict("ambiguous current store")

        def delete_revision(self, revision_id: str) -> IndexJob:
            raise ValueError("revision cannot be deleted")

    client = _client(FailingReconciler())

    assert client.get(f"/api/v1/indexing/revisions/{REVISION}").status_code == 404
    assert client.post(f"/api/v1/indexing/revisions/{REVISION}/rebuild").status_code == 409
    assert client.delete(f"/api/v1/indexing/revisions/{REVISION}").status_code == 422
    assert client.post("/api/v1/indexing/revisions/not-a-revision/rebuild").status_code == 422
    assert client.delete("/api/v1/indexing/revisions/not-a-revision").status_code == 422
