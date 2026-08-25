from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy import inspect, text

from oms_hub.db import Database
from oms_hub.indexing.models import IndexJob, IndexState, ProviderStore, StoreKey
from oms_hub.indexing.repository import IndexRepository
from oms_hub.runtime import WorkerSupervisor
from oms_hub.workers import (
    GEMINI_INDEX_JOB_TYPE,
    RecoveryReport,
    WorkResult,
    adapt_durable_worker,
    build_worker_registry,
)


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


def test_migration_activates_index_job_lease_contract(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()

    inspector = inspect(database.engine)
    assert {"provider_stores", "provider_documents", "index_jobs"} <= set(
        inspector.get_table_names()
    )
    assert {"lease_owner", "lease_expires_at"} <= {
        column["name"] for column in inspector.get_columns("index_jobs")
    }
    database.close()

    legacy = Database(f"sqlite:///{tmp_path / 'legacy.db'}")
    with legacy.engine.begin() as connection:
        connection.execute(text("CREATE TABLE index_jobs (id VARCHAR(36) PRIMARY KEY)"))
    legacy.migrate()
    assert {"lease_owner", "lease_expires_at"} <= {
        column["name"] for column in inspect(legacy.engine).get_columns("index_jobs")
    }
    legacy.close()


def test_index_job_claim_is_exclusive_and_expired_lease_is_reclaimable(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'hub.db'}"
    database = Database(url)
    database.migrate()
    repository = IndexRepository(database)
    stored = repository.create_store(_store())
    job = repository.save_job(
        IndexJob(
            store_id=stored.id,
            source_revision_id="sr_1",
            state=IndexState.NOT_INDEXED,
        )
    )
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)

    def claim(worker_id: str) -> IndexJob | None:
        with Database(url) as connection:
            return IndexRepository(connection).claim_next_job(
                worker_id,
                now,
                lease_seconds=60,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("worker-1", "worker-2")))

    claimed = [value for value in claims if value is not None]
    assert len(claimed) == 1
    assert claimed[0].id == job.id
    assert claimed[0].lease_owner in {"worker-1", "worker-2"}
    assert claim("worker-3") is None

    reclaimed = repository.reclaim_expired_jobs(now + timedelta(seconds=61))
    assert reclaimed == 1
    with Database(url) as connection:
        next_claim = IndexRepository(connection).claim_next_job(
            "worker-3",
            now + timedelta(seconds=61),
            lease_seconds=60,
        )
    assert next_claim is not None
    assert next_claim.lease_owner == "worker-3"
    database.close()


def test_typed_worker_adapter_and_optional_index_registration() -> None:
    class Worker:
        def run_once(self) -> WorkResult:
            return WorkResult(worked=True, job_id="job-1")

        def recover_interrupted(self) -> RecoveryReport:
            return RecoveryReport(reclaimed_leases=2, resumed_jobs=1)

    adapter = adapt_durable_worker(GEMINI_INDEX_JOB_TYPE, Worker())
    registry = build_worker_registry(
        ingestion_worker=object(),
        generation_worker=object(),
        studio_worker=object(),
        indexing_worker=adapter,
    )

    assert adapter.job_type == "gemini_index_source_revision"
    assert adapter.run_once() is True
    assert adapter.recover_interrupted_jobs() == 3
    assert set(registry) == {
        "ingestion_worker",
        "generation_worker",
        "studio_worker",
        "indexing_worker",
    }
    assert WorkerSupervisor(registry).ready() == (False, "worker_not_started")
