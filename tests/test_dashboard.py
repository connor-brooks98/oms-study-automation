from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.domain import LectureStepName, StepStatus
from oms_hub.repositories import CatalogRepository, LectureInput


def settings_for(tmp_path, name: str) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / name}",
    )


def test_dashboard_shows_lecture_progress_and_review_count(tmp_path):
    app = create_app(settings_for(tmp_path, "web.db"))
    repository = CatalogRepository(app.state.database)
    lecture_id = repository.upsert_lecture(
        LectureInput(
            "Heme/Lymph",
            1,
            4,
            "Anemia I",
            "Jun Wang, MD, PhD",
            "2026-07-03",
        )
    )
    repository.set_step_status(
        lecture_id,
        LectureStepName.OUTLOOK_MATCHED,
        StepStatus.NEEDS_REVIEW,
        "confirm event",
    )

    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert "Lecture 04: Anemia I" in response.text
    assert "1 needs review" in response.text


def test_step_approval_updates_persisted_state(tmp_path):
    app = create_app(settings_for(tmp_path, "approve.db"))
    repository = CatalogRepository(app.state.database)
    lecture_id = repository.upsert_lecture(
        LectureInput(
            "Heme/Lymph",
            1,
            4,
            "Anemia I",
            "Jun Wang, MD, PhD",
            "2026-07-03",
        )
    )
    repository.set_step_status(
        lecture_id,
        LectureStepName.OUTLOOK_MATCHED,
        StepStatus.NEEDS_REVIEW,
    )

    response = TestClient(app).post(
        f"/lectures/{lecture_id}/steps/outlook_matched",
        data={"status": "queued", "detail": "approved"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    lecture = repository.get_lecture(lecture_id)
    assert lecture is not None
    step = next(
        item for item in lecture.steps if item.name == "outlook_matched"
    )
    assert step.status == "queued"


def test_tracker_issue_can_create_corrected_catalog_lecture(tmp_path):
    app = create_app(settings_for(tmp_path, "issue.db"))
    repository = CatalogRepository(app.state.database)
    repository.replace_import_issues(
        [
            (
                "NEURO 1",
                10,
                "ambiguous lecture number",
                '["19?", "Sleep Disorders"]',
            )
        ]
    )
    issue_id = repository.list_import_issues()[0].id

    response = TestClient(app).post(
        f"/review/import-issues/{issue_id}/resolve",
        data={
            "subject": "Neuro",
            "exam_number": "1",
            "lecture_number": "19",
            "topic": "Sleep Disorders",
            "lecturer": "Leah Snodgrass, MD",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert repository.list_import_issues() == []
    assert [
        (item.lecture_number, item.topic)
        for item in repository.list_lectures()
    ] == [(19, "Sleep Disorders")]


def test_api_returns_catalog_and_checklist_without_external_payloads(tmp_path):
    app = create_app(settings_for(tmp_path, "api.db"))
    repository = CatalogRepository(app.state.database)
    repository.upsert_lecture(
        LectureInput("Neuro", 1, 1, "CNS Pathology", "T. Campbell", None)
    )

    response = TestClient(app).get("/api/lectures")

    assert response.status_code == 200
    assert response.json()[0]["topic"] == "CNS Pathology"
    assert "payload_json" not in response.text
    assert "token" not in response.text.lower()
