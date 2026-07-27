from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
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
