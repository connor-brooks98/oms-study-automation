from pathlib import Path

from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings


def _client(tmp_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        allow_local_access=True,
    )
    return TestClient(create_app(settings))


def test_agent_api_is_absent_on_every_host(tmp_path: Path) -> None:
    client = _client(tmp_path)

    assert client.get("/agent/v1/health").status_code == 404
    assert client.post("/agent/v1/heartbeat", json={}).status_code == 404


def test_disposable_mac_agent_is_not_packaged_or_installed() -> None:
    root = Path(__file__).parents[2]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")

    assert "oms-anki-agent" not in pyproject
    assert "oms_anki_agent" not in pyproject
    assert not any((root / "src" / "oms_anki_agent").glob("*.py"))
    assert not any((root / "scripts" / "macos").glob("*"))
