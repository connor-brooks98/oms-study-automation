import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from oms_hub.db import Database
from oms_hub.llm.domain import DiagnosticSource
from oms_hub.models import PublishedQuizModel, StudioRunModel, StudioSourceModel
from oms_hub.study_generation.native_quiz import parse_native_quiz, serialize_native_quiz
from oms_hub.study_generation.practice_domain import (
    ImportSourceRole,
    ImportSourceSelection,
    QuizContentKind,
    StudioSourcePurpose,
)
from oms_hub.study_generation.repository import (
    GenerationRepository,
    StudioPublicationRecoveryConflict,
)
from oms_hub.study_generation.studio_domain import (
    StudioRunStage,
    StudioRunState,
    StudioSourceState,
    StudioSourceType,
)
from oms_hub.study_generation.studio_repository import StudioRepository
from oms_hub.study_generation.studio_worker import StudioWorker

MATCHING_QUIZ_JSON = json.dumps({
    "title": "Matching set",
    "questions": [{
        "kind": "matching",
        "stem": "Match each description with its term.",
        "prompts": [
            {"label": "A", "text": "Alpha", "correct_index": 1},
            {"label": "B", "text": "Beta", "correct_index": 0},
        ],
        "choices": ["Term one", "Term two"],
        "rationale": "Source-marked matches: A -> Term two; B -> Term one.",
        "image_ref": None,
    }],
})

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

    def list_studio_source_ids(self, notebook_id, *, baseline_ids=None):
        raise self.error

    def delete_studio_source(self, notebook_id, source_id):
        raise self.error


class _NeverAskGateway(_RaisingGateway):
    def __init__(self) -> None:
        super().__init__(AssertionError("historical publication must be adopted"))
        self.ask_calls = 0

    def ask_studio(self, subject, exam_number, prompt, remote_source_ids):
        self.ask_calls += 1
        return super().ask_studio(subject, exam_number, prompt, remote_source_ids)


class _SuccessfulGateway:
    def __init__(self) -> None:
        self.ask_calls = 0

    def ask_studio(self, subject, exam_number, prompt, remote_source_ids):
        self.ask_calls += 1
        return "replacement-notebook", serialize_native_quiz(
            _quiz("Replacement response")
        )


class _ImageGateway(_SuccessfulGateway):
    def ask_studio(self, subject, exam_number, prompt, remote_source_ids):
        notebook_id, answer = super().ask_studio(subject, exam_number, prompt, remote_source_ids)
        payload = json.loads(answer)
        payload["questions"][0]["image_ref"] = {
            "key": "figure-1", "source_title": "Lecture notes", "locator": "p1",
            "description": "Required figure",
        }
        return notebook_id, json.dumps(payload)


class _MatchingGateway(_SuccessfulGateway):
    def ask_studio(self, subject, exam_number, prompt, remote_source_ids):
        del subject, exam_number, prompt, remote_source_ids
        self.ask_calls += 1
        return "matching-notebook", MATCHING_QUIZ_JSON


class _ImportWorker:
    def __init__(self) -> None:
        self.runs = []

    def run(self, run) -> None:
        self.runs.append(run)


def test_lecture_generation_delegates_once_before_any_notebook_or_publication_work(tmp_path):
    repository = StudioRepository(_database(tmp_path))
    run = _queued_run(repository)
    with repository.database.session() as session:
        stored = session.get(StudioRunModel, run.id)
        stored.workflow_kind = "lecture_generation"
        stored.backend = "codex_subscription"
    delegated = _ImportWorker()
    connection = _FakeConnection()
    worker = StudioWorker(
        repository, _RaisingGateway(AssertionError("Notebook must not be consulted")),
        object(), connection, publisher=object(), gpt_worker=delegated,
    )
    assert worker.run_once()
    assert len(delegated.runs) == 1 and delegated.runs[0].id == run.id
    assert not connection.invalidated
    assert not worker.run_once()


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


