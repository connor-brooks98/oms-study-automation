import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
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

    exact = repository.require_item("exact-transcript")
    assert exact.state is UploadState.COMPLETE
    assert "Exact transcript already processed" in exact.evidence
    assert repository.count_jobs("exact-transcript", "process") == 0


def test_exact_current_slide_queues_repair_when_filed_pdf_checksum_changed(
    tmp_path,
):
    database, repository, lecture_id = _prepared_repository(tmp_path)
    source = b"PowerPoint source"
    source_digest = hashlib.sha256(source).hexdigest()
    original_pdf = b"Original converted PDF"
    original_pdf_digest = hashlib.sha256(original_pdf).hexdigest()
    _, source_path, _ = _add_upload(
        repository,
        tmp_path,
        "current-slide",
        source,
    )
    immutable_pdf = tmp_path / "immutable.pdf"
    immutable_pdf.write_bytes(original_pdf)
    filed_pdf = tmp_path / "filed.pdf"
    filed_pdf.write_bytes(b"Changed after filing")
    with database.session() as session:
        session.add(
            StudyRevisionModel(
                upload_item_id="current-slide",
                lecture_id=lecture_id,
                kind=UploadKind.SLIDES.value,
                source_sha256=source_digest,
                immutable_source_path=str(source_path),
                derived_sha256=original_pdf_digest,
                immutable_derived_path=str(immutable_pdf),
                canonical_source_path=str(source_path),
                canonical_derived_path=str(filed_pdf),
                icloud_path=str(tmp_path / "icloud.pdf"),
                state="current",
                current=True,
            )
        )
    batch_id = repository.create_batch(UploadKind.SLIDES)
    duplicate_path = tmp_path / "duplicate-slide.ready"
    duplicate_path.write_bytes(source)
    repository.add_item(
        UploadKind.SLIDES,
        StagedUpload(
            batch_id=batch_id,
            item_id="duplicate-slide",
            path=duplicate_path,
            sha256=source_digest,
            size_bytes=len(source),
            original_filename="duplicate-slide.pptx",
        ),
    )

    repository.set_manual_assignment("duplicate-slide", lecture_id)

    duplicate = repository.require_item("duplicate-slide")
    assert duplicate.state is UploadState.QUEUED
    assert repository.count_jobs("duplicate-slide", "process") == 1


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


def _prepared_route_client(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{tmp_path / 'route-hub.db'}",
        allow_local_access=True,
    )
    app = create_app(settings)
    repository = app.state.ingestion_repository
    catalog = app.state.catalog_repository
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
    _add_current_transcript(
        app.state.database,
        repository,
        tmp_path,
        lecture_id,
        b"Original transcript.",
    )
    staging_root = app.state.upload_staging.root
    staging_root.mkdir(parents=True, exist_ok=True)
    batch_id, staged_path, _ = _add_upload(
        repository,
        staging_root,
        "route-paused-transcript",
        b"Corrected transcript.",
    )
    repository.set_manual_assignment(
        "route-paused-transcript",
        lecture_id,
    )
    return (
        TestClient(app),
        repository,
        batch_id,
        staged_path,
    )


def test_batch_status_includes_safe_duplicate_warning_metadata(tmp_path):
    client, _, batch_id, _ = _prepared_route_client(tmp_path)

    response = client.get(f"/api/upload-batches/{batch_id}")

    assert response.status_code == 200
    assert response.json()["items"][0]["duplicate_warning"] == {
        "subject": "Cardiology",
        "lecture_number": 7,
        "topic": "Heart Failure",
    }


def test_confirm_route_queues_one_processing_job(tmp_path):
    client, repository, _, _ = _prepared_route_client(tmp_path)

    first = client.post(
        "/api/upload-items/route-paused-transcript/confirm"
    )
    second = client.post(
        "/api/upload-items/route-paused-transcript/confirm"
    )

    assert first.status_code == 200
    assert first.json() == {
        "item_id": "route-paused-transcript",
        "state": "queued",
    }
    assert second.status_code == 200
    assert repository.count_jobs(
        "route-paused-transcript",
        "process",
    ) == 1


def test_discard_route_removes_staged_upload_without_a_job(tmp_path):
    client, repository, _, staged_path = _prepared_route_client(tmp_path)

    first = client.post(
        "/api/upload-items/route-paused-transcript/discard"
    )
    second = client.post(
        "/api/upload-items/route-paused-transcript/discard"
    )

    assert first.status_code == 200
    assert first.json() == {
        "item_id": "route-paused-transcript",
        "state": "discarded",
    }
    assert second.status_code == 200
    assert not staged_path.exists()
    assert repository.count_jobs(
        "route-paused-transcript",
        "process",
    ) == 0


def test_stale_decision_route_returns_conflict(tmp_path):
    client, _, _, _ = _prepared_route_client(tmp_path)
    client.post("/api/upload-items/route-paused-transcript/confirm")

    response = client.post(
        "/api/upload-items/route-paused-transcript/discard"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "upload is not awaiting confirmation"
    )


def test_transcript_upload_page_renders_accessible_duplicate_dialog(
    tmp_path,
):
    client, _, _, _ = _prepared_route_client(tmp_path)

    response = client.get("/uploads/transcripts")

    assert response.status_code == 200
    assert response.text.count("data-duplicate-dialog") == 1
    assert "already been processed for this lecture" in response.text
    assert "data-duplicate-lecture" in response.text
    assert "data-confirm-duplicate" in response.text
    assert "data-discard-duplicate" in response.text
    assert "data-cancel-duplicate" in response.text
    assert "Process anyway" in response.text
    assert "Discard upload" in response.text
    assert "Cancel upload" in response.text
