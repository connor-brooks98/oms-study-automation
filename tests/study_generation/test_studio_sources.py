from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oms_hub.db import Database
from oms_hub.llm.domain import DiagnosticSource
from oms_hub.study_generation.domain import NotebookMapping
from oms_hub.study_generation.notebook import StoredNotebookLMGateway
from oms_hub.study_generation.notebook_errors import (
    NotebookAuthenticationError,
    NotebookGatewayError,
)
from oms_hub.study_generation.studio_domain import StudioSourceState
from oms_hub.study_generation.studio_repository import StudioRepository
from oms_hub.study_generation.studio_service import StudioService
from oms_hub.study_generation.studio_worker import StudioWorker


class NoopConverter:
    def convert(self, source: Path, destination: Path) -> None:
        raise AssertionError("conversion was not expected")


class RecordingConnection:
    def __init__(self) -> None:
        self.invalidated: list[str] = []

    def invalidate(self, diagnostic: str) -> object:
        self.invalidated.append(diagnostic)
        return object()


class RecordingGateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.failures: dict[str, Exception] = {}

    def attach_studio_source(
        self,
        subject: str,
        exam_number: int,
        source_type: str,
        title: str,
        *,
        path: Path | None = None,
        text: str | None = None,
        url: str | None = None,
    ) -> tuple[str, str]:
        self.calls.append(
            {
                "subject": subject,
                "exam_number": exam_number,
                "source_type": source_type,
                "title": title,
                "path": path,
                "text": text,
                "url": url,
            }
        )
        failure = self.failures.get(title)
        if failure is not None:
            raise failure
        return "notebook-1", f"remote-{len(self.calls)}"


def _components(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    repository = StudioRepository(database)
    service = StudioService(repository, tmp_path / "studio", 1024 * 1024)
    return database, repository, service


def test_sources_are_durable_and_each_type_is_attached_independently(tmp_path):
    database, repository, service = _components(tmp_path)
    service.add_text("Neuro", 1, "Professor notes", "action potential")
    service.add_url("Neuro", 1, "Bad URL", "https://example.com/bad")
    service.add_url("Neuro", 1, "Good URL", "https://example.com/good")
    gateway = RecordingGateway()
    gateway.failures["Bad URL"] = NotebookGatewayError(
        "NotebookLM could not process this URL",
        source=DiagnosticSource.SOURCE_PROCESSING,
        retryable=True,
    )
    worker = StudioWorker(
        repository,
        gateway,  # type: ignore[arg-type]
        NoopConverter(),
        RecordingConnection(),
    )

    assert worker.run_once() is True
    assert worker.run_once() is True
    assert worker.run_once() is True

    sources = repository.list_sources(" NEURO ", 1)
    assert [source.title for source in sources] == [
        "Professor notes",
        "Bad URL",
        "Good URL",
    ]
    assert sources[0].state is StudioSourceState.ATTACHED
    assert sources[1].state is StudioSourceState.PENDING
    assert sources[1].next_attempt_at is not None
    assert sources[2].state is StudioSourceState.ATTACHED
    assert gateway.calls[0]["text"] == "action potential"
    assert gateway.calls[1]["url"] == "https://example.com/bad"
    assert gateway.calls[2]["url"] == "https://example.com/good"
    database.close()


def test_auth_failure_invalidates_connection_and_fails_source(tmp_path):
    database, repository, service = _components(tmp_path)
    service.add_url("Neuro", 1, "Login source", "https://example.com")
    gateway = RecordingGateway()
    gateway.failures["Login source"] = NotebookAuthenticationError()
    connection = RecordingConnection()
    worker = StudioWorker(
        repository,
        gateway,  # type: ignore[arg-type]
        NoopConverter(),
        connection,
    )

    worker.run_once()

    source = repository.list_sources("neuro", 1)[0]
    assert source.state is StudioSourceState.FAILED
    assert source.diagnostic_source == DiagnosticSource.AUTHENTICATION.value
    assert connection.invalidated == ["NotebookLM login expired; reconnect Google in Settings."]
    database.close()


@dataclass
class Remote:
    id: str
    title: str = ""
    status: str = "ready"


class ClientSources:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def add_file(self, notebook_id, path, *, wait, title):
        self.calls.append(("add_file", (notebook_id, path, wait, title)))
        return Remote("file-source")

    async def add_text(self, notebook_id, title, text, *, wait):
        self.calls.append(("add_text", (notebook_id, title, text, wait)))
        return Remote("text-source")

    async def add_url(self, notebook_id, url, *, wait):
        self.calls.append(("add_url", (notebook_id, url, wait)))
        return Remote("url-source")


class ClientNotebooks:
    async def list(self):
        return [Remote("notebook-1", "Neuro · Exam 1")]


class ClientContext(AbstractAsyncContextManager[Any]):
    def __init__(self, client: object):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class NotebookRepository:
    def notebook_mapping(self, subject_key, exam_number):
        if (subject_key, exam_number) == ("neuro", 1):
            return NotebookMapping(
                1,
                "Neuro",
                "neuro",
                1,
                "notebook-1",
                "Neuro · Exam 1",
            )
        return None


def test_gateway_uses_source_attachment_methods_only(tmp_path):
    client = type(
        "Client",
        (),
        {"notebooks": ClientNotebooks(), "sources": ClientSources()},
    )()
    gateway = StoredNotebookLMGateway(
        tmp_path / "storage.json",
        NotebookRepository(),  # type: ignore[arg-type]
        client_factory=lambda: ClientContext(client),
    )
    file_path = tmp_path / "source.pdf"
    file_path.write_bytes(b"file")

    assert gateway.attach_studio_source("Neuro", 1, "file", "File", path=file_path) == (
        "notebook-1",
        "file-source",
    )
    assert gateway.attach_studio_source("Neuro", 1, "text", "Text", text="notes") == (
        "notebook-1",
        "text-source",
    )
    assert gateway.attach_studio_source("Neuro", 1, "url", "URL", url="https://example.com") == (
        "notebook-1",
        "url-source",
    )
    assert [name for name, _args in client.sources.calls] == [
        "add_file",
        "add_text",
        "add_url",
    ]
    assert not hasattr(client, "artifacts")