def _quiz(title: str):
    return parse_native_quiz(
        json.dumps(
            {
                "title": title,
                "questions": [
                    {
                        "stem": "Which choice is correct?",
                        "choices": ["First", "Second"],
                        "correct_index": 0,
                        "rationale": "The first choice is correct.",
                    }
                ],
            }
        )
    )


def test_studio_notebook_worker_rejects_matching_output(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = StudioRepository(database)
    publisher = GenerationRepository(database)
    run = _queued_run(repository)
    worker = StudioWorker(
        repository,
        _MatchingGateway(),
        object(),
        _FakeConnection(),
        publisher=publisher,
    )

    assert worker.run_once() is True
    rejected = repository.get_run(run.id)
    assert rejected.state is StudioRunState.RETRYING
    assert rejected.diagnostic_source == DiagnosticSource.CONTRACT.value
    assert publisher.published_quizzes(frozenset({QuizContentKind.EXAM_REVIEW})) == ()


def test_recovery_adopts_owned_publication_without_repeating_remote_chat(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    repository = StudioRepository(database)
    publisher = GenerationRepository(database)
    run = _queued_run(repository)
    claimed = repository.claim_next_run()
    assert claimed is not None and claimed.id == run.id
    repository.save_run_response(run.id, "original durable response")
    repository.set_run_stage(run.id, StudioRunStage.PUBLISH)
    original = publisher.publish_studio_quiz(run.id, _quiz("Original quiz"))
    gateway = _NeverAskGateway()
    worker = StudioWorker(
        repository,
        gateway,
        object(),
        _FakeConnection(),
        publisher=publisher,
    )

    assert worker.recover_interrupted_jobs() == 1
    assert worker.run_once() is False

    recovered = repository.get_run(run.id)
    published = publisher.published_quiz(original.token)
    assert gateway.ask_calls == 0
    assert recovered.state is StudioRunState.COMPLETE
    assert recovered.stage is StudioRunStage.COMPLETE
    assert recovered.published_token == original.token
    assert recovered.raw_response == "original durable response"
    assert published is not None and published.quiz.title == "Original quiz"
    assert [
        item.token
        for item in publisher.published_quizzes(
            frozenset({QuizContentKind.EXAM_REVIEW})
        )
    ] == [original.token]


def test_recovery_adopts_historical_publication_owned_by_already_failed_run(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    repository = StudioRepository(database)
    publisher = GenerationRepository(database)
    run = _queued_run(repository)
    claimed = repository.claim_next_run()
    assert claimed is not None
    repository.save_run_response(run.id, "durable response")
    original = publisher.publish_studio_quiz(run.id, _quiz("Historical quiz"))
    repository.fail_run(run.id, DiagnosticSource.STUDY_HUB.value, "historical split")
    gateway = _NeverAskGateway()
    worker = StudioWorker(
        repository,
        gateway,
        object(),
        _FakeConnection(),
        publisher=publisher,
    )

    assert worker.recover_interrupted_jobs() == 1

    recovered = repository.get_run(run.id)
    assert recovered.state is StudioRunState.COMPLETE
    assert recovered.stage is StudioRunStage.COMPLETE
    assert recovered.published_token == original.token
    assert recovered.raw_response == "durable response"
    assert gateway.ask_calls == 0


def test_migration_recovery_and_worker_converge_on_later_publication_owner(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with database.engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX ix_studio_runs_active_label")
    with database.session() as session:
        session.add_all([
            StudioRunModel(
                id="earlier-active-run",
                subject="Neuro",
                subject_key="neuro",
                exam_number=1,
                destination_subject="Neuro",
                destination_subject_key="neuro",
                destination_exam_number=1,
                label="Historical duplicate",
                label_key="historical duplicate",
                prompt="Draft a quiz.",
                state="queued",
                stage="validate",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            ),
            StudioRunModel(
                id="later-publication-owner",
                subject="Neuro",
                subject_key="neuro",
                exam_number=1,
                destination_subject="Neuro",
                destination_subject_key="neuro",
                destination_exam_number=1,
                label="Historical duplicate",
                label_key="historical duplicate",
                prompt="Draft a quiz.",
                state="running",
                stage="publish",
                attempts=1,
                notebook_id="notebook-1",
                raw_response="durable response",
                created_at="2026-01-02T00:00:00+00:00",
                updated_at="2026-01-02T00:00:00+00:00",
            ),
        ])
    with database.session() as session:
        session.add(PublishedQuizModel(
            token="a" * 64,
            studio_run_id="later-publication-owner",
            destination_subject="Neuro",
            destination_subject_key="neuro",
            destination_exam_number=1,
            label="Historical duplicate",
            label_key="historical duplicate",
            title="Historical duplicate",
            payload_json=serialize_native_quiz(_quiz("Historical duplicate")),
            content_kind=QuizContentKind.EXAM_REVIEW.value,
            active=True,
        ))

    database.migrate()
    with database.session() as session:
        after_first_migration = [
            (row.id, row.state, row.diagnostic_source, row.error)
            for row in session.query(StudioRunModel).order_by(StudioRunModel.id)
        ]
    database.migrate()
    with database.session() as session:
        after_second_migration = [
            (row.id, row.state, row.diagnostic_source, row.error)
            for row in session.query(StudioRunModel).order_by(StudioRunModel.id)
        ]

    gateway = _NeverAskGateway()
    repository = StudioRepository(database)
    publisher = GenerationRepository(database)
    worker = StudioWorker(
        repository,
        gateway,
        object(),
        _FakeConnection(),
        publisher=publisher,
    )

    assert after_second_migration == after_first_migration
    assert worker.recover_interrupted_jobs() == 1
    assert worker.run_once() is False
    assert worker.recover_interrupted_jobs() == 0

    runs = {run.id: run for run in repository.list_runs()}
    publications = publisher.published_quizzes(
        frozenset({QuizContentKind.EXAM_REVIEW})
    )
    assert gateway.ask_calls == 0
    assert len(publications) == 1
    assert publications[0].token == "a" * 64
    assert runs["later-publication-owner"].state is StudioRunState.COMPLETE
    assert runs["later-publication-owner"].published_token == "a" * 64
    assert runs["earlier-active-run"].state is StudioRunState.FAILED
    assert all(
        run.state not in {
            StudioRunState.QUEUED,
            StudioRunState.RUNNING,
            StudioRunState.RETRYING,
        }
        for run in runs.values()
    )


def test_worker_terminally_rejects_claim_when_another_run_owns_publication(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    repository = StudioRepository(database)
    publisher = GenerationRepository(database)
    owner = _queued_run(repository)
    assert repository.claim_next_run() is not None
    publication = publisher.publish_studio_quiz(owner.id, _quiz("Practice Quiz"))
    assert publisher.adopt_owned_studio_publication(owner.id) is not None
    with database.session() as session:
        session.add(StudioRunModel(
            id="conflicting-claimed-run",
            subject="Neuro",
            subject_key="neuro",
            exam_number=1,
            destination_subject="Neuro",
            destination_subject_key="neuro",
            destination_exam_number=1,
            label="Practice Quiz",
            label_key="practice quiz",
            prompt="Draft a duplicate quiz.",
            state="queued",
            stage="validate",
        ))
    competitor = repository.get_run("conflicting-claimed-run")
    gateway = _NeverAskGateway()
    worker = StudioWorker(
        repository,
        gateway,
        object(),
        _FakeConnection(),
        publisher=publisher,
    )

    assert worker.run_once() is True

    rejected = repository.get_run(competitor.id)
    assert gateway.ask_calls == 0
    assert rejected.state is StudioRunState.FAILED
    assert rejected.diagnostic_source == "recovery"
    assert rejected.error == (
        f"active publication {publication.token} is owned by Studio run {owner.id}; "
        "remote chat was not created"
    )


def test_rerun_successor_may_chat_while_predecessor_publication_stays_live(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    repository = StudioRepository(database)
    publisher = GenerationRepository(database)
    predecessor = _queued_run(repository)
    claimed_predecessor = repository.claim_next_run()
    assert claimed_predecessor is not None
    publication = publisher.publish_studio_quiz(
        predecessor.id,
        _quiz("Practice Quiz"),
    )
    assert publisher.adopt_owned_studio_publication(predecessor.id) is not None

    successor = repository.rerun(predecessor.id)
    gateway = _SuccessfulGateway()
    worker = StudioWorker(
        repository,
        gateway,
        object(),
        _FakeConnection(),
        publisher=publisher,
    )

    assert worker.recover_interrupted_jobs() == 0
    assert repository.get_run(successor.id).state is StudioRunState.QUEUED
    assert worker.run_once() is True

    completed = repository.get_run(successor.id)
    assert gateway.ask_calls == 1
    assert completed.state is StudioRunState.COMPLETE
    assert completed.stage is StudioRunStage.COMPLETE
    still_published = publisher.published_quiz(publication.token)
    assert still_published is not None
    assert still_published.studio_run_id == successor.id
    assert still_published.version == publication.version + 1


def test_startup_recovery_completes_owner_and_retires_conflicting_active_run(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with database.engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX ix_studio_runs_active_label")
    with database.session() as session:
        session.add_all([
            StudioRunModel(
                id="startup-owner",
                subject="Neuro",
                subject_key="neuro",
                exam_number=1,
                destination_subject="Neuro",
                destination_subject_key="neuro",
                destination_exam_number=1,
                label="Startup conflict",
                label_key="startup conflict",
                prompt="Owner",
                state="running",
                stage="publish",
            ),
            StudioRunModel(
                id="startup-competitor",
                subject="Neuro",
                subject_key="neuro",
                exam_number=1,
                destination_subject="Neuro",
                destination_subject_key="neuro",
                destination_exam_number=1,
                label="Startup conflict",
                label_key="startup conflict",
                prompt="Competitor",
                state="queued",
                stage="validate",
            ),
        ])
    with database.session() as session:
        session.add(PublishedQuizModel(
            token="b" * 64,
            studio_run_id="startup-owner",
            destination_subject="Neuro",
            destination_subject_key="neuro",
            destination_exam_number=1,
            label="Startup conflict",
            label_key="startup conflict",
            title="Startup conflict",
            payload_json=serialize_native_quiz(_quiz("Startup conflict")),
            content_kind=QuizContentKind.EXAM_REVIEW.value,
            active=True,
        ))
    publisher = GenerationRepository(database)

    assert publisher.recover_owned_studio_publications() == 1
    assert publisher.recover_owned_studio_publications() == 0

    with database.session() as session:
        owner = session.get(StudioRunModel, "startup-owner")
        competitor = session.get(StudioRunModel, "startup-competitor")
        assert owner is not None and competitor is not None
        assert (owner.state, owner.stage, owner.published_token) == (
            "complete",
            "complete",
            "b" * 64,
        )
        assert competitor.state == "failed"
        assert competitor.diagnostic_source == "recovery"
        assert competitor.next_attempt_at is None
        assert competitor.error == (
            f"active publication {'b' * 64} is owned by Studio run startup-owner; "
            "conflicting run retired during startup recovery"
        )


def test_startup_recovery_fails_closed_on_multiple_active_publications(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with database.session() as session:
        session.add_all([
            StudioRunModel(
                id=run_id,
                subject="Neuro",
                subject_key="neuro",
                exam_number=1,
                destination_subject="Neuro",
                destination_subject_key="neuro",
                destination_exam_number=1,
                label="Ambiguous",
                label_key="ambiguous",
                prompt="Historical",
                state="failed",
                stage="publish",
            )
            for run_id in ("ambiguous-owner-1", "ambiguous-owner-2")
        ])
    with database.session() as session:
        session.add_all([
            PublishedQuizModel(
                token=token * 64,
                studio_run_id=run_id,
                destination_subject="Neuro",
                destination_subject_key="neuro",
                destination_exam_number=1,
                label="Ambiguous",
                label_key="ambiguous",
                title="Ambiguous",
                payload_json=serialize_native_quiz(_quiz("Ambiguous")),
                content_kind=QuizContentKind.EXAM_REVIEW.value,
                active=True,
            )
            for token, run_id in (
                ("c", "ambiguous-owner-1"),
                ("d", "ambiguous-owner-2"),
            )
        ])

    with pytest.raises(
        StudioPublicationRecoveryConflict,
        match=(
            "startup recovery conflict: multiple active Studio publications exist "
            "for neuro exam 1 label ambiguous"
        ),
    ):
        GenerationRepository(database).recover_owned_studio_publications()


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


def _make_run_retry_due(repository: StudioRepository, run_id: str) -> None:
    with repository.database.session() as session:
        stored = session.get(StudioRunModel, run_id)
        assert stored is not None
        stored.next_attempt_at = None


def test_publication_retry_reuses_saved_notebook_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    repository = StudioRepository(database)
    publisher = GenerationRepository(database)
    run = _queued_run(repository)
    gateway = _SuccessfulGateway()
    worker = StudioWorker(repository, gateway, object(), _FakeConnection(), publisher)
    publish = publisher.publish_and_complete_studio_run

    def busy_publish(*args, **kwargs):
        raise _sqlite_busy_error()

    monkeypatch.setattr(publisher, "publish_and_complete_studio_run", busy_publish)
    assert worker.run_once() is True
    assert repository.get_run(run.id).state is StudioRunState.RETRYING
    assert repository.get_run(run.id).notebook_id == "replacement-notebook"

    monkeypatch.setattr(publisher, "publish_and_complete_studio_run", publish)
    _make_run_retry_due(repository, run.id)
    assert worker.run_once() is True
    assert repository.get_run(run.id).state is StudioRunState.COMPLETE
    assert gateway.ask_calls == 1


def test_interrupted_run_reuses_saved_notebook_response(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = StudioRepository(database)
    publisher = GenerationRepository(database)
    run = _queued_run(repository)
    assert repository.claim_next_run() is not None
    answer = serialize_native_quiz(_quiz("Durable response"))
    repository.save_run_response(run.id, answer, "original-notebook")
    gateway = _NeverAskGateway()
    worker = StudioWorker(repository, gateway, object(), _FakeConnection(), publisher)

    assert worker.recover_interrupted_jobs() == 1
    assert worker.run_once() is True
    completed = repository.get_run(run.id)
    assert completed.state is StudioRunState.COMPLETE
    assert completed.notebook_id == "original-notebook"
    assert completed.raw_response == answer
    assert gateway.ask_calls == 0


@pytest.mark.parametrize("valid_retry", [True, False])
def test_invalid_cached_response_gets_bounded_fresh_chat_retry(
    tmp_path: Path, valid_retry: bool,
) -> None:
    database = _database(tmp_path)
    repository = StudioRepository(database)
    run = _queued_run(repository)
    gateway = _SuccessfulGateway()
    valid_answer = serialize_native_quiz(_quiz("Valid response"))

    def answer_sequence(subject, exam_number, prompt, remote_source_ids):
        gateway.ask_calls += 1
        answer = valid_answer if valid_retry and gateway.ask_calls > 1 else "invalid JSON"
        return "notebook-1", answer

    gateway.ask_studio = answer_sequence
    worker = StudioWorker(
        repository, gateway, object(), _FakeConnection(), GenerationRepository(database)
    )

    assert worker.run_once() is True
    assert repository.get_run(run.id).state is StudioRunState.RETRYING
    _make_run_retry_due(repository, run.id)
    assert worker.run_once() is True
    expected_state = StudioRunState.COMPLETE if valid_retry else StudioRunState.FAILED
    assert repository.get_run(run.id).state is expected_state
    assert gateway.ask_calls == 2
    attempts = repository.list_run_attempts(run.id)
    assert attempts[0].raw_response == "invalid JSON"
    assert attempts[0].diagnostic_source == "contract"


def test_attempt_save_busy_retries_without_repeating_saved_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    repository = StudioRepository(database)
    run = _queued_run(repository)
    gateway = _SuccessfulGateway()
    worker = StudioWorker(
        repository, gateway, object(), _FakeConnection(), GenerationRepository(database)
    )
    record_attempt = repository.record_run_attempt
    record_calls = 0

    def busy_once(*args, **kwargs):
        nonlocal record_calls
        record_calls += 1
        if record_calls == 1:
            raise _sqlite_busy_error()
        return record_attempt(*args, **kwargs)

    monkeypatch.setattr(repository, "record_run_attempt", busy_once)
    assert worker.run_once() is True
    assert repository.get_run(run.id).state is StudioRunState.RETRYING
    _make_run_retry_due(repository, run.id)
    assert worker.run_once() is True
    assert repository.get_run(run.id).state is StudioRunState.COMPLETE
    assert gateway.ask_calls == 1


@pytest.mark.parametrize("failure_point", ["set_run_stage", "await_image_review", "complete_run"])
def test_local_generation_write_busy_retries_using_saved_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str,
) -> None:
    database = _database(tmp_path)
    repository = StudioRepository(database)
    run = _queued_run(repository)
    has_images = failure_point == "await_image_review"
    gateway = _ImageGateway() if has_images else _SuccessfulGateway()
    publisher = None if failure_point == "complete_run" else GenerationRepository(database)
    worker = StudioWorker(repository, gateway, object(), _FakeConnection(), publisher)
    original = getattr(repository, failure_point)

    def busy_write(*args, **kwargs):
        raise _sqlite_busy_error()

    monkeypatch.setattr(repository, failure_point, busy_write)
    assert worker.run_once() is True
    assert repository.get_run(run.id).state is StudioRunState.RETRYING
    assert repository.get_run(run.id).raw_response is not None

    monkeypatch.setattr(repository, failure_point, original)
    _make_run_retry_due(repository, run.id)
    assert worker.run_once() is True
    expected = StudioRunState.AWAITING_IMAGES if has_images else StudioRunState.COMPLETE
    assert repository.get_run(run.id).state is expected
    assert gateway.ask_calls == 1


def test_autobind_failure_preserves_manual_image_review(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    database = _database(tmp_path)
    repository = StudioRepository(database)
    run = _queued_run(repository)
    gateway = _ImageGateway()

    class FailingImages:
        def auto_bind_from_sources(self, run_id, requirements, sources):
            raise RuntimeError("private payload should not be logged")

    worker = StudioWorker(
        repository, gateway, object(), _FakeConnection(), GenerationRepository(database),
        image_service=FailingImages(),
    )

    assert worker.run_once() is True
    stored = repository.get_run(run.id)
    assert stored.state is StudioRunState.AWAITING_IMAGES
    assert stored.stage is StudioRunStage.IMAGE_REVIEW
    review = repository.quiz_review(run.id)
    assert review.unresolved_keys == ("figure-1",)
    assert worker.run_once() is False
    assert gateway.ask_calls == 1
    assert "RuntimeError" in caplog.text
    assert "private payload" not in caplog.text


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
    payload = tmp_path / "notes.txt"
    payload.write_text("Lecture notes", encoding="utf-8")
    delayed = repository.create_source(
        "Neuro", 1, StudioSourceType.TEXT, "Delayed source", payload_path=payload
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
