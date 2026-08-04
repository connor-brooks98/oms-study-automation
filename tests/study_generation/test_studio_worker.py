import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from oms_hub.db import Database
from oms_hub.study_generation.studio_domain import StudioRunState, StudioSourceType
from oms_hub.study_generation.studio_repository import StudioRepository
from oms_hub.study_generation.studio_worker import StudioWorker

_OPEN_DATABASES: list[Database] = []


@pytest.fixture(autouse=True)
def _close_databases() -> None:
    yield
    while _OPEN_DATABASES:
        _OPEN_DATABASES.pop().close()


def _database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    _OPEN_DATABASES.append(database)
    database.migrate()
    return database


class _FakeConnection:
    def __init__(self) -> None:
        self.invalidated: list[str] = []

    def invalidate(self, diagnostic: str) -> None:
        self.invalidated.append(diagnostic)


class _RaisingGateway:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def ask_studio(self, subject, exam_number, prompt, remote_source_ids):
        raise self.error

    def attach_studio_source(self, subject, exam_number, source_type, title, **kwargs):
        raise self.error


def _sqlite_busy_error() -> OperationalError:
    orig = sqlite3.OperationalError("database is locked")
    orig.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return OperationalError("stmt", {}, orig)


def _queued_run(repository: StudioRepository):
    source = repository.create_source(
        "Neuro",
        1,
        StudioSourceType.TEXT,
        "Lecture notes",
    )
    repository.complete(source.id, "notebook-1", "remote-source-1")
    return repository.queue_run(
        "Neuro",
        1,
        "Draft a quiz.",
        [source.id],
        "Practice Quiz",
        "Neuro",
        1,
    )


def test_sqlite_busy_chat_failure_retries_studio_run(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = StudioRepository(database)
    _queued_run(repository)

    worker = StudioWorker(
        repository,
        _RaisingGateway(_sqlite_busy_error()),
        object(),
        _FakeConnection(),
    )

    assert worker.run_once() is True

    run = repository.list_runs()[0]
    assert run.state is StudioRunState.RETRYING
    assert run.next_attempt_at is not None


def test_non_busy_chat_failure_fails_studio_run(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = StudioRepository(database)
    _queued_run(repository)

    worker = StudioWorker(
        repository,
        _RaisingGateway(RuntimeError("boom")),
        object(),
        _FakeConnection(),
    )

    assert worker.run_once() is True

    run = repository.list_runs()[0]
    assert run.state is StudioRunState.FAILED


def test_sqlite_busy_source_attach_failure_is_retried(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = StudioRepository(database)
    payload = tmp_path / "notes.txt"
    payload.write_text("Lecture notes content.", encoding="utf-8")
    repository.create_source(
        "Neuro",
        1,
        StudioSourceType.TEXT,
        "Lecture notes",
        payload_path=payload,
    )

    worker = StudioWorker(
        repository,
        _RaisingGateway(_sqlite_busy_error()),
        object(),
        _FakeConnection(),
    )

    assert worker.run_once() is True

    source = repository.list_sources()[0]
    assert source.state.value == "pending"
    assert source.next_attempt_at is not None


def test_non_busy_source_attach_failure_is_not_retried(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = StudioRepository(database)
    payload = tmp_path / "notes.txt"
    payload.write_text("Lecture notes content.", encoding="utf-8")
    repository.create_source(
        "Neuro",
        1,
        StudioSourceType.TEXT,
        "Lecture notes",
        payload_path=payload,
    )

    worker = StudioWorker(
        repository,
        _RaisingGateway(RuntimeError("boom")),
        object(),
        _FakeConnection(),
    )

    assert worker.run_once() is True

    source = repository.list_sources()[0]
    assert source.state.value == "failed"
