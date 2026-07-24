from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.canvas.classifier import classify_attachment
from oms_hub.canvas.domain import CatalogMatch, CourseMappingInput
from oms_hub.canvas.pairing import PairingService
from oms_hub.config import Settings
from oms_hub.files.atomic import sha256_file
from oms_hub.repositories import CatalogRepository, LectureInput
from tests.canvas.test_classifier import attachment
from tests.canvas.test_pairing import MemorySecretStore


def client_for(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'web.db'}",
        )
    )
    app.state.canvas_pairing = PairingService(
        app.state.canvas_repository,
        MemorySecretStore(),
    )
    return TestClient(app), app


def test_setup_shows_ordered_readiness_checks(tmp_path) -> None:
    client, _ = client_for(tmp_path)
    response = client.get("/setup?detail=canvas")
    assert response.status_code == 200
    labels = [
        "Extension paired",
        "Eight courses mapped",
        "Local study folder confirmed",
        "iCloud staging folder confirmed",
        "Discovery scan completed",
        "Discovery preview confirmed",
        "Automatic processing enabled",
    ]
    positions = [response.text.index(label) for label in labels]
    assert positions == sorted(positions)


def test_auto_processing_cannot_be_enabled_before_setup(tmp_path) -> None:
    client, app = client_for(tmp_path)
    response = client.post("/canvas/enable")
    assert response.status_code == 409
    assert app.state.canvas_repository.connection().auto_process is False


def test_zero_item_scan_cannot_be_confirmed(tmp_path) -> None:
    client, app = client_for(tmp_path)
    app.state.canvas_repository.heartbeat(
        "connected",
        scan_complete=True,
        item_count=0,
    )

    response = client.post("/canvas/confirm-preview")

    assert response.status_code == 409
    assert "at least one item" in response.json()["detail"]
    assert app.state.canvas_repository.connection().discovery_confirmed is False


def test_zero_item_scan_cannot_enable_automatic_processing(tmp_path) -> None:
    client, app = client_for(tmp_path)
    repository = app.state.canvas_repository
    repository.set_pairing("extension", "fingerprint")
    repository.replace_course_mappings(
        [
            CourseMappingInput(str(index), f"Course {index}", f"C{index}", subject)
            for index, subject in enumerate(
                ["Neuro", "MSK", "OPP", "EPC", "Heme/Lymph", "Cardio", "Renal", "Resp"],
                start=1,
            )
        ]
    )
    repository.heartbeat("connected", scan_complete=True, item_count=0)
    repository.set_setup(
        study_root=str(tmp_path / "study"),
        icloud_staging_root=str(tmp_path / "icloud"),
        discovery_confirmed=True,
    )

    response = client.post("/canvas/enable")

    assert response.status_code == 409
    assert repository.connection().auto_process is False


def test_automatic_processing_can_be_paused(tmp_path) -> None:
    client, app = client_for(tmp_path)
    app.state.canvas_repository.set_setup(auto_process=True)

    response = client.post("/canvas/disable", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/setup?detail=canvas"
    assert app.state.canvas_repository.connection().auto_process is False


def test_setup_shows_pause_control_while_automatic_processing_is_enabled(tmp_path) -> None:
    client, app = client_for(tmp_path)
    app.state.canvas_repository.set_setup(auto_process=True)

    response = client.get("/setup?detail=canvas")

    assert response.status_code == 200
    assert 'action="/canvas/disable"' in response.text
    assert "Pause automatic processing" in response.text


def test_dashboard_rejects_cross_site_form_posts(tmp_path) -> None:
    client, _ = client_for(tmp_path)
    response = client.post(
        "/canvas/enable",
        headers={"Origin": "https://malicious.example"},
    )
    assert response.status_code == 403


def test_eight_discovered_courses_can_be_mapped(tmp_path) -> None:
    client, app = client_for(tmp_path)
    candidates = [
        {"course_id": str(index), "course_name": f"Course {index}", "course_code": f"C{index}"}
        for index in range(1, 9)
    ]
    app.state.canvas_repository.set_course_candidates(candidates)
    response = client.post(
        "/canvas/mappings",
        data={
            "course_neuro": "1",
            "course_neuro_enabled": "on",
            "course_msk": "2",
            "course_opp": "3",
            "course_epc": "4",
            "course_heme": "5",
            "course_cardio": "6",
            "course_renal": "7",
            "course_resp": "8",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    mappings = app.state.canvas_repository.list_course_mappings()
    assert len(mappings) == 8
    assert [item.subject for item in mappings if item.enabled] == ["Neuro"]


def test_setup_shows_per_course_scan_controls(tmp_path) -> None:
    client, app = client_for(tmp_path)
    candidates = [
        {"course_id": str(index), "course_name": f"Course {index}", "course_code": f"C{index}"}
        for index in range(1, 9)
    ]
    app.state.canvas_repository.set_course_candidates(candidates)

    response = client.get("/setup?detail=canvas")

    assert response.status_code == 200
    assert 'name="course_neuro_enabled"' in response.text
    assert "Include Neuro in scans and processing" in response.text


def test_navigation_has_one_consolidated_setup_entry(tmp_path) -> None:
    client, _ = client_for(tmp_path)

    response = client.get("/")

    assert 'href="/setup"' in response.text
    assert 'href="/canvas/setup"' not in response.text
    assert 'href="/panopto/setup"' not in response.text


def failed_revision(app, tmp_path):
    repository = app.state.canvas_repository
    repository.replace_course_mappings(
        [CourseMappingInput("751", "Hematology & Lymph", "HEME", "Heme/Lymph")]
    )
    lecture_id = CatalogRepository(app.state.database).upsert_lecture(
        LectureInput("Heme/Lymph", 1, 4, "Anemia I", "Professor", None)
    )
    value = attachment("Anemia.pptx")
    stored = repository.ingest_metadata(
        value,
        classify_attachment(value),
        CatalogMatch(lecture_id, "Heme/Lymph", 1, 0.99, "exact"),
    )
    source = tmp_path / "Anemia.pptx"
    source.write_bytes(b"PK-source")
    repository.complete_ingestion(stored.revision_id, sha256_file(source), str(source))
    repository.mark_revision_review(stored.revision_id, "PowerPoint export failed")
    return stored.revision_id


def test_review_shows_failed_revision_and_retry_control(tmp_path) -> None:
    client, app = client_for(tmp_path)
    revision_id = failed_revision(app, tmp_path)

    response = client.get("/canvas/review")

    assert response.status_code == 200
    assert "PowerPoint export failed" in response.text
    assert f'action="/canvas/revisions/{revision_id}/retry"' in response.text


def test_retry_control_requeues_failed_revision(tmp_path) -> None:
    client, app = client_for(tmp_path)
    revision_id = failed_revision(app, tmp_path)

    response = client.post(
        f"/canvas/revisions/{revision_id}/retry",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert app.state.canvas_repository.get_revision(revision_id).state == "downloaded"
    assert app.state.canvas_repository.list_review_items() == []
