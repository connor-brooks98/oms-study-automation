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

    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert health["deployment_root"] == "unreported"
    assert health["build_revision"] == "unreported"
    page = client.get("/settings")
    assert page.status_code == 200
    assert "Lecture exam tracker" in page.text


def test_health_reports_launcher_provenance_when_supplied(tmp_path):
    root = tmp_path / "release"
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        allow_local_access=True,
        deployment_root=root,
        build_revision="690e1b52e247ca5c937ddda42d59664e39f2889c",
    )

    health = TestClient(create_app(settings)).get("/health").json()

    assert health["deployment_root"] == str(root)
    assert health["build_revision"] == "690e1b52e247ca5c937ddda42d59664e39f2889c"
