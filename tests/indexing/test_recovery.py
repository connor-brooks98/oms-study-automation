from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oms_hub.db import Database
from oms_hub.indexing.models import (
    ALLOWED_TRANSITIONS,
    IndexJob,
    IndexState,
    ProviderDocument,
    ProviderStore,
    StoreKey,
)
from oms_hub.indexing.repository import IndexRepository
from oms_hub.indexing.service import IndexResult
from oms_hub.indexing.worker import IndexWorker
from oms_hub.workers import RecoveryReport

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
_OPEN_DATABASES: list[Database] = []


@pytest.fixture(autouse=True)
def _close_databases() -> Iterator[None]:
    yield
    while _OPEN_DATABASES:
        _OPEN_DATABASES.pop().close()


def _store() -> ProviderStore:
    key = StoreKey.course("course-1", "exam-1")
    return ProviderStore(
        store_key=key,
        provider="gemini",
        provider_store_name="fileSearchStores/store-1",
        embedding_model="models/gemini-embedding-2",
        authority_namespace=key.authority_namespace,
        course_id=key.course_id,
        exam_id=key.exam_id,
    )


def _database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    _OPEN_DATABASES.append(database)
    database.migrate()
    return database


class NoopIndexingService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def index_revision(self, _source_revision_id: str) -> IndexResult:
        self.calls.append(_source_revision_id)
        return IndexResult(_source_revision_id, IndexState.READY)


class FakeAdmin:
    def __init__(self) -> None:
        self.delete_document_calls: list[str] = []

    async def delete_document(self, provider_document_id: str) -> None:
        self.delete_document_calls.append(provider_document_id)


def _persist_interrupted_job(
    repository: IndexRepository,
    *,
    state: IndexState,
    provider_file_name: str | None,
    provider_operation_name: str | None,
    provider_document_id: str | None = None,
    retry_count: int = 0,
) -> IndexJob:
    store = repository.create_store(_store())
    source_revision_id = f"sr_{state.value}_{retry_count}"
    repository.save_document(
        ProviderDocument(
            store_id=store.id,
            provider="gemini",
            provider_document_id=provider_document_id,
            source_revision_id=source_revision_id,
            provider_file_name=provider_file_name,
            provider_operation_name=provider_operation_name,
            metadata={"source_revision_id": source_revision_id},
            state=state,
            retry_count=retry_count,
        )
    )
    return repository.save_job(
        IndexJob(
            store_id=store.id,
            source_revision_id=source_revision_id,
            provider_document_id=provider_document_id,
            provider_operation_name=provider_operation_name,
            state=state,
            retry_count=retry_count,
            lease_owner="interrupted-worker",
            lease_expires_at=(NOW - timedelta(seconds=1)).isoformat(),
        )
    )


@pytest.mark.parametrize(
    (
        "state",
        "provider_file_name",
        "provider_operation_name",
        "provider_document_id",
        "expected_state",
    ),
    (
        (
            IndexState.UPLOADING_FILE,
            None,
            None,
            None,
            IndexState.UPLOADING_FILE,
        ),
        (
            IndexState.FILE_UPLOADED,
            "files/file-1",
            None,
            None,
            IndexState.FILE_UPLOADED,
        ),
        (
            IndexState.IMPORTING,
            "files/file-1",
            "operations/import-1",
            None,
            IndexState.IMPORTING,
        ),
        (
            IndexState.IMPORTING,
            "files/file-1",
            None,
            None,
            IndexState.FILE_UPLOADED,
        ),
        (
            IndexState.DELETING,
            "files/file-1",
            None,
            "fileSearchStores/store-1/documents/document-1",
            IndexState.DELETING,
        ),
    ),
)
def test_recover_interrupted_resumes_each_nonterminal_phase(
    tmp_path: Path,
    state: IndexState,
    provider_file_name: str | None,
    provider_operation_name: str | None,
    provider_document_id: str | None,
    expected_state: IndexState,
) -> None:
    database = _database(tmp_path)
    repository = IndexRepository(database)
    job = _persist_interrupted_job(
        repository,
        state=state,
        provider_file_name=provider_file_name,
        provider_operation_name=provider_operation_name,
        provider_document_id=provider_document_id,
    )
    service = NoopIndexingService()
    admin = FakeAdmin()
    worker = IndexWorker(
        repository,
        service,
        admin=admin,
        worker_id="worker-1",
        lease_seconds=60,
        now=lambda: NOW,
    )

    report = worker.recover_interrupted()
    recovered = repository.get_job(job.id)

    assert report == RecoveryReport(reclaimed_leases=1, resumed_jobs=1)
    assert recovered is not None
    assert recovered.state is expected_state
    assert recovered.lease_owner is None
    assert recovered.lease_expires_at is None
    if state is IndexState.DELETING:
        result = worker.run_once()
        assert result.job_id == job.id
        assert admin.delete_document_calls == [provider_document_id]
        deleted = repository.get_job(job.id)
        assert deleted is not None
        assert deleted.state is IndexState.DELETED
        assert deleted.lease_owner is None
        assert deleted.lease_expires_at is None
    else:
        assert service.calls == []


