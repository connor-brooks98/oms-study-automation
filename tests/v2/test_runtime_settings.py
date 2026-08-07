from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.models import RuntimeSettingAuditModel, RuntimeSettingModel, SchemaVersionModel


def _settings(tmp_path, **overrides):
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        allow_local_access=True,
        **overrides,
    )


def _csrf_client(app):
    client = TestClient(app)
    client.get("/settings")
    return client, {app.state.csrf.header_name: client.cookies.get(app.state.csrf.cookie_name)}


def test_runtime_settings_fresh_database_and_repeat_migration_are_additive(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    database.migrate()

    tables = set(inspect(database.engine).get_table_names())
    assert {"runtime_settings", "runtime_setting_audit"} <= tables
    with database.session() as session:
        assert session.get(SchemaVersionModel, 1).version == 18
    database.close()


def test_stage_port_is_audited_and_applies_only_after_next_start(tmp_path):
    settings = _settings(tmp_path)
    first_app = create_app(settings)
    client, csrf = _csrf_client(first_app)

    staged = client.put(
        "/settings/runtime/anki-connect-port",
        json={"port": 8766},
        headers=csrf,
    )

    assert staged.status_code == 200
    assert staged.headers["cache-control"] == "no-store"
    assert staged.json() == {
        "anki_connect_port": 8766,
        "source": "staged_override",
        "revision": 1,
        "restart_required": True,
        "message": "AnkiConnect port staged. Restart Study Hub to apply it.",
    }
    # Running clients do not change transport configuration in-process.
    assert first_app.state.settings.anki_connect_url == "http://127.0.0.1:8765"

    restarted = create_app(settings)
    assert restarted.state.settings.anki_connect_url == "http://127.0.0.1:8766"
    assert restarted.state.database is not first_app.state.database
    active = restarted.state.runtime_settings.status()
    assert active.source == "active_override"
    assert active.restart_required is False
    active_page = TestClient(restarted).get("/settings")
    assert "The audited AnkiConnect override is active; no restart is required." in active_page.text
    with restarted.state.database.session() as session:
        row = session.get(RuntimeSettingModel, "anki_connect_port")
        audit = session.scalars(select(RuntimeSettingAuditModel)).one()
        assert row is not None and row.value == "8766" and row.revision == 1
        assert audit.action == "staged"
        assert audit.actor == "local"
        assert audit.previous_value is None


def test_port_validation_csrf_and_dashboard_collision_are_rejected(tmp_path):
    app = create_app(_settings(tmp_path))
    client = TestClient(app)

    denied = client.put("/settings/runtime/anki-connect-port", json={"port": 8766})
    assert denied.status_code == 403

    client.get("/settings")
    csrf = {app.state.csrf.header_name: client.cookies.get(app.state.csrf.cookie_name)}
    too_low = client.put(
        "/settings/runtime/anki-connect-port",
        json={"port": 80},
        headers=csrf,
    )
    collision = client.put(
        "/settings/runtime/anki-connect-port",
        json={"port": 8787},
        headers=csrf,
    )

    assert too_low.status_code == 422
    assert "1024 and 65535" in too_low.json()["detail"]
    assert collision.status_code == 422
    assert "cannot use the same port" in collision.json()["detail"]


def test_clear_before_restart_returns_to_the_active_environment_value(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings)
    client, csrf = _csrf_client(app)
    client.put(
        "/settings/runtime/anki-connect-port",
        json={"port": 8766},
        headers=csrf,
    )

    cleared = client.delete("/settings/runtime/anki-connect-port", headers=csrf)

    assert cleared.status_code == 200
    assert cleared.json()["anki_connect_port"] == 8765
    assert cleared.json()["source"] == "environment"
    assert cleared.json()["revision"] == 2
    assert cleared.json()["restart_required"] is False
    assert cleared.json()["message"] == "Using the deployment value; no restart is required."
    with app.state.database.session() as session:
        assert session.get(RuntimeSettingModel, "anki_connect_port") is None
        audits = session.scalars(
            select(RuntimeSettingAuditModel).order_by(RuntimeSettingAuditModel.revision)
        ).all()
    assert [(item.action, item.revision, item.actor) for item in audits] == [
        ("staged", 1, "local"),
        ("cleared", 2, "local"),
    ]


def test_changing_or_clearing_an_active_override_is_staged_for_restart(tmp_path):
    settings = _settings(tmp_path)
    first_app = create_app(settings)
    first_client, first_csrf = _csrf_client(first_app)
    first_client.put(
        "/settings/runtime/anki-connect-port",
        json={"port": 8766},
        headers=first_csrf,
    )

    active_app = create_app(settings)
    active_client, active_csrf = _csrf_client(active_app)
    assert active_app.state.runtime_settings.status().source == "active_override"

    unchanged = active_client.put(
        "/settings/runtime/anki-connect-port",
        json={"port": 8766},
        headers=active_csrf,
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["source"] == "active_override"
    assert unchanged.json()["restart_required"] is False
    assert unchanged.json()["message"] == "AnkiConnect port is already active."

    changed = active_client.put(
        "/settings/runtime/anki-connect-port",
        json={"port": 8767},
        headers=active_csrf,
    )
    assert changed.status_code == 200
    assert changed.json()["source"] == "staged_override"
    assert changed.json()["restart_required"] is True

    cleared = active_client.delete("/settings/runtime/anki-connect-port", headers=active_csrf)
    assert cleared.status_code == 200
    assert cleared.json()["source"] == "environment"
    assert cleared.json()["restart_required"] is True
    assert active_app.state.settings.anki_connect_url == "http://127.0.0.1:8766"


def test_runtime_page_exposes_status_not_remote_access_or_secret_values(tmp_path):
    app = create_app(
        _settings(
            tmp_path,
            public_hostname="study.example.com",
            cloudflare_access_issuer="https://team.cloudflareaccess.com",
            cloudflare_access_audience="audience-sentinel",
            cloudflare_access_allowed_email="student@example.com",
            anki_agent_hostname="agent.example.com",
            anki_agent_token_key="agent-token-sentinel",
        )
    )
    client = TestClient(app)

    page = client.get("/settings")

    assert page.status_code == 200
    assert "Configured (managed outside Study Hub)" in page.text
    for secret_or_boundary in (
        "study.example.com",
        "team.cloudflareaccess.com",
        "audience-sentinel",
        "student@example.com",
        "agent.example.com",
        "agent-token-sentinel",
        str(tmp_path),
    ):
        assert secret_or_boundary not in page.text
