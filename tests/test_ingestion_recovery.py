import hashlib
from datetime import UTC, datetime

from oms_hub.db import Database
from oms_hub.ingestion.domain import StagedUpload, UploadKind, UploadState
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.models import IngestionJobModel
from oms_hub.repositories import CatalogRepository, LectureInput


def _queued_repository(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    lecture_id = CatalogRepository(database).upsert_lecture(
        LectureInput("Neuro", 1, 1, "Seizures", "Dr Test", None)
    )
    payload = b"Recoverable transcript"
    staged = tmp_path / "transcript.ready"
    staged.write_bytes(payload)
    repository = IngestionRepository(database)
    batch_id = repository.create_batch(UploadKind.TRANSCRIPTS)
    repository.add_item(
        UploadKind.TRANSCRIPTS,
        StagedUpload(
            batch_id=batch_id,
            item_id="recoverable-item",
            path=staged,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            original_filename="transcript.txt",
        ),
    )
    repository.set_manual_assignment("recoverable-item", lecture_id)
    return database, repository, batch_id


def test_recovery_requeues_processing_job_item_and_batch(tmp_path):
    database, repository, batch_id = _queued_repository(tmp_path)
    claimed = repository.claim_next_job(datetime.now(UTC))
    assert claimed is not None
    assert repository.require_item(claimed.upload_item_id).state is UploadState.PROCESSING

    assert repository.recover_interrupted_jobs() == 1

    item = repository.require_item(claimed.upload_item_id)
    assert item.state is UploadState.QUEUED
    assert item.error == "requeued after an interrupted Hub process"
    batch = repository.get_batch(batch_id)
    assert batch is not None
    assert batch.state is UploadState.QUEUED
    with database.session() as session:
        job = session.get(IngestionJobModel, claimed.id)
        assert job is not None
        assert job.state == UploadState.QUEUED.value
        assert job.next_attempt_at is None

    reclaimed = repository.claim_next_job(datetime.now(UTC))
    assert reclaimed is not None
    assert reclaimed.id == claimed.id
    assert reclaimed.attempts == claimed.attempts + 1


def test_recovery_is_noop_without_interrupted_jobs(tmp_path):
    _, repository, _ = _queued_repository(tmp_path)

    assert repository.recover_interrupted_jobs() == 0
    assert repository.require_item("recoverable-item").state is UploadState.QUEUED
