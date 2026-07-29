from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.repositories import LectureInput


def test_generation_status_is_safe_and_not_cacheable(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 1, 1, "Seizures", "", None)
    )

    response = TestClient(app).get(
        f"/lectures/{lecture_id}/generation-status"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["outline"]["state"] == "ready"
    assert response.json()["quiz"]["state"] == "ready"
