import hashlib
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.panopto.browser_domain import BrowserRequestKind


class MemorySecrets:
    def __init__(self):
        self.values = {"openai-api-key": "configured"}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def panopto_client_for(tmp_path):
    prompt_path = tmp_path / "vault" / "Transcript Cleaning.md"
    prompt_path.parent.mkdir()
    prompt_path.write_text("Preserve every substantive fact.", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'panopto-web.db'}",
        transcript_prompt_path=prompt_path,
        panopto_revision_root=tmp_path / "revisions",
        study_root=tmp_path / "OMS II",
    )
    app = create_app(settings)
    app.state.secrets = MemorySecrets()
    app.state.openai_cleaner.secrets = app.state.secrets
    return TestClient(app), app, prompt_path


def test_setup_is_single_default_overview(tmp_path):
    client, _, _ = panopto_client_for(tmp_path)

    page = client.get("/setup")

    assert page.status_code == 200
    assert "Setup Center" in page.text
    assert "Canvas" in page.text
    assert "Panopto" in page.text
    assert "Sign in to Panopto" in page.text
    assert "Test Panopto Connection" in page.text
    assert "Check Panopto command" not in page.text
    assert "client secret" not in page.text.lower()
    assert "redirect" not in page.text.lower()


def test_one_click_test_returns_bridge_request_id(tmp_path):
    client, app, _ = panopto_client_for(tmp_path)

    response = client.post("/setup/panopto/test")

    assert response.status_code == 200
    request_id = response.json()["request_id"]
    request = app.state.panopto_repository.get_browser_request(
        request_id
    )
    assert request is not None
    assert request.kind is BrowserRequestKind.CONNECTION_TEST


def test_setup_status_and_event_stream_are_safe(tmp_path):
    client, _, _ = panopto_client_for(tmp_path)

    status = client.get("/api/setup/status")
    stream = client.get("/api/setup/events?once=true")

    assert status.status_code == 200
    assert set(status.json()) == {"canvas", "panopto", "openai", "prompt"}
    assert "api-key" not in status.text.lower()
    assert "bearer" not in status.text.lower()
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "event: status" in stream.text


def test_legacy_setup_routes_redirect_into_setup_center(tmp_path):
    client, _, _ = panopto_client_for(tmp_path)

    response = client.get("/panopto/setup", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/setup?detail=panopto"


def test_scan_now_queues_instead_of_blocking_on_network(tmp_path):
    client, app, _ = panopto_client_for(tmp_path)

    response = client.post("/panopto/scan", follow_redirects=False)

    assert response.status_code == 303
    request = app.state.panopto_repository.next_browser_request(
        datetime(2026, 7, 23, 14, tzinfo=UTC)
    )
    assert request is not None
    assert request.kind is BrowserRequestKind.SCAN
    assert request.payload == {"manual": True}


def test_enable_requires_browser_acceptance_prompt_and_openai(tmp_path):
    client, _, _ = panopto_client_for(tmp_path)

    response = client.post("/panopto/enable")

    assert response.status_code == 409
    assert "Complete every Panopto setup step" in response.text


def test_enable_after_browser_acceptance_and_prompt_approval(tmp_path):
    client, app, prompt_path = panopto_client_for(tmp_path)
    digest = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
    app.state.panopto_repository.heartbeat(
        "connected",
        datetime(2026, 7, 23, 14, tzinfo=UTC),
    )
    app.state.panopto_repository.mark_acceptance_validated(
        datetime(2026, 7, 23, 14, tzinfo=UTC)
    )
    app.state.panopto_repository.approve_prompt(digest, str(prompt_path))
    app.state.panopto_prompt.approved_sha256 = digest

    response = client.post("/panopto/enable", follow_redirects=False)

    assert response.status_code == 303
    assert app.state.panopto_repository.connection().enabled is True


def test_prompt_change_after_approval_blocks_enable(tmp_path):
    client, app, prompt_path = panopto_client_for(tmp_path)
    digest = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
    app.state.panopto_repository.heartbeat("connected", datetime.now(UTC))
    app.state.panopto_repository.mark_acceptance_validated()
    app.state.panopto_repository.approve_prompt(digest, str(prompt_path))
    app.state.panopto_prompt.approved_sha256 = digest
    prompt_path.write_text("Changed prompt.", encoding="utf-8")

    assert client.post("/panopto/enable").status_code == 409
