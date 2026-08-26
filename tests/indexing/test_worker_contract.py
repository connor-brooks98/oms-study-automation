from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy import inspect, text

from oms_hub.db import Database
from oms_hub.indexing.models import IndexJob, IndexState, ProviderStore, StoreKey
from oms_hub.indexing.models import ProviderDocument
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


def test_migration_replaces_single_document_uniqueness_without_losing_rows(
    tmp_path,
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'legacy-inputs.db'}")
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE provider_stores ("
                "id VARCHAR(36) PRIMARY KEY, store_key VARCHAR(255) NOT NULL, "
                "provider VARCHAR(50) NOT NULL, provider_store_name VARCHAR(500) NOT NULL, "
                "embedding_model VARCHAR(200) NOT NULL, authority_namespace VARCHAR(100) NOT NULL, "
                "course_id VARCHAR(100) NOT NULL, exam_id VARCHAR(100), state VARCHAR(30) NOT NULL, "
                "generation INTEGER NOT NULL, is_current BOOLEAN NOT NULL, "
                "created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE provider_documents ("
                "id VARCHAR(36) PRIMARY KEY, store_id VARCHAR(36) NOT NULL, "
                "provider VARCHAR(50) NOT NULL, provider_document_id VARCHAR(500), "
                "source_revision_id VARCHAR(200) NOT NULL, provider_file_name VARCHAR(500), "
                "provider_document_name VARCHAR(500), provider_operation_name VARCHAR(500), "
                "input_byte_count INTEGER, metadata_json TEXT NOT NULL, state VARCHAR(30) NOT NULL, "
                "retry_count INTEGER NOT NULL, last_error_category VARCHAR(100), "
                "created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL, "
                "CONSTRAINT uq_provider_documents_store_revision "
                "UNIQUE (store_id, source_revision_id))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO provider_stores VALUES ("
                "'store-1','course:course-1:exam:exam-1','gemini','stores/1','model',"
                "'course_material','course-1','exam-1','ready',1,1,'now','now')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO provider_documents VALUES ("
                "'doc-1','store-1','gemini','documents/1','sr_1',NULL,NULL,NULL,NULL,"
                "'{}','ready',0,NULL,'now','now')"
            )
        )

    database.migrate()
    repository = IndexRepository(database)
    existing = repository.get_document_by_source_revision("store-1", "sr_1")
    assert existing is not None and existing.input_key == "pptx"
    repository.save_document(
        ProviderDocument(
            store_id="store-1",
            provider="gemini",
            provider_document_id="documents/2",
            source_revision_id="sr_1",
            input_key="normalized_markdown",
            input_kind="markdown",
            input_sha256="c" * 64,
        )
    )
    assert len(repository.list_documents("store-1")) == 2
    database.close()


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
