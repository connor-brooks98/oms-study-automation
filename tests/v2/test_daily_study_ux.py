from fastapi.testclient import TestClient
from selectolax.parser import HTMLParser

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.repositories import LectureInput


def app_with_lecture(tmp_path):
    app = create_app(
        Settings(_env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'hub.db'}")
    )
    lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Heme/Lymph", 3, 28, "Lymphoma", "", None)
    )
    return app, lecture_id


def test_home_defaults_to_library_and_provides_valid_recent_catalog(tmp_path):
    app, lecture_id = app_with_lecture(tmp_path)
    page = TestClient(app).get("/")
    document = HTMLParser(page.text)

    recent = document.css_first("[data-recent-lecture]")
    assert recent is not None
    assert recent.css_first("[data-recent-title]").text(strip=True) == "Choose a lecture"
    assert recent.css_first("[data-recent-link]").attributes["href"] == "/lectures"
    assert f'"id": {lecture_id}' in recent.attributes["data-lectures"]


def test_library_exposes_searchable_material_and_study_states(tmp_path):
    app, _ = app_with_lecture(tmp_path)
    document = HTMLParser(TestClient(app).get("/lectures").text)

    assert document.css_first("input[data-lecture-search]").attributes["type"] == "search"
    row = document.css_first("[data-lecture-row]")
    assert "heme/lymph exam 3 lecture 28" in row.attributes["data-search-text"].lower()
    assert row.css_first(".lecture-title small").text(strip=True) == "Study passes 0/5"
    assert (
        row.css_first(".row-progress").attributes["aria-label"] == "Materials processing 0 percent"
    )


def test_lecture_omits_study_actions_and_keeps_materials_and_pass_tracker(tmp_path):
    app, lecture_id = app_with_lecture(tmp_path)
    document = HTMLParser(TestClient(app).get(f"/lectures/{lecture_id}").text)

    assert document.css_first(".lecture-study-actions") is None
    assert len(document.css(".file-card-grid .file-card")) == 4
    assert document.css_first("#pass-tracker [data-pass-count]").text(strip=True) == "0/5"
    pipeline = document.css_first(".pipeline-card")
    assert pipeline.tag == "details" and "open" not in pipeline.attributes
    assert (
        document.css_first("[data-pass-count]")
        .parent.text(strip=True)
        .endswith("completed / configured")
    )


def test_exam_passes_explains_configured_count_and_keeps_dynamic_rows(tmp_path):
    app, _ = app_with_lecture(tmp_path)
    page = TestClient(app).get("/lectures/exams/3/passes", params={"subject": "Heme/Lymph"})

    assert "completed / configured passes" in page.text
    assert "seven-pass target" in page.text
    assert "/studio?subject=Heme/Lymph&amp;exam=3&amp;workflow=import" in page.text
