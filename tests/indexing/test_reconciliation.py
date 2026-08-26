from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
from pydantic import SecretStr

from oms_hub.db import Database
from oms_hub.indexing.models import (
    IndexJob,
    IndexState,
    ProviderDocument,
    ProviderStore,
    StoreKey,
)
from oms_hub.indexing.reconciliation import (
    FindingKind,
    IndexReconciler,
    ReconciliationConflict,
)
from oms_hub.indexing.repository import IndexRepository
from oms_hub.indexing.service import IndexResult
from oms_hub.indexing.worker import IndexWorker
from oms_hub.knowledge.models import SourceRevisionState
from oms_hub.providers.gemini.client import GeminiClientFactory
from oms_hub.providers.gemini.errors import GeminiProviderError, GeminiTransientError
from oms_hub.providers.gemini.file_search import (
    GeminiFileSearchAdmin,
    RemoteDocumentObservation,
)
from oms_hub.providers.gemini.models import GeminiConfig

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
REVISION = "sr_" + ("a" * 26)


def _database(path: Path | None = None) -> Database:
    database = Database("sqlite://" if path is None else f"sqlite:///{path}")
    database.create_schema()
    return database


def _store(*, generation: int = 1, current: bool = True) -> ProviderStore:
    key = StoreKey.course("course-1", "exam-1")
    return ProviderStore(
        store_key=key,
        provider="gemini",
        provider_store_name=f"fileSearchStores/store-{generation}",
        embedding_model="models/gemini-embedding-2",
        authority_namespace=key.authority_namespace,
        course_id=key.course_id,
        exam_id=key.exam_id,
        generation=generation,
        is_current=current,
    )


def _document(
    store: ProviderStore,
    input_key: str,
    input_kind: str,
    digest: str,
    *,
    revision: str = REVISION,
    state: IndexState = IndexState.READY,
) -> ProviderDocument:
    return ProviderDocument(
        store_id=store.id,
        provider="gemini",
        provider_document_id=f"{store.provider_store_name}/documents/{input_key}",
        source_revision_id=revision,
        input_key=input_key,
        input_kind=input_kind,
        input_sha256=digest,
        provider_file_name=f"files/{input_key}",
        provider_document_name=f"documents/{input_key}",
        provider_operation_name=f"operations/{input_key}",
        input_byte_count=100 + len(input_key),
        metadata={"source_revision_id": revision, "input_key": input_key},
        state=state,
    )


def _observation(document: ProviderDocument) -> RemoteDocumentObservation:
    assert document.provider_document_id is not None
    assert document.input_sha256 is not None
    return RemoteDocumentObservation(
        provider_document_id=document.provider_document_id,
        source_revision_id=document.source_revision_id,
        input_key=document.input_key,
        input_kind=document.input_kind,
        input_sha256=document.input_sha256,
        input_byte_count=document.input_byte_count,
    )


class _Knowledge:
    def __init__(self, states: dict[str, SourceRevisionState] | None = None) -> None:
        self.states = states or {REVISION: SourceRevisionState.READY}

    def get_revision_view(self, revision_id: str) -> object:
        return SimpleNamespace(revision_state=self.states[revision_id])


class _SnapshotAdmin:
    def __init__(self, observations: tuple[RemoteDocumentObservation, ...] = ()) -> None:
        self.observations = observations
        self.snapshot_calls: list[str] = []
        self.client_factory = SimpleNamespace(
            config=SimpleNamespace(
                sdk_version="2.14.0",
                file_search_model="gemini-3.7-flash",
                embedding_model="models/gemini-embedding-2",
                request_timeout_seconds=120,
                operation_timeout_seconds=900,
                api_key=SecretStr("must-not-serialize"),
            )
        )
        self.snapshot_error: GeminiProviderError | None = None

    async def snapshot_documents(
        self, store: ProviderStore
    ) -> tuple[RemoteDocumentObservation, ...]:
        self.snapshot_calls.append(store.provider_store_name)
        if self.snapshot_error is not None:
            raise self.snapshot_error
        return self.observations


class _RawDocuments:
    def __init__(self, items: list[object]) -> None:
        self.items = items

    async def list(self, **_kwargs: object) -> list[object]:
        return list(self.items)


class _RawAio:
    def __init__(self, items: list[object]) -> None:
        self.file_search_stores = SimpleNamespace(documents=_RawDocuments(items))

    async def aclose(self) -> None:
        return None


class _RawSdk:
    def __init__(self, items: list[object]) -> None:
        self.aio = _RawAio(items)


def _sdk_factory(items: list[object]) -> Any:
    def factory(**_kwargs: object) -> _RawSdk:
        return _RawSdk(items)

    return factory


