from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings


def test_local_mutations_require_csrf_and_forms_include_token(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    client = TestClient(app)
    settings = client.get("/settings")
    token = client.cookies.get("study_hub_csrf")

    assert token is not None
    assert 'name="csrf_token"' in settings.text
    assert client.post("/settings/ai/openai/test").status_code == 403
    accepted = client.post(
        "/settings/ai/openai/test",
        headers={"X-CSRF-Token": token},
    )
    assert accepted.status_code == 200
