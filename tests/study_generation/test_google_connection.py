import json
import stat
from dataclasses import dataclass

from oms_hub.db import Database
from oms_hub.study_generation.browser_profile import launch_google_profile
from oms_hub.study_generation.google_connection import (
    GoogleConnectionService,
    GoogleOAuthClientStore,
    GoogleSurface,
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
class Probe:
    emails: dict[GoogleSurface, str]

    def account_email(self, surface):
        return self.emails[surface]


class FailedProbe:
    def account_email(self, surface):
        errors = {
            GoogleSurface.NOTEBOOK: "notebook sign-in is required",
            GoogleSurface.GEMINI: "browser executable doesn't exist",
            GoogleSurface.DOCS: "invalid_grant",
        }
        raise RuntimeError(errors[surface])


class BrowserLauncher:
    def __init__(self):
        self.options = None
        self.context = object()

    def launch_persistent_context(self, **options):
        self.options = options
        return self.context


def test_connected_status_requires_same_account_on_all_surfaces(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    service = GoogleConnectionService(
        GenerationRepository(database),
        MemorySecrets(),
        tmp_path,
        Probe(
            {
                GoogleSurface.NOTEBOOK: "student@example.com",
                GoogleSurface.GEMINI: "student@example.com",
                GoogleSurface.DOCS: "other@example.com",
            }
        ),
    )

    status = service.test()

    assert status.state == "failed"
    assert status.message == "Google surfaces are connected to different accounts"


def test_matching_accounts_are_recorded_without_credentials(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    secrets = MemorySecrets()
    service = GoogleConnectionService(
        GenerationRepository(database),
        secrets,
        tmp_path,
        Probe({surface: "student@example.com" for surface in GoogleSurface}),
    )

    status = service.test()

    assert status.state == "connected"
    assert status.account_email == "student@example.com"
    assert secrets.values["google-connected-email"] == "student@example.com"
    assert "token" not in repr(status).casefold()


def test_oauth_client_is_validated_and_saved_owner_only(tmp_path):
    store = GoogleOAuthClientStore(tmp_path)
    payload = json.dumps(
        {
            "installed": {
                "client_id": "client.apps.googleusercontent.com",
                "client_secret": "secret-value",
                "redirect_uris": ["http://localhost"],
            }
        }
    ).encode()

    path = store.save(payload)

    assert path == tmp_path / "google" / "oauth-client.json"
    if hasattr(stat, "S_IMODE"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "secret-value" not in repr(store.status())


def test_oauth_client_rejects_non_desktop_payload(tmp_path):
    store = GoogleOAuthClientStore(tmp_path)

    try:
        store.save(b'{"web": {"client_id": "wrong-kind"}}')
    except ValueError as error:
        assert "Desktop app" in str(error)
    else:
        raise AssertionError("expected invalid OAuth client to be rejected")


def test_connection_test_reports_safe_actionable_surface_failures(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    service = GoogleConnectionService(
        GenerationRepository(database),
        MemorySecrets(),
        tmp_path,
        FailedProbe(),
    )

    status = service.test()

    assert status.state == "failed"
    assert status.message == (
        "NotebookLM needs sign-in. Gemini needs Google Chrome installed. "
        "Google Docs authorization expired; connect Google again."
    )
    assert [surface.message for surface in status.surfaces] == [
        "NotebookLM needs sign-in.",
        "Gemini needs Google Chrome installed.",
        "Google Docs authorization expired; connect Google again.",
    ]
    assert "invalid_grant" not in repr(status)


def test_google_profile_uses_system_chrome_without_automation_marker(tmp_path):
    browser = BrowserLauncher()

    context = launch_google_profile(
        browser,
        tmp_path / "browser-profile",
        headless=False,
    )

    assert context is browser.context
    assert browser.options == {
        "user_data_dir": str(tmp_path / "browser-profile"),
        "channel": "chrome",
        "headless": False,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--password-store=basic",
        ],
        "ignore_default_args": ["--enable-automation"],
    }