def test_remote_snapshot_is_pure_and_preserves_multimodal_input_identity() -> None:
    database = _database()
    repository = IndexRepository(database)
    store = repository.create_store(_store())
    items = [
        SimpleNamespace(
            name=f"{store.provider_store_name}/documents/pdf",
            size_bytes=120,
            custom_metadata={
                "source_revision_id": REVISION,
                "input_key": "pdf",
                "input_kind": "pdf",
                "input_sha256": "a" * 64,
            },
        ),
        SimpleNamespace(
            name=f"{store.provider_store_name}/documents/markdown",
            size_bytes=80,
            custom_metadata={
                "source_revision_id": REVISION,
                "input_key": "normalized_markdown",
                "input_kind": "markdown",
                "input_sha256": "b" * 64,
            },
        ),
        SimpleNamespace(
            name=f"{store.provider_store_name}/documents/invalid",
            custom_metadata={"source_revision_id": REVISION},
        ),
    ]
    factory = GeminiClientFactory(
        GeminiConfig(api_key=SecretStr("synthetic-secret")),
        sdk_factory=_sdk_factory(items),
    )
    admin = GeminiFileSearchAdmin(repository, factory)

    snapshot = asyncio.run(admin.snapshot_documents(store))

    assert repository.list_documents(store) == []
    assert [(item.input_key, item.input_kind) for item in snapshot[:2]] == [
        ("normalized_markdown", "markdown"),
        ("pdf", "pdf"),
    ]
    assert snapshot[2].validation_error == "invalid_metadata"
    assert snapshot[2].input_key is None


def test_reconcile_reports_deterministic_multimodal_orphans_without_writes() -> None:
    database = _database()
    repository = IndexRepository(database)
    store = repository.create_store(_store())
    pdf = repository.save_document(_document(store, "pdf", "pdf", "a" * 64))
    markdown = repository.save_document(
        _document(store, "normalized_markdown", "markdown", "b" * 64)
    )
    remote_pdf = _observation(pdf)
    remote_only = RemoteDocumentObservation(
        provider_document_id=f"{store.provider_store_name}/documents/remote-image",
        source_revision_id=REVISION,
        input_key="image." + ("c" * 64),
        input_kind="image",
        input_sha256="c" * 64,
        input_byte_count=44,
    )
    invalid = RemoteDocumentObservation(
        provider_document_id=f"{store.provider_store_name}/documents/invalid",
        validation_error="invalid_metadata",
    )
    admin = _SnapshotAdmin((remote_pdf, remote_pdf, remote_only, invalid))
    reconciler = IndexReconciler(repository, _Knowledge(), admin)
    before = repository.list_documents(store)

    report = asyncio.run(reconciler.reconcile_store(store.id))

    assert repository.list_documents(store) == before
    assert report.applied is False
    assert report.repaired_input_count == 0
    assert tuple(item.kind for item in report.findings) == (
        FindingKind.DUPLICATE_REMOTE,
        FindingKind.INVALID_REMOTE,
        FindingKind.LOCAL_MISSING_REMOTE,
        FindingKind.REMOTE_MISSING_LOCAL,
    )
    assert report.findings[2].input_key == markdown.input_key


def test_reconcile_missing_store_is_report_only_even_with_apply() -> None:
    database = _database()
    repository = IndexRepository(database)
    store = repository.create_store(_store())
    repository.save_document(_document(store, "pdf", "pdf", "a" * 64))
    admin = _SnapshotAdmin()
    admin.snapshot_error = GeminiProviderError("missing", provider_status_code=404)
    reconciler = IndexReconciler(repository, _Knowledge(), admin)

    report = asyncio.run(reconciler.reconcile_store(store.id, apply=True))

    assert [item.kind for item in report.findings] == [FindingKind.STORE_MISSING]
    assert report.repaired_input_count == 0
    assert repository.list_documents(store)[0].state is IndexState.READY


