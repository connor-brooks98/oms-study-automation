from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.canvas.pairing import PairingService
from oms_hub.config import Settings
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
    response = client.get("/canvas/setup")
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
    assert len(app.state.canvas_repository.list_course_mappings()) == 8
