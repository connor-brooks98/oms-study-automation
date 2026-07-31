from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from oms_hub.db import Database
from oms_hub.study_generation.domain import NotebookMapping
from oms_hub.study_generation.notebook import StoredNotebookLMGateway
from oms_hub.study_generation.studio_domain import (
    StudioRunState,
    StudioSourceType,
)
from oms_hub.study_generation.studio_repository import StudioRepository
from oms_hub.study_generation.studio_service import StudioService
from oms_hub.study_generation.studio_worker import StudioWorker


class NoopConverter:
    def convert(self, source: Path, destination: Path) -> None:
        raise AssertionError("conversion was not expected")


class Connection:
    def invalidate(self, diagnostic: str) -> object:
        return object()


class ChatGateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def ask_studio(self, subject, exam_number, prompt, source_ids):
        self.calls.append(
            {
                "subject": subject,
                "exam_number": exam_number,
                "prompt": prompt,
                "source_ids": list(source_ids),
            }
        )
        return "notebook-1", '{"title":"Generated","questions":[]}'


def _components(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    repository = StudioRepository(database)
    service = StudioService(repository, tmp_path / "studio", 1024 * 1024)
    return database, repository, service


def test_queue_run_snapshots_only_attached_sources_and_appends_contract(tmp_path):
    database, repository, service = _components(tmp_path)
    attached = repository.create_source(
        "Neuro",
        1,
        StudioSourceType.URL,
        "Professor page",
        source_url="https://example.com",
    )
    repository.complete(attached.id, "notebook-1", "remote-professor")
    pending = repository.create_source(
        "Neuro",
        1,
        StudioSourceType.URL,
        "Pending",
        source_url="https://example.com/pending",
    )

    run = service.queue_run(
        "Neuro",
        1,
        "Create a comprehensive quiz.",
        [attached.id],
        "Exam 1 comprehensive",
        "Neuro",
        1,
    )

    assert [source.source_id for source in run.sources] == [attached.id]
    assert [source.remote_source_id for source in run.sources] == ["remote-professor"]
    assert "Return exactly one JSON object" in run.prompt
    with pytest.raises(ValueError, match="must be attached"):
        service.queue_run(
            "Neuro",
            1,
            "Prompt",
            [pending.id],
            "Invalid",
            "Neuro",
            1,
        )
    database.close()


def test_prompt_only_run_passes_an_explicit_empty_source_list_to_chat(tmp_path):
    database, repository, service = _components(tmp_path)
    run = service.queue_run(
        "Neuro",
        1,
        "Create a quiz from general knowledge.",
        [],
        "Prompt only",
        "Neuro",
        1,
    )
    gateway = ChatGateway()
    worker = StudioWorker(
        repository,
        gateway,  # type: ignore[arg-type]
        NoopConverter(),
        Connection(),
    )

    assert worker.run_once() is True

    completed = repository.get_run(run.id)
    assert completed.state is StudioRunState.COMPLETE
    assert completed.raw_response == '{"title":"Generated","questions":[]}'
    assert gateway.calls[0]["source_ids"] == []
    attempts = repository.list_run_attempts(run.id)
    assert attempts[0].diagnostic_source == "notebook_chat"
    assert attempts[0].raw_response == completed.raw_response
    database.close()


@dataclass
class Remote:
    id: str
    title: str = ""
    status: str = "ready"


class ClientNotebooks:
    async def list(self):
        return [Remote("notebook-1", "Neuro · Exam 1")]


class ClientSources:
    def __init__(self) -> None:
        self.list_calls = 0

    async def list(self, notebook_id):
        self.list_calls += 1
        return [Remote("source-1")]


class ClientChat:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def ask(self, notebook_id, prompt, *, source_ids):
        self.calls.append(
            {
                "notebook_id": notebook_id,
                "prompt": prompt,
                "source_ids": list(source_ids),
            }
        )
        return type("Answer", (), {"answer": "chat response"})()


class ClientContext(AbstractAsyncContextManager[Any]):
    def __init__(self, client: object):
        self.client = client
        self.entries = 0

    async def __aenter__(self):
        self.entries += 1
        return self.client

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class NotebookRepository:
    def notebook_mapping(self, subject_key, exam_number):
        return NotebookMapping(
            1,
            "Neuro",
            "neuro",
            1,
            "notebook-1",
            "Neuro · Exam 1",
        )


def test_gateway_asks_chat_in_one_context_with_explicit_selection(tmp_path):
    client = type(
        "Client",
        (),
        {
            "notebooks": ClientNotebooks(),
            "sources": ClientSources(),
            "chat": ClientChat(),
        },
    )()
    context = ClientContext(client)
    gateway = StoredNotebookLMGateway(
        tmp_path / "storage.json",
        NotebookRepository(),  # type: ignore[arg-type]
        client_factory=lambda: context,
    )

    notebook_id, answer = gateway.ask_studio(
        "Neuro",
        1,
        "Prompt",
        [],
    )

    assert (notebook_id, answer) == ("notebook-1", "chat response")
    assert context.entries == 1
    assert client.sources.list_calls == 1
    assert client.chat.calls[0]["source_ids"] == []
    assert not hasattr(client, "artifacts")
