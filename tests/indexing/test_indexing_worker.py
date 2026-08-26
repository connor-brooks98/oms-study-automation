from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from oms_hub.db import Database
from oms_hub.indexing.models import (
    IndexJob,
    IndexState,
    ProviderDocument,
    ProviderStore,
    StoreKey,
)
from oms_hub.indexing.repository import IndexRepository
from oms_hub.indexing.service import IndexingInputError, IndexResult
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
_OPEN_DATABASES: list[Database] = []


@pytest.fixture(autouse=True)
def _close_databases() -> Iterator[None]:
    yield
    while _OPEN_DATABASES:
        _OPEN_DATABASES.pop().close()


def _open_database(url: str) -> Database:
    database = Database(url)
    _OPEN_DATABASES.append(database)
    return database


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
    database = _open_database(f"sqlite:///{tmp_path / 'hub.db'}")
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
        provider_document_name: str | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = block
        self.result_state = result_state
        self.provider_document_name = provider_document_name
        self.admin: object | None = None

    async def index_revision(self, source_revision_id: str) -> IndexResult:
        self.calls.append(source_revision_id)
        self.started.set()
        if self.block:
            assert self.release.wait(timeout=5)
        return IndexResult(
            source_revision_id,
            self.result_state,
            self.provider_document_name,
        )


class FakeAdmin:
    def __init__(self) -> None:
        self.delete_document_calls: list[str] = []

    async def delete_document(self, provider_document_id: str) -> None:
        self.delete_document_calls.append(provider_document_id)


class _ReplacingRepository(IndexRepository):
    def __init__(
        self,
        database: Database,
        successor: IndexRepository,
        clock: list[datetime],
        replace_state: IndexState,
    ) -> None:
        super().__init__(database)
        self.successor = successor
        self.clock = clock
        self.replace_state = replace_state
        self.replacement: IndexJob | None = None

    def save_claimed_job(
        self,
        job: IndexJob,
        lease_owner: str,
        *,
        now: datetime | None = None,
    ) -> IndexJob | None:
        saved = super().save_claimed_job(job, lease_owner, now=now)
        if saved is not None and job.state is self.replace_state and self.replacement is None:
            self.clock[0] += timedelta(seconds=2)
            self.replacement = self.successor.claim_job(
                job.id,
                "worker-b",
                self.clock[0],
                lease_seconds=60,
            )
            assert self.replacement is not None
        return saved


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