def test_recover_interrupted_terminalizes_exhausted_retry_budget(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = IndexRepository(database)
    job = _persist_interrupted_job(
        repository,
        state=IndexState.RETRYABLE_FAILURE,
        provider_file_name="files/file-1",
        provider_operation_name="operations/import-1",
        retry_count=3,
    )
    repository.upsert_job(
        replace(
            job,
            next_attempt_at=(NOW + timedelta(minutes=5)).isoformat(),
        )
    )
    worker = IndexWorker(
        repository,
        NoopIndexingService(),
        worker_id="worker-1",
        lease_seconds=60,
        max_attempts=3,
        now=lambda: NOW,
    )

    report = worker.recover_interrupted()
    recovered = repository.get_job(job.id)

    assert report == RecoveryReport(reclaimed_leases=1, terminal_failures=1)
    assert recovered is not None
    assert recovered.state is IndexState.TERMINAL_FAILURE
    assert recovered.lease_owner is None
    assert recovered.lease_expires_at is None


def test_recovery_terminalizes_exhausted_delete_retry(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = IndexRepository(database)
    job = _persist_interrupted_job(
        repository,
        state=IndexState.DELETING,
        provider_file_name="files/file-1",
        provider_operation_name=None,
        provider_document_id="fileSearchStores/store-1/documents/document-1",
        retry_count=3,
    )
    admin = FakeAdmin()
    worker = IndexWorker(
        repository,
        NoopIndexingService(),
        admin=admin,
        worker_id="worker-1",
        lease_seconds=60,
        max_attempts=3,
        now=lambda: NOW,
    )

    report = worker.recover_interrupted()
    recovered = repository.get_job(job.id)

    assert report == RecoveryReport(reclaimed_leases=1, terminal_failures=1)
    assert recovered is not None
    assert recovered.state is IndexState.TERMINAL_FAILURE
    assert recovered.lease_owner is None
    assert admin.delete_document_calls == []


def test_recovery_uses_document_operation_persisted_before_worker_checkpoint(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    repository = IndexRepository(database)
    job = _persist_interrupted_job(
        repository,
        state=IndexState.IMPORTING,
        provider_file_name="files/file-1",
        provider_operation_name="operations/import-1",
    )
    repository.upsert_job(replace(job, provider_operation_name=None))
    worker = IndexWorker(
        repository,
        NoopIndexingService(),
        worker_id="worker-1",
        lease_seconds=60,
        now=lambda: NOW,
    )

    report = worker.recover_interrupted()
    recovered = repository.get_job(job.id)
    document = repository.get_document_by_source_revision(
        job.store_id,
        job.source_revision_id,
    )

    assert report == RecoveryReport(reclaimed_leases=1, resumed_jobs=1)
    assert recovered is not None
    assert recovered.state is IndexState.IMPORTING
    assert recovered.provider_operation_name == "operations/import-1"
    assert document is not None
    assert document.state is IndexState.IMPORTING


def test_recovery_reclaims_completed_job_without_counting_it_resumed(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = IndexRepository(database)
    job = _persist_interrupted_job(
        repository,
        state=IndexState.READY,
        provider_file_name="files/file-1",
        provider_operation_name="operations/import-1",
        provider_document_id="fileSearchStores/store-1/documents/document-1",
    )
    worker = IndexWorker(
        repository,
        NoopIndexingService(),
        worker_id="worker-1",
        lease_seconds=60,
        now=lambda: NOW,
    )

    report = worker.recover_interrupted()
    recovered = repository.get_job(job.id)

    assert report == RecoveryReport(reclaimed_leases=1)
    assert recovered is not None
    assert recovered.state is IndexState.READY
    assert recovered.lease_owner is None


def test_recovery_restores_deleting_identity_from_document(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = IndexRepository(database)
    provider_document_id = "fileSearchStores/store-1/documents/document-1"
    job = _persist_interrupted_job(
        repository,
        state=IndexState.DELETING,
        provider_file_name="files/file-1",
        provider_operation_name=None,
        provider_document_id=provider_document_id,
    )
    repository.upsert_job(replace(job, provider_document_id=None))
    admin = FakeAdmin()
    worker = IndexWorker(
        repository,
        NoopIndexingService(),
        admin=admin,
        worker_id="worker-1",
        lease_seconds=60,
        now=lambda: NOW,
    )

    report = worker.recover_interrupted()
    recovered = repository.get_job(job.id)
    result = worker.run_once()

    assert report == RecoveryReport(reclaimed_leases=1, resumed_jobs=1)
    assert recovered is not None
    assert recovered.provider_document_id == provider_document_id
    assert result.job_id == job.id
    assert admin.delete_document_calls == [provider_document_id]


def test_import_recovery_transition_is_explicitly_allowed() -> None:
    assert IndexState.FILE_UPLOADED in ALLOWED_TRANSITIONS[IndexState.IMPORTING]


def test_recover_interrupted_leaves_live_lease_owned(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = IndexRepository(database)
    job = _persist_interrupted_job(
        repository,
        state=IndexState.IMPORTING,
        provider_file_name="files/file-1",
        provider_operation_name="operations/import-1",
    )
    repository.upsert_job(
        replace(
            job,
            lease_owner="live-worker",
            lease_expires_at=(NOW + timedelta(seconds=60)).isoformat(),
        )
    )
    worker = IndexWorker(
        repository,
        NoopIndexingService(),
        worker_id="worker-1",
        lease_seconds=60,
        now=lambda: NOW,
    )

    report = worker.recover_interrupted()
    recovered = repository.get_job(job.id)

    assert report == RecoveryReport()
    assert recovered is not None
    assert recovered.state is IndexState.IMPORTING
    assert recovered.lease_owner == "live-worker"
    assert recovered.lease_expires_at == (NOW + timedelta(seconds=60)).isoformat()


def test_recovery_does_not_mutate_job_claimed_after_global_reclaim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = f"sqlite:///{tmp_path / 'hub.db'}"
    database = Database(url)
    _OPEN_DATABASES.append(database)
    database.migrate()
    repository = IndexRepository(database)
    job = _persist_interrupted_job(
        repository,
        state=IndexState.IMPORTING,
        provider_file_name="files/file-1",
        provider_operation_name=None,
    )
    competitor_database = Database(url)
    _OPEN_DATABASES.append(competitor_database)
    competitor = IndexRepository(competitor_database)
    reclaim_expired = repository.reclaim_expired_jobs

    def reclaim_then_compete(now: datetime) -> int:
        reclaimed = reclaim_expired(now)
        claimed = competitor.claim_next_job("competitor", now, lease_seconds=60)
        assert claimed is not None and claimed.id == job.id
        return reclaimed

    monkeypatch.setattr(repository, "reclaim_expired_jobs", reclaim_then_compete)
    worker = IndexWorker(
        repository,
        NoopIndexingService(),
        worker_id="recovery-worker",
        lease_seconds=60,
        now=lambda: NOW,
    )

    report = worker.recover_interrupted()
    recovered = repository.get_job(job.id)
    document = repository.get_document_by_source_revision(
        job.store_id,
        job.source_revision_id,
    )

    assert report == RecoveryReport(reclaimed_leases=1)
    assert recovered is not None
    assert recovered.state is IndexState.IMPORTING
    assert recovered.lease_owner == "competitor"
    assert document is not None
    assert document.state is IndexState.IMPORTING
