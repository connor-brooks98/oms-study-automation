from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.repositories import LectureInput
from tests.support import csrf_client


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
        f'href="/uploads?lecture_id={lecture_id}"'
        in page.text
    )
    assert "Upload Lecture PPTX" in page.text
    assert (
        f'href="/uploads?lecture_id={lecture_id}"'
        in page.text
    )
    assert "Upload Lecture Transcript" in page.text
    assert "Lecture Summary" in page.text
    assert "Lecture Quiz Generation" in page.text


def test_dashboard_has_unified_upload_entry_point(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )

    page = TestClient(app).get("/")

    assert 'href="/uploads"' in page.text
    assert "+ Upload files" in page.text
    assert 'href="/uploads/slides"' not in page.text
    assert 'href="/uploads/transcripts"' not in page.text


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
    assert "Neuro · Lecture 01 · Seizures" in page.text
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
    client = csrf_client(app)

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
