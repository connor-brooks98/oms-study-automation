import hashlib
from pathlib import Path

from oms_hub.db import Database
from oms_hub.ingestion.domain import StagedUpload, UploadKind, UploadState
from oms_hub.ingestion.matcher import UploadMatcher
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.ingestion.service import IngestionService
from oms_hub.ingestion.staging import StagingService, UploadRejected
from oms_hub.models import StudyRevisionModel
from oms_hub.repositories import CatalogRepository, LectureInput


def _prepared_repository(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    catalog = CatalogRepository(database)
    lecture_id = catalog.upsert_lecture(
        LectureInput(
            "Cardiology",
            1,
            7,
            "Heart Failure",
            "Dr Test",
            None,
        )
    )
    return database, IngestionRepository(database), lecture_id


def _add_upload(
    repository: IngestionRepository,
    root: Path,
    item_id: str,
    payload: bytes,
) -> tuple[str, Path, str]:
    staged_path = root / f"{item_id}.ready"
    staged_path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    batch_id = repository.create_batch(UploadKind.TRANSCRIPTS)
    repository.add_item(
        UploadKind.TRANSCRIPTS,
        StagedUpload(
            batch_id=batch_id,
            item_id=item_id,
            path=staged_path,
            sha256=digest,
            size_bytes=len(payload),
            original_filename=f"{item_id}.txt",
        ),
    )
    return batch_id, staged_path, digest


def _add_current_transcript(
    database: Database,
    repository: IngestionRepository,
    root: Path,
    lecture_id: int,
    payload: bytes,
) -> str:
    item_id = "current-transcript"
    _, staged_path, digest = _add_upload(
        repository,
        root,
        item_id,
        payload,
    )
    cleaned_path = root / "current-cleaned.txt"
    cleaned_path.write_text("Cleaned current transcript.", encoding="utf-8")
    with database.session() as session:
        session.add(
            StudyRevisionModel(
                upload_item_id=item_id,
                lecture_id=lecture_id,
                kind=UploadKind.TRANSCRIPTS.value,
                source_sha256=digest,
                immutable_source_path=str(staged_path),
                derived_sha256=hashlib.sha256(
                    cleaned_path.read_bytes()
                ).hexdigest(),
                immutable_derived_path=str(cleaned_path),
                canonical_derived_path=str(cleaned_path),
                state="current",
                current=True,
            )
        )
    return digest


def test_new_lecture_transcript_queues_normally(tmp_path):
    _, repository, lecture_id = _prepared_repository(tmp_path)
    _add_upload(repository, tmp_path, "new-transcript", b"New transcript.")

    repository.set_manual_assignment("new-transcript", lecture_id)

    assert repository.require_item("new-transcript").state is UploadState.QUEUED
    assert repository.count_jobs("new-transcript", "process") == 1


def test_exact_current_transcript_completes_without_a_job(tmp_path):
    database, repository, lecture_id = _prepared_repository(tmp_path)
    payload = b"Already processed transcript."
    _add_current_transcript(
        database,
        repository,
        tmp_path,
        lecture_id,
        payload,
    )
    _add_upload(repository, tmp_path, "exact-transcript", payload)

    repository.set_manual_assignment("exact-transcript", lecture_id)

    assert (
        repository.require_item("exact-transcript").state
        is UploadState.COMPLETE
    )
    assert repository.count_jobs("exact-transcript", "process") == 0


def test_different_transcript_for_cleaned_lecture_awaits_confirmation(
    tmp_path,
):
    database, repository, lecture_id = _prepared_repository(tmp_path)
    _add_current_transcript(
        database,
        repository,
        tmp_path,
        lecture_id,
        b"Original transcript.",
    )
    _add_upload(
        repository,
        tmp_path,
        "different-transcript",
        b"Corrected transcript.",
    )

    repository.set_manual_assignment("different-transcript", lecture_id)

    assert (
        repository.require_item("different-transcript").state
        is UploadState.AWAITING_CONFIRMATION
    )
    assert repository.count_jobs("different-transcript", "process") == 0


def _prepared_paused_service(tmp_path, *, outside_staging=False):
    database, repository, lecture_id = _prepared_repository(tmp_path)
    catalog = CatalogRepository(database)
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    _add_current_transcript(
        database,
        repository,
        tmp_path,
        lecture_id,
        b"Original transcript.",
    )
    upload_root = tmp_path if outside_staging else staging_root
    _, staged_path, _ = _add_upload(
        repository,
        upload_root,
        "paused-transcript",
        b"Corrected transcript.",
    )
    repository.set_manual_assignment("paused-transcript", lecture_id)
    staging = StagingService(staging_root, 1_000_000, 2_000_000)
    service = IngestionService(
        repository,
        catalog,
        UploadMatcher(),
        staging,
    )
    return repository, service, staged_path


def test_confirm_processing_is_idempotent_and_creates_one_job(tmp_path):
    repository, service, _ = _prepared_paused_service(tmp_path)

    first = service.confirm_processing("paused-transcript")
    second = service.confirm_processing("paused-transcript")

    assert first.state is UploadState.QUEUED
    assert second.state is UploadState.QUEUED
    assert repository.count_jobs("paused-transcript", "process") == 1


def test_discard_deletes_staged_file_without_creating_a_job(tmp_path):
    repository, service, staged_path = _prepared_paused_service(tmp_path)

    first = service.discard_item("paused-transcript")
    second = service.discard_item("paused-transcript")

    assert first.state is UploadState.DISCARDED
    assert second.state is UploadState.DISCARDED
    assert not staged_path.exists()
    assert repository.count_jobs("paused-transcript", "process") == 0


def test_discard_rejects_path_outside_staging_and_keeps_item_paused(
    tmp_path,
):
    repository, service, staged_path = _prepared_paused_service(
        tmp_path,
        outside_staging=True,
    )

    try:
        service.discard_item("paused-transcript")
    except UploadRejected as error:
        assert str(error) == "upload path escaped staging root"
    else:
        raise AssertionError("unsafe staged path was accepted")

    assert staged_path.exists()
    assert (
        repository.require_item("paused-transcript").state
        is UploadState.AWAITING_CONFIRMATION
    )
    assert repository.count_jobs("paused-transcript", "process") == 0
