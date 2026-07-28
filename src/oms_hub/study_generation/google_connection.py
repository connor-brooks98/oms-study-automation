import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from oms_hub.security.secret_store import SecretStore
from oms_hub.study_generation.notebook_auth import NotebookCLIAuth
from oms_hub.study_generation.repository import GenerationRepository

CONNECTED_EMAIL_KEY = "google-connected-email"
OAUTH_REFRESH_TOKEN_KEY = "google-oauth-refresh-token"
GOOGLE_SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents",
)


@dataclass(frozen=True, slots=True)
class OAuthClientStatus:
    configured: bool


class GoogleOAuthClientStore:
    def __init__(self, data_dir: Path):
        self.root = (data_dir / "google").resolve()
        self.path = self.root / "oauth-client.json"

    def save(self, payload: bytes) -> Path:
        try:
            decoded = json.loads(payload)
            installed = decoded["installed"]
            required = (
                installed["client_id"],
                installed["client_secret"],
                installed["redirect_uris"],
            )
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError(
                "Choose the OAuth client JSON for a Google Desktop app"
            ) from error
        if not all(required) or not isinstance(required[2], list):
            raise ValueError("Choose the OAuth client JSON for a Google Desktop app")
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.root.chmod(0o700)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(payload)
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(self.path)
        if os.name != "nt":
            self.path.chmod(0o600)
        return self.path

    def status(self) -> OAuthClientStatus:
        return OAuthClientStatus(configured=self.path.is_file())


class GoogleSurface(StrEnum):
    NOTEBOOK = "notebook"
    DOCS = "docs"


@dataclass(frozen=True, slots=True)
class GoogleSurfaceStatus:
    name: str
    state: str
    message: str | None = None


@dataclass(frozen=True, slots=True)
class GoogleConnectionStatus:
    state: str
    account_email: str | None
    surfaces: tuple[GoogleSurfaceStatus, ...]
    message: str | None


class GoogleAccountProbe(Protocol):
    def account_email(self, surface: GoogleSurface) -> str: ...


class InteractiveGoogleProbe(GoogleAccountProbe, Protocol):
    def start_interactive(self) -> None: ...


