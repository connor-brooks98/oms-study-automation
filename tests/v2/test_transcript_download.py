import hashlib
from urllib.parse import unquote

from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.domain import LectureKey
from oms_hub.ingestion.domain import StagedUpload, UploadKind
from oms_hub.models import StudyRevisionModel
from oms_hub.repositories import LectureInput
from oms_hub.routing import build_transcript_destination


def _prepared_download(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        study_root=tmp_path / "study",
        allow_local_access=True,
    )
    app = create_app(settings)
    lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput(
            "Cardiology",
            1,
            7,
            "Heart Failure",
            "Dr Test",
            None,
        )
    )
    raw = b"Raw transcript."
    raw_path = tmp_path / "raw.txt"
    raw_path.write_bytes(raw)
    repository = app.state.ingestion_repository
    batch_id = repository.create_batch(UploadKind.TRANSCRIPTS)
    repository.add_item(
        UploadKind.TRANSCRIPTS,
        StagedUpload(
            batch_id=batch_id,
            item_id="download-transcript",
            path=raw_path,
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            original_filename="download-transcript.txt",
        ),
    )
    cleaned_bytes = b"Validated cleaned transcript.\nSecond line.\n"
    cleaned_path = build_transcript_destination(
        settings,
        LectureKey("Cardiology", 1, 7, "Heart Failure"),
    )
    cleaned_path.parent.mkdir(parents=True)
    cleaned_path.write_bytes(cleaned_bytes)
    with app.state.database.session() as session:
        revision = StudyRevisionModel(
            upload_item_id="download-transcript",
            lecture_id=lecture_id,
            kind=UploadKind.TRANSCRIPTS.value,
            source_sha256=hashlib.sha256(raw).hexdigest(),
            immutable_source_path=str(raw_path),
            derived_sha256=hashlib.sha256(cleaned_bytes).hexdigest(),
            immutable_derived_path=str(cleaned_path),
            canonical_derived_path=str(cleaned_path),
            state="current",
            current=True,
        )
        session.add(revision)
        session.flush()
        revision_id = revision.id
    return TestClient(app), revision_id, cleaned_path, cleaned_bytes


def test_cleaned_review_page_offers_transcript_download(tmp_path):
    client, revision_id, _, _ = _prepared_download(tmp_path)

    response = client.get(f"/artifacts/{revision_id}/cleaned")

    assert response.status_code == 200
    assert "Download transcript" in response.text
    assert (
        f'href="/artifacts/{revision_id}/cleaned/download"'
        in response.text
    )


def test_lecture_page_offers_cleaned_download_instead_of_raw_transcript(tmp_path):
    client, revision_id, _, _ = _prepared_download(tmp_path)
    lecture_id = client.app.state.catalog_repository.list_lectures()[0].id

    response = client.get(f"/lectures/{lecture_id}")

    assert response.status_code == 200
    assert "Download Transcript" in response.text
    assert f'href="/artifacts/{revision_id}/cleaned/download"' in response.text
    assert "Open Raw Transcript" not in response.text


def test_download_returns_exact_bytes_with_descriptive_filename(tmp_path):
    client, revision_id, _, cleaned_bytes = _prepared_download(tmp_path)

    response = client.get(
        f"/artifacts/{revision_id}/cleaned/download"
    )

    assert response.status_code == 200
    assert response.content == cleaned_bytes
    assert response.headers["content-type"].startswith("text/plain")
    assert (
        "Cardiology - Lecture 07 - Heart Failure - Transcript.txt"
        in unquote(response.headers["content-disposition"])
    )
    assert response.headers["cache-control"] == "private, no-store"


def test_download_rejects_checksum_mismatched_transcript(tmp_path):
    client, revision_id, cleaned_path, _ = _prepared_download(tmp_path)
    cleaned_path.write_text("Altered after validation.", encoding="utf-8")

    response = client.get(
        f"/artifacts/{revision_id}/cleaned/download"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "artifact checksum does not match its record"
    )
    assert response.content != cleaned_path.read_bytes()
