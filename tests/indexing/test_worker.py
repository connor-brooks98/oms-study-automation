from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oms_hub.db import Database
from oms_hub.indexing.models import (
    IndexJob,
    IndexState,
    ProviderStore,
    StoreKey,
)
from oms_hub.indexing.repository import IndexRepository
from oms_hub.indexing.service import IndexResult
from oms_hub.indexing.worker import IndexWorker
from oms_hub.providers.gemini.errors import (
    GeminiAuthenticationError,
    GeminiContractError,
    GeminiProviderError,
    GeminiQuotaError,
    GeminiTransientError,
)
from oms_hub.workers import WorkResult


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


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
    database.migrate()
    return database


def _queued_job(repository: IndexRepository) -> IndexJob:
    store = repository.create_store(_store())
    return repository.save_job(
        IndexJob(
            store_id=store.id,
            source_revision_id="sr_worker_1",
            state=IndexState.NOT_INDEXED,
        )
    )


class FakeIndexingService:
    def __init__(
        self,
        *,
        block: bool = False,
        result_state: IndexState = IndexState.READY,
    ) -> None:
        self.calls: list[str] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = block
        self.result_state = result_state

    async def index_revision(self, source_revision_id: str) -> IndexResult:
        self.calls.append(source_revision_id)
        self.started.set()
        if self.block:
            assert self.release.wait(timeout=5)
        return IndexResult(source_revision_id, self.result_state)


class FakeAdmin:
    def __init__(self) -> None:
        self.delete_document_calls: list[str] = []

    async def delete_document(self, provider_document_id: str) -> None:
        self.delete_document_calls.append(provider_document_id)


def _worker(
    repository: IndexRepository,
    service: FakeIndexingService,
    worker_id: str,
    *,
    max_attempts: int = 3,
    admin: FakeAdmin | None = None,
) -> IndexWorker:
    return IndexWorker(
        repository,
        service,
        admin=admin,
        worker_id=worker_id,
        lease_seconds=60,
        max_attempts=max_attempts,
        now=lambda: NOW,
    )


def test_idle_worker_returns_no_work_result(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = IndexRepository(database)
    service = FakeIndexingService()

    result = _worker(repository, service, "worker-1").run_once()

    assert result == WorkResult(worked=False)
    assert service.calls == []


def test_two_workers_claim_one_source_revision_exclusively(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'hub.db'}"
    database = Database(url)
    database.migrate()
    job = _queued_job(IndexRepository(database))
    first_service = FakeIndexingService(block=True)
    second_service = FakeIndexingService()
    first = _worker(IndexRepository(Database(url)), first_service, "worker-1")
    second = _worker(IndexRepository(Database(url)), second_service, "worker-2")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first.run_once)
        assert first_service.started.wait(timeout=5)
        second_result = pool.submit(second.run_once).result(timeout=5)
        first_service.release.set()
        first_result = first_future.result(timeout=5)

    assert first_result == WorkResult(worked=True, job_id=job.id)
    assert second_result == WorkResult(worked=False)
    assert first_service.calls == [job.source_revision_id]
    assert second_service.calls == []


def test_success_persists_ready_state_and_releases_lease(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = IndexRepository(database)
    job = _queued_job(repository)
    service = FakeIndexingService()

    result = _worker(repository, service, "worker-1").run_once()
    stored = repository.get_job(job.id)

    assert result == WorkResult(worked=True, job_id=job.id)
    assert stored is not None
    assert stored.state is IndexState.READY
    assert stored.lease_owner is None
    assert stored.lease_expires_at is None


@pytest.mark.parametrize(
    ("error", "expected_state"),
    (
        (GeminiAuthenticationError("auth"), IndexState.TERMINAL_FAILURE),
        (GeminiContractError("contract"), IndexState.TERMINAL_FAILURE),
        (
            GeminiProviderError("too large", category="file-too-large"),
            IndexState.TERMINAL_FAILURE,
        ),
        (GeminiQuotaError("rate limited"), IndexState.RETRYABLE_FAILURE),
        (GeminiTransientError("server unavailable"), IndexState.RETRYABLE_FAILURE),
    ),
)
def test_retry_policy_separates_terminal_and_retryable_categories(
    tmp_path: Path,
    error: GeminiProviderError,
    expected_state: IndexState,
) -> None:
    class FailingService:
        async def index_revision(self, _source_revision_id: str) -> IndexResult:
            raise error

    database = _database(tmp_path)
    repository = IndexRepository(database)
    job = _queued_job(repository)

    result = _worker(
        repository,
        FailingService(),  # type: ignore[arg-type]
        "worker-1",
    ).run_once()
    stored = repository.get_job(job.id)

    assert result == WorkResult(worked=True, job_id=job.id)
    assert stored is not None
    assert stored.state is expected_state
    assert stored.last_error_category == error.category
    assert stored.lease_owner is None


@pytest.mark.parametrize("returned_state", (IndexState.RETRYABLE_FAILURE, IndexState.TERMINAL_FAILURE))
def test_worker_handles_failure_state_returned_by_indexing_service(
    tmp_path: Path,
    returned_state: IndexState,
) -> None:
    database = _database(tmp_path)
    repository = IndexRepository(database)
    job = _queued_job(repository)

    result = _worker(
        repository,
        FakeIndexingService(result_state=returned_state),
        "worker-1",
    ).run_once()
    stored = repository.get_job(job.id)

    assert result == WorkResult(worked=True, job_id=job.id)
    assert stored is not None
    assert stored.state is returned_state
    assert stored.lease_owner is None


def test_retry_budget_exhaustion_terminalizes_retryable_failure(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = IndexRepository(database)
    job = _queued_job(repository)
    service = FakeIndexingService(result_state=IndexState.RETRYABLE_FAILURE)

    result = _worker(
        repository,
        service,
        "worker-1",
        max_attempts=1,
    ).run_once()
    stored = repository.get_job(job.id)

    assert result == WorkResult(worked=True, job_id=job.id)
    assert stored is not None
    assert stored.state is IndexState.TERMINAL_FAILURE
    assert stored.retry_count == 1