class GoogleConnectionService:
    def __init__(
        self,
        repository: GenerationRepository,
        secrets: SecretStore,
        data_dir: Path,
        probe: GoogleAccountProbe,
    ) -> None:
        self.repository = repository
        self.secrets = secrets
        self.data_dir = data_dir
        self.probe = probe
        self.oauth_clients = GoogleOAuthClientStore(data_dir)
        self._lock = threading.Lock()

    def status(self) -> GoogleConnectionStatus:
        stored = self.repository.google_status()
        if stored is None:
            return GoogleConnectionStatus("disconnected", None, (), None)
        surfaces = tuple(
            GoogleSurfaceStatus(name.value, state or "disconnected")
            for name, state in (
                (GoogleSurface.NOTEBOOK, stored.notebook_state),
                (GoogleSurface.DOCS, stored.docs_state),
            )
        )
        return GoogleConnectionStatus(
            stored.state,
            stored.account_email,
            surfaces,
            stored.diagnostic,
        )

    def test(self) -> GoogleConnectionStatus:
        emails: dict[GoogleSurface, str] = {}
        surface_statuses: list[GoogleSurfaceStatus] = []
        for surface in GoogleSurface:
            try:
                emails[surface] = self.probe.account_email(surface).casefold()
                surface_statuses.append(
                    GoogleSurfaceStatus(surface.value, "connected")
                )
            except Exception as error:  # noqa: BLE001 - external boundary is sanitized
                surface_statuses.append(
                    GoogleSurfaceStatus(
                        surface.value,
                        "failed",
                        _safe_surface_error(surface, error),
                    )
                )
        values = set(emails.values())
        if len(emails) != len(GoogleSurface):
            state = "failed"
            email = None
            message = " ".join(
                item.message
                for item in surface_statuses
                if item.message is not None
            )
        elif len(values) != 1:
            state = "failed"
            email = None
            message = "Google surfaces are connected to different accounts"
        else:
            state = "connected"
            email = next(iter(values))
            message = None
            self.secrets.set(CONNECTED_EMAIL_KEY, email)
        now = datetime.now(UTC).isoformat()
        by_name = {item.name: item.state for item in surface_statuses}
        self.repository.save_google_status(
            state=state,
            account_email=email,
            notebook_state=by_name[GoogleSurface.NOTEBOOK.value],
            gemini_state="unused",
            docs_state=by_name[GoogleSurface.DOCS.value],
            diagnostic=message,
            tested_at=now,
        )
        return GoogleConnectionStatus(
            state,
            email,
            tuple(surface_statuses),
            message,
        )

    def start_interactive(self) -> GoogleConnectionStatus:
        if not self._lock.acquire(blocking=False):
            return _connecting_status(
                "A Google sign-in window is already open on this Study Hub device."
            )
        try:
            self.repository.save_google_status(
                state="connecting",
                account_email=None,
                notebook_state="connecting",
                gemini_state="unused",
                docs_state="connecting",
                diagnostic="Complete Google sign-in in the browser window.",
                tested_at=datetime.now(UTC).isoformat(),
            )
            starter = getattr(self.probe, "start_interactive", None)
            if starter is None:
                raise RuntimeError("interactive Google connection is unavailable")
            starter()
            return self.test()
        except Exception as error:  # noqa: BLE001 - persist only a safe diagnostic
            message = _safe_interactive_error(error)
            self.repository.save_google_status(
                state="failed",
                account_email=None,
                notebook_state="failed",
                gemini_state="unused",
                docs_state="failed",
                diagnostic=message,
                tested_at=datetime.now(UTC).isoformat(),
            )
            return self.status()
        finally:
            self._lock.release()

    def save_oauth_client(self, payload: bytes) -> OAuthClientStatus:
        self.oauth_clients.save(payload)
        return self.oauth_clients.status()

    def oauth_credentials(self) -> object:
        from google.oauth2.credentials import Credentials

        refresh_token = self.secrets.get(OAUTH_REFRESH_TOKEN_KEY)
        if not refresh_token or not self.oauth_clients.status().configured:
            raise RuntimeError("Google Docs access is not connected")
        installed = json.loads(
            self.oauth_clients.path.read_text(encoding="utf-8")
        )["installed"]
        return Credentials(  # type: ignore[no-untyped-call]
            token=None,
            refresh_token=refresh_token,
            token_uri=installed.get(
                "token_uri",
                "https://oauth2.googleapis.com/token",
            ),
            client_id=installed["client_id"],
            client_secret=installed["client_secret"],
            scopes=GOOGLE_SCOPES,
        )


class PlaywrightGoogleProbe:
    """Google Docs OAuth and NotebookLM CLI authentication probe."""

    def __init__(
        self,
        data_dir: Path,
        secrets: SecretStore,
        notebook_auth: NotebookCLIAuth | None = None,
    ):
        self.root = (data_dir / "google").resolve()
        self.storage_state = self.root / "notebooklm-storage.json"
        self.secrets = secrets
        self.oauth_clients = GoogleOAuthClientStore(data_dir)
        self.notebook_auth = notebook_auth or NotebookCLIAuth(self.storage_state)

    def start_interactive(self) -> None:
        if not self.oauth_clients.status().configured:
            raise RuntimeError("Google OAuth client is not configured")
        self._connect_docs()
        self.notebook_auth.login()
        notebook_check = self.notebook_auth.check()
        if not notebook_check.connected:
            raise RuntimeError(
                notebook_check.message or "NotebookLM login is required."
            )

    def account_email(self, surface: GoogleSurface) -> str:
        email = self.secrets.get(CONNECTED_EMAIL_KEY)
        if not email:
            raise RuntimeError(f"{surface.value} account is not connected")
        if surface is GoogleSurface.DOCS:
            refresh_token = self.secrets.get(OAUTH_REFRESH_TOKEN_KEY)
            if not refresh_token or not self.oauth_clients.status().configured:
                raise RuntimeError("Google Docs account is not connected")
            return _oauth_email(
                _stored_oauth_credentials(
                    self.oauth_clients,
                    refresh_token,
                )
            )
        notebook_check = self.notebook_auth.check()
        if not notebook_check.connected:
            raise RuntimeError(
                notebook_check.message or "NotebookLM sign-in is required"
            )
        return email

    def _connect_docs(self) -> None:
        refresh_token = self.secrets.get(OAUTH_REFRESH_TOKEN_KEY)
        if refresh_token:
            try:
                credentials = _stored_oauth_credentials(
                    self.oauth_clients,
                    refresh_token,
                )
                email = _oauth_email(credentials)
            except Exception:  # noqa: BLE001 - invalid token falls through to OAuth
                refresh_token = None
            else:
                self.secrets.set(CONNECTED_EMAIL_KEY, email)
                return

        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.oauth_clients.path),
            scopes=GOOGLE_SCOPES,
        )
        credentials = flow.run_local_server(
            host="127.0.0.1",
            port=0,
            open_browser=True,
            authorization_prompt_message="Complete Google access in your browser.",
            timeout_seconds=300,
            access_type="offline",
            prompt="consent",
        )
        refresh_token = credentials.refresh_token or refresh_token
        if not refresh_token:
            raise RuntimeError("Google did not provide a reusable login")
        email = _oauth_email(credentials)
        self.secrets.set(OAUTH_REFRESH_TOKEN_KEY, refresh_token)
        self.secrets.set(CONNECTED_EMAIL_KEY, email)