def test_apply_resets_only_ready_local_input_missing_remotely() -> None:
    database = _database()
    repository = IndexRepository(database)
    store = repository.create_store(_store())
    pdf = repository.save_document(_document(store, "pdf", "pdf", "a" * 64))
    markdown = repository.save_document(
        replace(
            _document(store, "normalized_markdown", "markdown", "b" * 64),
            retry_count=2,
            last_error_category="transient",
        )
    )
    repository.save_job(
        IndexJob(
            store_id=store.id,
            source_revision_id=REVISION,
            operation_kind="index",
            provider_document_id=pdf.provider_document_id,
            provider_operation_name=pdf.provider_operation_name,
            state=IndexState.READY,
            retry_count=2,
            last_error_category="transient",
            last_error_message="safe-category",
            next_attempt_at=NOW.isoformat(),
        )
    )
    reconciler = IndexReconciler(repository, _Knowledge(), _SnapshotAdmin((_observation(pdf),)))

    report = asyncio.run(reconciler.reconcile_store(store.id, apply=True))

    reset = repository.get_document(markdown.id)
    untouched = repository.get_document(pdf.id)
    job = repository.get_job_by_revision(store.id, REVISION)
    assert report.repaired_input_count == 1
    assert reset is not None and reset.state is IndexState.NOT_INDEXED
    assert reset.provider_document_id is None
    assert reset.provider_document_name is None
    assert reset.provider_file_name is None
    assert reset.provider_operation_name is None
    assert reset.retry_count == 0 and reset.last_error_category is None
    assert (reset.input_key, reset.input_kind, reset.input_sha256) == (
        markdown.input_key,
        markdown.input_kind,
        markdown.input_sha256,
    )
    assert reset.input_byte_count == markdown.input_byte_count
    assert reset.metadata == markdown.metadata
    assert untouched == pdf
    assert job is not None and job.state is IndexState.NOT_INDEXED
    assert job.operation_kind == "index" and job.lease_token is None
    assert job.provider_document_id is None and job.provider_operation_name is None
    assert job.retry_count == 0 and job.last_error_category is None
    assert job.last_error_message is None and job.next_attempt_at is None


def test_apply_schedules_permanent_delete_for_stale_revision() -> None:
    database = _database()
    repository = IndexRepository(database)
    store = repository.create_store(_store())
    pdf = repository.save_document(_document(store, "pdf", "pdf", "a" * 64))
    reconciler = IndexReconciler(
        repository,
        _Knowledge({REVISION: SourceRevisionState.STALE}),
        _SnapshotAdmin((_observation(pdf),)),
        now=lambda: NOW,
    )

    report = asyncio.run(reconciler.reconcile_store(store.id, apply=True))

    job = repository.get_job_by_revision(store.id, REVISION)
    assert [item.kind for item in report.findings] == [FindingKind.STALE_SOURCE]
    assert job is not None and job.operation_kind == "delete"
    assert job.state is IndexState.DELETING


