from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings


def test_v2_health_and_settings_are_available(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        allow_local_access=True,
    )
    client = TestClient(create_app(settings))

    assert client.get("/health").json()["status"] == "ok"
    page = client.get("/settings")
    assert page.status_code == 200
    assert "Lecture exam tracker" in page.text