def test_default_lease_covers_provider_request_and_operation_deadlines(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    repository = IndexRepository(database)
    service = FakeIndexingService()
    service.admin = SimpleNamespace(
        client_factory=SimpleNamespace(
            config=SimpleNamespace(
                request_timeout_seconds=120,
                operation_timeout_seconds=900,
            )
        )
    )

    worker = IndexWorker(repository, service, now=lambda: NOW)

    assert worker.lease_seconds > 120 + 900


def test_two_workers_claim_one_source_revision_exclusively(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'hub.db'}"
    database = _open_database(url)
    database.migrate()
    job = _queued_job(IndexRepository(database))
    first_service = FakeIndexingService(block=True)
    second_service = FakeIndexingService()
    first = _worker(IndexRepository(_open_database(url)), first_service, "worker-1")
    second = _worker(IndexRepository(_open_database(url)), second_service, "worker-2")

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


def test_stale_success_cannot_overwrite_newer_reclaimed_lease(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'hub.db'}"
    database = _open_database(url)
    database.migrate()
    repository = IndexRepository(database)
    job = _queued_job(repository)
    first_service = FakeIndexingService(block=True)
    second_service = FakeIndexingService(block=True)
    first = IndexWorker(
        IndexRepository(_open_database(url)),
        first_service,
        worker_id="shared-worker-id",
        lease_seconds=1,
        now=lambda: NOW,
    )
    second = IndexWorker(
        IndexRepository(_open_database(url)),
        second_service,
        worker_id="shared-worker-id",
        lease_seconds=60,
        now=lambda: NOW.replace(second=2),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first.run_once)
        assert first_service.started.wait(timeout=5)
        second_future = pool.submit(second.run_once)
        assert second_service.started.wait(timeout=5)
        newer_claim = repository.get_job(job.id)
        assert newer_claim is not None and newer_claim.lease_owner is not None

        first_service.release.set()
        assert first_future.result(timeout=5).job_id == job.id
        after_stale_success = repository.get_job(job.id)

        assert after_stale_success is not None
        assert after_stale_success.state is IndexState.NOT_INDEXED
        assert after_stale_success.lease_owner == newer_claim.lease_owner

        second_service.release.set()
        assert second_future.result(timeout=5).job_id == job.id

    final = repository.get_job(job.id)
    assert final is not None
    assert final.state is IndexState.READY
    assert final.lease_owner is None


def test_success_persists_ready_state_and_releases_lease(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = IndexRepository(database)
    job = _queued_job(repository)
    service = FakeIndexingService(
        provider_document_name="fileSearchStores/store-1/documents/document-1"
    )

    result = _worker(repository, service, "worker-1").run_once()
    stored = repository.get_job(job.id)

    assert result == WorkResult(worked=True, job_id=job.id)
    assert stored is not None
    assert stored.state is IndexState.READY
    assert stored.provider_document_id == service.provider_document_name
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


@pytest.mark.parametrize(
    "returned_state", (IndexState.RETRYABLE_FAILURE, IndexState.TERMINAL_FAILURE)
)
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


@pytest.mark.parametrize(
    ("returned_state", "category"),
    (
        (IndexState.RETRYABLE_FAILURE, "quota"),
        (IndexState.TERMINAL_FAILURE, "contract"),
    ),
)
def test_returned_failure_uses_persisted_provider_category(
    tmp_path: Path,
    returned_state: IndexState,
    category: str,
) -> None:
    database = _database(tmp_path)
    repository = IndexRepository(database)
    job = _queued_job(repository)

    class PersistingFailureService:
        async def index_revision(self, source_revision_id: str) -> IndexResult:
            repository.save_document(
                ProviderDocument(
                    store_id=job.store_id,
                    provider="gemini",
                    provider_document_id=None,
                    source_revision_id=source_revision_id,
                    state=returned_state,
                    last_error_category=category,
                )
            )
            return IndexResult(source_revision_id, returned_state)

    result = IndexWorker(
        repository,
        PersistingFailureService(),
        worker_id="worker-1",
        lease_seconds=60,
        now=lambda: NOW,
    ).run_once()
    stored = repository.get_job(job.id)

    assert result == WorkResult(worked=True, job_id=job.id)
    assert stored is not None
    assert stored.state is returned_state
    assert stored.last_error_category == category
    document = repository.get_document_by_source_revision(
        job.store_id,
        job.source_revision_id,
    )
    assert document is not None
    assert document.last_error_category == category


def test_file_size_input_failure_is_terminal_with_stable_category(tmp_path: Path) -> None:
    class OversizedService:
        async def index_revision(self, _source_revision_id: str) -> IndexResult:
            raise IndexingInputError("canonical source exceeds the provider size limit")

    database = _database(tmp_path)
    repository = IndexRepository(database)
    job = _queued_job(repository)

    result = IndexWorker(
        repository,
        OversizedService(),
        worker_id="worker-1",
        lease_seconds=60,
        now=lambda: NOW,
    ).run_once()
    stored = repository.get_job(job.id)

    assert result == WorkResult(worked=True, job_id=job.id)
    assert stored is not None
    assert stored.state is IndexState.TERMINAL_FAILURE
    assert stored.last_error_category == "file-too-large"


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


def test_unexpected_error_persists_only_its_type(tmp_path: Path) -> None:
    class FailingService:
        async def index_revision(self, _source_revision_id: str) -> IndexResult:
            raise RuntimeError("private-provider-payload-token")

    database = _database(tmp_path)
    repository = IndexRepository(database)
    job = _queued_job(repository)

    IndexWorker(
        repository,
        FailingService(),
        worker_id="worker-1",
        lease_seconds=60,
        now=lambda: NOW,
    ).run_once()
    stored = repository.get_job(job.id)

    assert stored is not None
    assert stored.state is IndexState.TERMINAL_FAILURE
    assert stored.last_error_message == "RuntimeError"


def test_stale_lease_cannot_overwrite_or_release_new_claim(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = IndexRepository(database)
    job = _queued_job(repository)
    first = repository.claim_next_job("worker:claim-1", NOW, lease_seconds=1)
    second = repository.claim_next_job(
        "worker:claim-2",
        NOW.replace(second=2),
        lease_seconds=60,
    )

    assert first is not None
    assert second is not None
    assert repository.save_claimed_job(
        replace(first, state=IndexState.READY),
        "worker:claim-1",
    ) is None
    assert repository.release_job_lease(job.id, "worker:claim-1") is False
    stored = repository.get_job(job.id)
    assert stored is not None
    assert stored.state is IndexState.NOT_INDEXED
    assert stored.lease_owner == "worker:claim-2"


def test_replaced_terminal_worker_cannot_terminalize_provider_document(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'terminal-fence.db'}"
    bootstrap = _open_database(url)
    bootstrap.migrate()
    successor = IndexRepository(_open_database(url))
    clock = [NOW]
    repository = _ReplacingRepository(
        _open_database(url),
        successor,
        clock,
        IndexState.TERMINAL_FAILURE,
    )
    job = _queued_job(repository)
    repository.save_document(
        ProviderDocument(
            store_id=job.store_id,
            provider="gemini",
            provider_document_id=None,
            source_revision_id=job.source_revision_id,
            state=IndexState.UPLOADING_FILE,
        )
    )
    worker = IndexWorker(
        repository,
        FakeIndexingService(result_state=IndexState.TERMINAL_FAILURE),
        worker_id="worker-a",
        lease_seconds=1,
        now=lambda: clock[0],
    )

    worker.run_once()

    document = successor.get_document_by_source_revision(job.store_id, job.source_revision_id)
    stored_job = successor.get_job(job.id)
    assert repository.replacement is not None
    assert document is not None and document.state is IndexState.UPLOADING_FILE
    assert stored_job is not None and stored_job.lease_owner == "worker-b"


def test_replaced_recovery_worker_cannot_rewind_provider_document(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'recovery-fence.db'}"
    bootstrap = _open_database(url)
    bootstrap.migrate()
    successor = IndexRepository(_open_database(url))
    clock = [NOW]
    repository = _ReplacingRepository(
        _open_database(url),
        successor,
        clock,
        IndexState.FILE_UPLOADED,
    )
    store = repository.create_store(_store())
    job = repository.save_job(
        IndexJob(
            store_id=store.id,
            source_revision_id="sr_recovery_fence",
            state=IndexState.IMPORTING,
        )
    )
    repository.save_document(
        ProviderDocument(
            store_id=store.id,
            provider="gemini",
            provider_document_id=None,
            source_revision_id=job.source_revision_id,
            state=IndexState.IMPORTING,
        )
    )
    expired = repository.claim_job(
        job.id,
        "crashed-worker",
        NOW - timedelta(seconds=2),
        lease_seconds=1,
    )
    assert expired is not None
    worker = IndexWorker(
        repository,
        FakeIndexingService(),
        worker_id="worker-a",
        lease_seconds=1,
        now=lambda: clock[0],
    )

    worker.recover_interrupted()

    document = successor.get_document_by_source_revision(store.id, job.source_revision_id)
    stored_job = successor.get_job(job.id)
    assert repository.replacement is not None
    assert document is not None and document.state is IndexState.IMPORTING
    assert stored_job is not None and stored_job.lease_owner == "worker-b"