def _stored_oauth_credentials(
    oauth_clients: GoogleOAuthClientStore,
    refresh_token: str,
) -> object:
    installed = json.loads(
        oauth_clients.path.read_text(encoding="utf-8")
    )["installed"]
    from google.oauth2.credentials import Credentials

    return Credentials(  # type: ignore[no-untyped-call]
        token=None,
        refresh_token=refresh_token,
        token_uri=installed.get(
            "token_uri",
            "https://oauth2.googleapis.com/token",
        ),
        client_id=installed["client_id"],
        client_secret=installed["client_secret"],
        scopes=GOOGLE_SCOPES,
    )


def _oauth_email(credentials: object) -> str:
    from googleapiclient.discovery import build  # type: ignore[import-untyped]

    service = build("oauth2", "v2", credentials=credentials, cache_discovery=False)
    result = service.userinfo().get().execute()
    email = result.get("email")
    if not isinstance(email, str) or not email:
        raise RuntimeError("Google account email was unavailable")
    return email.casefold()


def _connecting_status(message: str) -> GoogleConnectionStatus:
    return GoogleConnectionStatus(
        "connecting",
        None,
        tuple(
            GoogleSurfaceStatus(surface.value, "connecting")
            for surface in GoogleSurface
        ),
        message,
    )


def _safe_surface_error(
    surface: GoogleSurface,
    error: Exception,
) -> str:
    label = {
        GoogleSurface.NOTEBOOK: "NotebookLM",
        GoogleSurface.DOCS: "Google Docs",
    }[surface]
    message = str(error).casefold()
    if "executable doesn't exist" in message or "browser executable" in message:
        return f"{label} needs Google Chrome installed."
    if "invalid_grant" in message or "expired or revoked" in message:
        return f"{label} authorization expired; connect Google again."
    if (
        "sign-in" in message
        or "sign in" in message
        or "not connected" in message
    ):
        return f"{label} needs sign-in."
    if "access_denied" in message or "access denied" in message:
        return f"{label} access was denied by Google."
    if "timed out" in message or "timeout" in message:
        return f"{label} timed out while checking the account."
    return f"{label} could not be reached."


def _safe_interactive_error(error: Exception) -> str:
    message = str(error).casefold()
    if "not configured" in message:
        return "Save the Google Desktop app OAuth client JSON, then connect again."
    if "access_denied" in message or "access denied" in message:
        return (
            "Google denied this account. Add the email as an OAuth test user, "
            "then connect again."
        )
    if "invalid_grant" in message or "expired or revoked" in message:
        return "Google authorization expired; connect Google again."
    if "reusable login" in message:
        return "Google did not return offline access; connect again and approve access."
    if "executable doesn't exist" in message or "browser executable" in message:
        return (
            "Google Chrome is not installed on the NUC. Install Chrome, "
            "then connect Google again."
        )
    if "timed out" in message or "timeout" in message:
        return (
            "Google sign-in timed out. Close any old sign-in window and connect again."
        )
    return (
        "Google sign-in stopped before all services connected. "
        "Connect again and complete every browser sign-in window."
    )