class _DeleteAdmin:
    def __init__(self, fail_once: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_once = fail_once
        self.client_factory = SimpleNamespace(
            config=SimpleNamespace(request_timeout_seconds=1, operation_timeout_seconds=1)
        )

    async def delete_remote_document(self, provider_document_id: str) -> None:
        self.calls.append(provider_document_id)
        if self.fail_once == provider_document_id:
            self.fail_once = None
            raise GeminiTransientError("temporary")


class _IndexService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def index_revision(self, revision_id: str) -> IndexResult:
        self.calls.append(revision_id)
        return IndexResult(revision_id, IndexState.READY, "documents/rebuilt")


class _Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value


def test_rebuild_deletes_every_input_then_resets_for_normal_worker_indexing() -> None:
    database = _database()
    repository = IndexRepository(database)
    store = repository.create_store(_store())
    documents = [
        repository.save_document(_document(store, "pdf", "pdf", "a" * 64)),
        repository.save_document(
            _document(store, "normalized_markdown", "markdown", "b" * 64)
        ),
    ]
    admin = _DeleteAdmin()
    service = _IndexService()
    reconciler = IndexReconciler(repository, _Knowledge(), admin, now=lambda: NOW)
    job = reconciler.rebuild_revision(REVISION)
    worker = IndexWorker(
        repository,
        service,
        admin=admin,
        worker_id="worker-1",
        lease_seconds=60,
        now=lambda: NOW,
    )

    worker.run_once()

    reset_job = repository.get_job(job.id)
    reset_documents = repository.list_documents(store)
    assert admin.calls == [item.provider_document_id for item in documents]
    assert reset_job is not None and reset_job.state is IndexState.NOT_INDEXED
    assert reset_job.operation_kind == "index" and reset_job.lease_token is None
    assert all(item.state is IndexState.NOT_INDEXED for item in reset_documents)
    assert all(item.provider_document_id is None for item in reset_documents)
    assert all(item.provider_file_name is None for item in reset_documents)
    assert all(item.provider_operation_name is None for item in reset_documents)
    assert all(item.metadata == {} for item in reset_documents)

    worker.run_once()

    assert service.calls == [REVISION]
    assert repository.get_job(job.id).state is IndexState.READY  # type: ignore[union-attr]


def test_rebuild_resumes_remaining_inputs_after_transient_delete_failure() -> None:
    database = _database()
    repository = IndexRepository(database)
    store = repository.create_store(_store())
    first = repository.save_document(_document(store, "pdf", "pdf", "a" * 64))
    second = repository.save_document(
        _document(store, "normalized_markdown", "markdown", "b" * 64)
    )
    assert second.provider_document_id is not None
    admin = _DeleteAdmin(fail_once=second.provider_document_id)
    service = _IndexService()
    clock = _Clock()
    reconciler = IndexReconciler(repository, _Knowledge(), admin, now=clock)
    job = reconciler.rebuild_revision(REVISION)
    worker = IndexWorker(
        repository,
        service,
        admin=admin,
        worker_id="worker-1",
        lease_seconds=60,
        now=clock,
    )

    worker.run_once()

    after_failure = repository.get_job(job.id)
    assert repository.get_document(first.id).state is IndexState.DELETED  # type: ignore[union-attr]
    assert repository.get_document(second.id).state is IndexState.DELETING  # type: ignore[union-attr]
    assert after_failure is not None and after_failure.operation_kind == "rebuild"
    assert after_failure.state is IndexState.DELETING

    clock.value += timedelta(seconds=10)
    worker.run_once()

    assert repository.get_job(job.id).state is IndexState.NOT_INDEXED  # type: ignore[union-attr]
    assert admin.calls.count(first.provider_document_id) == 1
    assert admin.calls.count(second.provider_document_id) == 2


def test_expired_lease_token_cannot_mutate_after_successor_claims(tmp_path: Path) -> None:
    path = tmp_path / "hub.db"
    first_database = _database(path)
    first_repository = IndexRepository(first_database)
    store = first_repository.create_store(_store())
    document = first_repository.save_document(_document(store, "pdf", "pdf", "a" * 64))
    first = first_repository.claim_revision_operation(
        store.id,
        REVISION,
        "delete",
        "worker-1",
        NOW,
        lease_seconds=1,
    )
    assert first is not None and first.lease_token is not None
    second_database = Database(f"sqlite:///{path}")
    second_repository = IndexRepository(second_database)
    later = NOW + timedelta(seconds=2)
    second = second_repository.claim_revision_operation(
        store.id,
        REVISION,
        "delete",
        "worker-2",
        later,
        lease_seconds=60,
    )
    assert second is not None and second.lease_token is not None

    assert first_repository.renew_revision_lease(first.id, first.lease_token, later, 60) is False
    assert (
        first_repository.mark_document_deleting_with_token(
            document.id, first.id, first.lease_token, later
        )
        is False
    )
    assert second_repository.renew_revision_lease(second.id, second.lease_token, later, 60)
    assert first_repository.get_document(document.id).state is IndexState.READY  # type: ignore[union-attr]


def test_revision_resolution_rejects_ambiguous_current_stores() -> None:
    database = _database()
    repository = IndexRepository(database)
    first = repository.create_store(_store())
    second_key = StoreKey.course("course-2", "exam-1")
    second = repository.create_store(
        ProviderStore(
            store_key=second_key,
            provider="gemini",
            provider_store_name="fileSearchStores/other",
            embedding_model="models/gemini-embedding-2",
            authority_namespace=second_key.authority_namespace,
            course_id=second_key.course_id,
            exam_id=second_key.exam_id,
        )
    )
    repository.save_document(_document(first, "pdf", "pdf", "a" * 64))
    repository.save_document(_document(second, "pdf", "pdf", "b" * 64))
    reconciler = IndexReconciler(repository, _Knowledge(), _SnapshotAdmin())

    with pytest.raises(ReconciliationConflict, match="current store"):
        reconciler.rebuild_revision(REVISION)


def test_health_is_offline_redacted_and_reports_byte_only_usage() -> None:
    database = _database()
    repository = IndexRepository(database)
    store = repository.create_store(_store())
    repository.save_document(_document(store, "pdf", "pdf", "a" * 64))
    repository.save_document(
        _document(
            store,
            "normalized_markdown",
            "markdown",
            "b" * 64,
            state=IndexState.TERMINAL_FAILURE,
        )
    )
    admin = _SnapshotAdmin()
    reconciler = IndexReconciler(repository, _Knowledge(), admin)

    health = reconciler.health()

    assert admin.snapshot_calls == []
    assert health.provider == "gemini"
    assert health.store_count == 1
    assert health.ready_document_count == 1
    assert health.failed_document_count == 1
    assert health.indexed_byte_count == 103
    assert health.index_token_count is None
    assert health.query_token_count is None
    assert health.estimated_cost is None
    assert "must-not-serialize" not in repr(health)

