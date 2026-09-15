"""A failed duplicate upload is not a replacement awaiting approval."""

import hashlib

from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.models import IngestionJobModel, StudyRevisionModel, UploadBatchModel, UploadItemModel
from oms_hub.repositories import LectureInput


def test_failed_duplicate_does_not_offer_approval_for_current_transcript(tmp_path):
    app = create_app(Settings(
        _env_file=None, data_dir=tmp_path / "data", study_root=tmp_path / "study",
        database_url=f"sqlite:///{tmp_path / 'hub.db'}", anki_enabled=False,
    ))
    lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Cardio", 1, 1, "Cardiac Cycle", "", None)
    )
    source = tmp_path / "transcript.txt"
    source.write_text("Retained transcript")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with app.state.database.session() as session:
        for item_id, state, backend in (
            ("older-duplicate", "needs_review", "codex_subscription"),
            ("current-upload", "complete", "legacy_api"),
        ):
            error = "The selected model is unavailable." if state == "needs_review" else None
            session.add(UploadBatchModel(id=item_id, kind="transcripts", state=state))
            session.flush()
            session.add(UploadItemModel(
                id=item_id, batch_id=item_id, kind="transcripts", original_filename=source.name,
                staged_path=str(source), sha256=digest, size_bytes=source.stat().st_size,
                state=state, lecture_id=lecture_id, error=error,
            ))
            session.flush()
            session.add(IngestionJobModel(
                upload_item_id=item_id, action="process", backend=backend, state=state,
                error=error,
            ))
        session.add(StudyRevisionModel(
            upload_item_id="current-upload", lecture_id=lecture_id, kind="transcripts",
            source_sha256=digest, immutable_source_path=str(source),
            derived_sha256=digest, immutable_derived_path=str(source),
            canonical_derived_path=str(source), state="current", current=True,
        ))

    response = TestClient(app).get("/review")
    assert response.status_code == 200
    assert "No replacement files need review." in response.text
    assert 'action="/review/replacements/' not in response.text
    assert app.state.ingestion_repository.list_proposed_revisions() == []
    with app.state.database.session() as session:
        older = session.get(UploadItemModel, "older-duplicate")
        assert older.state == "needs_review"
        assert older.error == "The selected model is unavailable."
