from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings


def test_health_reports_service_and_version(tmp_path):
    settings = Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'hub.db'}")
    client = TestClient(create_app(settings))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "service": "oms-study-automation",
        "status": "ok",
        "version": "0.1.0",
    }
    assert hasattr(client.app.state, "panopto_pipeline")
    assert hasattr(client.app.state, "panopto_discovery")
