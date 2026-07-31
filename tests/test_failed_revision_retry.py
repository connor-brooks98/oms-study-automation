import hashlib

from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.ingestion.domain import StagedUpload, UploadKind, UploadState
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.repositories import CatalogRepository, LectureInput


def _failed_revision(repository, catalog, tmp_path, *, item_id="failed-transcript"):
    lecture_id = catalog.upsert_lecture(
        LectureInput("Neuro", 1, 1, "Seizures", "Dr Test", None)
    )
    payload = b"Transcript that needs cleaning"
    staged = tmp_path / f"{item_id}.ready"
    staged.write_bytes(payload)
    batch_id = repository.create_batch(UploadKind.TRANSCRIPTS)
    repository.add_item(
        UploadKind.TRANSCRIPTS,
        StagedUpload(
            batch_id=batch_id,
            item_id=item_id,
            path=staged,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            original_filename=f"{item_id}.txt",
        ),
    )
    repository.set_manual_assignment(item_id, lecture_id)
    revision = repository.begin_revision(item_id, tmp_path / "immutable")
    return repository.finish_revision(
        item_id,
        revision.id,
        UploadState.NEEDS_REVIEW,
        current=False,
        error="cleaned transcript failed validation",
        revision_state="failed",
    )


def test_failed_transcript_retry_requeues_same_durable_job_and_revision(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
    )
    app = create_app(settings)
    repository = IngestionRepository(app.state.database)
    revision = _failed_revision(
        repository,
        CatalogRepository(app.state.database),
        tmp_path,
    )

    failed = repository.list_failed_revisions()
    assert [(item.revision.id, item.error) for item in failed] == [
        (revision.id, "cleaned transcript failed validation")
    ]
    retried = repository.retry_failed_revision(revision.id)

    assert retried.id == revision.id
    assert retried.state == "retrying"
    assert repository.list_failed_revisions() == []
    assert repository.require_item(revision.upload_item_id).state is UploadState.QUEUED
    assert repository.count_jobs(revision.upload_item_id, "process") == 1
    restarted = repository.begin_revision(revision.upload_item_id, tmp_path / "immutable")
    assert restarted.id == revision.id
    assert restarted.state == "proposed"


def test_review_shows_failure_reason_and_retry_requires_form_csrf(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path / "data",
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    revision = _failed_revision(
        app.state.ingestion_repository,
        app.state.catalog_repository,
        tmp_path,
    )
    client = TestClient(app)
    health = client.get("/health")
    assert health.status_code == 200
    token = client.cookies.get("study_hub_csrf")
    assert token is not None

    review = client.get("/review")
    assert review.status_code == 200
    assert "cleaned transcript failed validation" in review.text
    assert f'/review/replacements/{revision.id}/retry' in review.text

    missing = client.post(
        f"/review/replacements/{revision.id}/retry",
        data={},
        headers={"Origin": "http://testserver"},
    )
    assert missing.status_code == 403

    accepted = client.post(
        f"/review/replacements/{revision.id}/retry",
        data={"csrf_token": token},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    assert accepted.headers["location"] == "/review"
