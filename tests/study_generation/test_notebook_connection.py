from dataclasses import dataclass

import pytest

from oms_hub.db import Database
from oms_hub.study_generation.notebook_auth import NotebookAuthCheck
from oms_hub.study_generation.notebook_connection import (
    NotebookConnectionService,
    retire_google_docs_credentials,
)
from oms_hub.study_generation.repository import GenerationRepository


class MemorySecrets:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


@dataclass
class Check:
    connected: bool
    message: str | None = None
    requires_login: bool = False


class Auth:
    def __init__(self, connected=True):
        self.connected = connected
        self.login_calls = 0

    def check(self):
        return Check(
            self.connected,
            None if self.connected else "Gemini Notebook login is required.",
            requires_login=not self.connected,
        )

    def login(self):
        self.login_calls += 1


def service(tmp_path, auth):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    return NotebookConnectionService(GenerationRepository(database), auth)


def test_live_test_records_only_notebook_connection(tmp_path):
    connection = service(tmp_path, Auth(connected=True))

    status = connection.test()

    assert status.state == "connected"
    assert status.message is None
    stored = connection.repository.google_status()
    assert stored is not None
    assert stored.notebook_state == "connected"
    assert stored.docs_state == "retired"


def test_interactive_login_uses_notebook_cli_and_retests(tmp_path):
    auth = Auth(connected=True)
    connection = service(tmp_path, auth)

    status = connection.start_interactive()

    assert auth.login_calls == 1
    assert status.state == "connected"


def test_require_live_rejects_expired_notebook_login(tmp_path):
    connection = service(tmp_path, Auth(connected=False))

    try:
        connection.require_live()
    except RuntimeError as error:
        assert "login is required" in str(error)
    else:
        raise AssertionError("expected expired Notebook login to be rejected")


@pytest.mark.parametrize("interactive", [False, True])
def test_inconclusive_check_blocks_generation_without_expiring_session(tmp_path, interactive):
    auth = Auth()
    connection = service(tmp_path, auth)
    connection.test()

    def unavailable():
        return NotebookAuthCheck(False, "Connection check timed out. Try again.")

    auth.check = unavailable
    if interactive:
        def failed_login():
            raise RuntimeError("SID=secret")
        auth.login = failed_login
        assert connection.start_interactive().state == "unverified"

    with pytest.raises(RuntimeError, match="timed out"):
        connection.require_live()

    assert connection.status().state == "unverified"
    assert "reconnect" not in connection.status().message.casefold()

    auth.check = lambda: NotebookAuthCheck(True)
    assert connection.require_live().state == "connected"
    assert connection.status().message is None


def test_unexpected_check_error_is_sanitized_and_not_a_login_failure(tmp_path):
    auth = Auth()
    connection = service(tmp_path, auth)

    def broken_check():
        raise RuntimeError("SID=secret")

    auth.check = broken_check
    status = connection.test()

    assert status.state == "unverified"
    assert "SID=" not in status.message
    assert "reconnect" not in status.message.casefold()


def test_retirement_removes_only_docs_oauth_material(tmp_path):
    google = tmp_path / "google"
    google.mkdir()
    oauth = google / "oauth-client.json"
    storage = google / "notebooklm-storage.json"
    unrelated = google / "keep-me.json"
    oauth.write_text("oauth", encoding="utf-8")
    storage.write_text("notebook", encoding="utf-8")
    unrelated.write_text("other", encoding="utf-8")
    secrets = MemorySecrets()
    secrets.values.update(
        {
            "google-oauth-refresh-token": "refresh",
            "google-connected-email": "student@example.com",
            "unrelated-key": "keep",
        }
    )

    retire_google_docs_credentials(tmp_path, secrets)

    assert not oauth.exists()
    assert storage.read_text(encoding="utf-8") == "notebook"
    assert unrelated.read_text(encoding="utf-8") == "other"
    assert secrets.values == {"unrelated-key": "keep"}
