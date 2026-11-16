from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from oms_hub.security.secret_store import SecretStore
from oms_hub.study_generation.repository import GenerationRepository

LEGACY_NOTEBOOK_RETIRED = (
    "The legacy NotebookLM connector is retired. Use ExtendLM or Clinical Notebook "
    "for explicit source delivery. Quiz imports and GPT generation remain available."
)

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
        auth: NotebookAuth | None,
    ) -> None:
        self.repository = repository
        self.auth = auth

    def status(self) -> NotebookConnectionStatus:
        return NotebookConnectionStatus("retired", LEGACY_NOTEBOOK_RETIRED)

    def test(self) -> NotebookConnectionStatus:
        return self.status()

    def require_live(self) -> NotebookConnectionStatus:
        return self.status()

    def invalidate(self, message: str) -> NotebookConnectionStatus:
        return self.status()

    def start_interactive(self) -> NotebookConnectionStatus:
        return self.status()


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
