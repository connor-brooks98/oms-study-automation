import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from oms_hub.db import Database
from oms_hub.models import StudioSourceModel
from oms_hub.study_generation.practice_domain import (
    ImportSourceRole,
    ImportSourceSelection,
    QuizContentKind,
    StudioSourcePurpose,
)
from oms_hub.study_generation.studio_domain import (
    StudioRunState,
    StudioSourceState,
    StudioSourceType,
)
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

    def prepare_studio_source_add(self, subject, exam_number):
        raise self.error

    def add_studio_source_to_notebook(
        self, notebook_id, source_type, title, **kwargs
    ):
        raise self.error

    def list_studio_source_ids(self, notebook_id):
        raise self.error

    def delete_studio_source(self, notebook_id, source_id):
        raise self.error


class _ImportWorker:
    def __init__(self) -> None:
        self.runs = []

    def run(self, run) -> None:
        self.runs.append(run)


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
    assert source.state is StudioSourceState.ATTACHING


def test_delayed_source_operation_does_not_starve_queued_studio_run(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    repository = StudioRepository(database)
    delayed = repository.create_source(
        "Neuro", 1, StudioSourceType.TEXT, "Delayed source"
    )
    assert repository.claim_next() is not None
    operation_claim = repository.claim_next_source_operation()
    assert operation_claim is not None
    operation, _ = operation_claim
    repository.record_attach_baseline(operation.id, "notebook-1", set())
    repository.mark_attach_reconciling(operation.id, "notebook", "list unavailable")
    run = _queued_run(repository)

    worker = StudioWorker(
        repository,
        _RaisingGateway(_sqlite_busy_error()),
        object(),
        _FakeConnection(),
    )

    assert worker.run_once() is True
    assert repository.get(delayed.id).state is StudioSourceState.ATTACHING
    assert repository.get_run(run.id).state is StudioRunState.RETRYING


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


def test_direct_import_run_is_delegated_without_changing_notebook_worker_behavior(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    repository = StudioRepository(database)
    source = repository.create_source(
        "Neuro",
        1,
        StudioSourceType.TEXT,
        "Questions",
        purpose=StudioSourcePurpose.LOCAL_IMPORT,
    )
    with repository.database.session() as session:
        stored = session.get(StudioSourceModel, source.id)
        assert stored is not None
        stored.state = StudioSourceState.READY.value
    run = repository.queue_import_run(
        "Neuro",
        1,
        "Import",
        "Neuro",
        1,
        QuizContentKind.PRACTICE_QUESTIONS,
        (ImportSourceSelection(source.id, ImportSourceRole.QUESTIONS),),
    )
    imports = _ImportWorker()
    worker = StudioWorker(
        repository,
        _RaisingGateway(AssertionError("NotebookLM must not be called")),
        object(),
        _FakeConnection(),
        import_worker=imports,
    )

    assert worker.run_once() is True
    assert [item.id for item in imports.runs] == [run.id]
