import hashlib

from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.ingestion.domain import StagedUpload, UploadKind
from oms_hub.models import StudyRevisionModel
from oms_hub.repositories import LectureInput


def test_lecture_page_shows_separate_outline_and_quiz_controls(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
            study_root=tmp_path / "study",
        )
    )
    lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 1, 1, "Seizures", "Faculty", None)
    )

    page = TestClient(app).get(f"/lectures/{lecture_id}")

    assert page.status_code == 200
    assert "Generate Outline" in page.text
    assert "Generate Quiz" in page.text
    assert "Lecture Outline (PDF)" in page.text
    assert "Lecture Quiz" in page.text
    assert "Built from this lecture's PDF and cleaned transcript only." in page.text
    assert "Gemini Quiz Gem" not in page.text
    assert (
        f'href="/uploads/slides?lecture_id={lecture_id}"'
        in page.text
    )
    assert "Upload Lecture PPTX" in page.text
    assert (
        f'href="/uploads/transcripts?lecture_id={lecture_id}"'
        in page.text
    )
    assert "Upload Lecture Transcript" in page.text
    assert "Lecture Summary" in page.text
    assert "Lecture Quiz Generation" in page.text


def test_lecture_page_does_not_mark_a_changed_pdf_as_ready(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path / "data",
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
            study_root=tmp_path / "study",
        )
    )
    lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 1, 1, "Seizures", "Faculty", None)
    )
    changed_pdf = tmp_path / "changed.pdf"
    changed_pdf.write_bytes(b"changed after filing")
    repository = app.state.ingestion_repository
    batch_id = repository.create_batch(UploadKind.SLIDES)
    repository.add_item(
        UploadKind.SLIDES,
        StagedUpload(
            batch_id=batch_id,
            item_id="changed-slide",
            path=changed_pdf,
            sha256=hashlib.sha256(b"source").hexdigest(),
            size_bytes=len(b"source"),
            original_filename="lecture.pptx",
        ),
    )
    with app.state.database.session() as session:
        session.add(
            StudyRevisionModel(
                upload_item_id="changed-slide",
                lecture_id=lecture_id,
                kind=UploadKind.SLIDES.value,
                source_sha256=hashlib.sha256(b"source").hexdigest(),
                immutable_source_path=str(changed_pdf),
                derived_sha256=hashlib.sha256(b"original pdf").hexdigest(),
                immutable_derived_path=str(changed_pdf),
                canonical_source_path=str(changed_pdf),
                canonical_derived_path=str(changed_pdf),
                state="current",
                current=True,
            )
        )

    page = TestClient(app).get(f"/lectures/{lecture_id}")

    assert page.status_code == 200
    assert "Lecture PDF file checksum does not match." in page.text
    assert "Open Lecture PDF" not in page.text


def test_dashboard_has_separate_slide_and_transcript_uploads(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )

    page = TestClient(app).get("/")

    assert 'href="/uploads/slides"' in page.text
    assert "+ Upload lecture" in page.text
    assert 'href="/uploads/transcripts"' in page.text
    assert "+ Upload transcript" in page.text


def test_lecture_upload_page_targets_the_selected_lecture(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 1, 1, "Seizures", "Faculty", None)
    )

    page = TestClient(app).get(
        f"/uploads/transcripts?lecture_id={lecture_id}"
    )

    assert page.status_code == 200
    assert "Neuro Lecture 01 · Seizures" in page.text
    assert f'name="lecture_id" value="{lecture_id}"' in page.text


def test_targeted_upload_is_assigned_to_the_selected_lecture(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 1, 1, "Seizures", "Faculty", None)
    )
    client = TestClient(app)

    upload = client.post(
        "/uploads/transcripts",
        data={"lecture_id": str(lecture_id)},
        files={"files": ("lecture.txt", b"Lecture transcript", "text/plain")},
    )
    batch = client.get(
        f"/api/upload-batches/{upload.json()['batch_id']}"
    ).json()

    assert upload.status_code == 202
    assert batch["items"][0]["lecture_id"] == lecture_id
