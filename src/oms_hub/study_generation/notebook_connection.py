import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from oms_hub.security.secret_store import SecretStore
from oms_hub.study_generation.repository import GenerationRepository

OAUTH_REFRESH_TOKEN_KEY = "google-oauth-refresh-token"
CONNECTED_EMAIL_KEY = "google-connected-email"


class NotebookAuth(Protocol):
    def login(self) -> None: ...

    def check(self) -> object: ...


@dataclass(frozen=True, slots=True)
class NotebookConnectionStatus:
    state: str
    message: str | None = None


class NotebookConnectionService:
    def __init__(
        self,
        repository: GenerationRepository,
        auth: NotebookAuth,
    ) -> None:
        self.repository = repository
        self.auth = auth
        self._lock = threading.Lock()

    def status(self) -> NotebookConnectionStatus:
        stored = self.repository.google_status()
        if stored is None:
            return NotebookConnectionStatus("disconnected")
        state = stored.notebook_state or stored.state or "disconnected"
        return NotebookConnectionStatus(state, stored.diagnostic)

    def test(self) -> NotebookConnectionStatus:
        checked = self.auth.check()
        connected = bool(getattr(checked, "connected", False))
        message = None if connected else (
            getattr(checked, "message", None)
            or "Gemini Notebook login is required."
        )
        status = NotebookConnectionStatus(
            "connected" if connected else "failed",
            message,
        )
        self._save(status)
        return status

    def require_live(self) -> NotebookConnectionStatus:
        status = self.test()
        if status.state != "connected":
            raise RuntimeError(
                status.message or "Connect Gemini Notebook in Settings"
            )
        return status

    def invalidate(self, message: str) -> NotebookConnectionStatus:
        status = NotebookConnectionStatus("failed", message)
        self._save(status)
        return status

    def start_interactive(self) -> NotebookConnectionStatus:
        if not self._lock.acquire(blocking=False):
            return NotebookConnectionStatus(
                "connecting",
                "A Gemini Notebook sign-in window is already open.",
            )
        try:
            self._save(
                NotebookConnectionStatus(
                    "connecting",
                    "Complete Google sign-in in the browser window.",
                )
            )
            self.auth.login()
            return self.test()
        except Exception as error:  # noqa: BLE001 - external error is sanitized
            checked = self.test()
            if checked.state == "connected":
                return checked
            return self.invalidate(_safe_login_error(error))
        finally:
            self._lock.release()

    def _save(self, status: NotebookConnectionStatus) -> None:
        self.repository.save_google_status(
            state=status.state,
            account_email=None,
            notebook_state=status.state,
            gemini_state="unused",
            docs_state="retired",
            diagnostic=status.message,
            tested_at=datetime.now(UTC).isoformat(),
        )


def retire_google_docs_credentials(
    data_dir: Path,
    secrets: SecretStore,
) -> None:
    oauth_client = data_dir / "google" / "oauth-client.json"
    try:
        oauth_client.unlink()
    except FileNotFoundError:
        pass
    secrets.delete(OAUTH_REFRESH_TOKEN_KEY)
    secrets.delete(CONNECTED_EMAIL_KEY)


def _safe_login_error(error: Exception) -> str:
    text = str(error).casefold()
    if "timed out" in text:
        return "Gemini Notebook sign-in timed out. Try Connect again."
    if "unavailable" in text or "reinstall" in text:
        return "Gemini Notebook sign-in is unavailable. Reinstall Study Hub."
    return "Gemini Notebook sign-in did not complete."
