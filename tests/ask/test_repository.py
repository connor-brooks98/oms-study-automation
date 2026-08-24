from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from oms_hub.ask.models import AskMode, QuizPageContext
from oms_hub.ask.repository import AskRepository
from oms_hub.db import Database
from oms_hub.providers.contracts import RetrievalScope, TruthMode


def _scope(*, lecture_ids: tuple[str, ...] = ("lecture-13",)) -> RetrievalScope:
    return RetrievalScope(
        course_id="heme",
        exam_id="exam-2",
        lecture_ids=lecture_ids,
        truth_mode=TruthMode.COURSE_ONLY,
        source_revision_ids=("sr-1",),
    )


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    database = Database(f"sqlite:///{tmp_path / 'ask.db'}")
    database.create_schema()
    yield database
    database.close()


@pytest.fixture
def repository(database: Database) -> AskRepository:
    return AskRepository(database)


def test_threads_are_actor_scoped_and_listed_by_exact_scope(
    repository: AskRepository,
) -> None:
    scope = _scope()
    alice = repository.create_thread(
        "actor-alice", AskMode.GLOBAL, scope, thread_id="thread-alice"
    )
    repository.create_thread("actor-bob", AskMode.GLOBAL, scope, thread_id="thread-bob")

    assert repository.list_threads(scope, "actor-alice") == [alice]
    with pytest.raises(KeyError):
        repository.get_thread(alice.thread_id, "actor-bob")


def test_quiz_thread_rejects_a_different_question_context(
    repository: AskRepository,
) -> None:
    first_context = QuizPageContext(
        quiz_id="quiz-1", question_id="question-1", submitted=False
    )
    second_context = QuizPageContext(
        quiz_id="quiz-1", question_id="question-2", submitted=False
    )
    thread = repository.create_thread(
        "actor-alice",
        AskMode.QUIZ_PRE_SUBMIT,
        _scope(),
        page_context=first_context,
        thread_id="thread-question-1",
    )

    repository.append_user_message(
        thread.thread_id,
        "actor-alice",
        "Explain the concept.",
        page_context=first_context,
    )
    with pytest.raises(ValueError, match="question context"):
        repository.append_user_message(
            thread.thread_id,
            "actor-alice",
            "Explain the other question.",
            page_context=second_context,
        )


def test_messages_are_append_only_and_deterministically_ordered(
    repository: AskRepository,
) -> None:
    thread = repository.create_thread(
        "actor-alice", AskMode.GLOBAL, _scope(), thread_id="thread-1"
    )
    user = repository.append_user_message(
        thread.thread_id, "actor-alice", "First question", message_id="message-1"
    )
    assistant = repository.append_assistant_message(
        thread.thread_id, "actor-alice", "First answer", message_id="message-2"
    )
    second_user = repository.append_user_message(
        thread.thread_id, "actor-alice", "Follow up", message_id="message-3"
    )

    view = repository.get_thread(thread.thread_id, "actor-alice")
    assert view.messages == (user, assistant, second_user)
    assert [message.role for message in view.messages] == [
        "user",
        "assistant",
        "user",
    ]


def test_retrieval_history_keeps_provenance_and_no_raw_evidence(
    repository: AskRepository,
    database: Database,
) -> None:
    thread = repository.create_thread(
        "actor-alice", AskMode.LECTURE, _scope(), thread_id="thread-1"
    )
    run = repository.record_retrieval_run(
        thread.thread_id,
        "actor-alice",
        source_snapshot_hash="snapshot-sha",
        evidence_ids=("ev-1", "ev-2"),
        source_revision_ids=("sr-1", "sr-2"),
        provider_request_id="provider-request-1",
        prompt_version="ask-grounded-v1",
        schema_version="ask-v1",
        model="model-1",
        validation_outcome={"state": "valid", "attempt": 1},
        retrieval_run_id="retrieval-1",
    )

    view = repository.get_thread(thread.thread_id, "actor-alice")
    assert view.retrieval_runs == (run,)
    assert run.source_snapshot_hash == "snapshot-sha"
    assert run.evidence_ids == ("ev-1", "ev-2")
    assert run.source_revision_ids == ("sr-1", "sr-2")
    assert run.provider_request_id == "provider-request-1"
    assert run.prompt_version == "ask-grounded-v1"
    assert run.schema_version == "ask-v1"
    assert run.model == "model-1"
    assert run.validation_outcome == {"state": "valid", "attempt": 1}

    columns = {
        column["name"] for column in inspect(database.engine).get_columns("retrieval_evidence")
    }
    assert "evidence_id" in columns
    assert "source_revision_id" in columns
    assert "raw_evidence" not in columns
    assert "excerpt" not in columns


def test_delete_thread_removes_owned_derivatives_but_not_canonical_evidence(
    repository: AskRepository,
    database: Database,
) -> None:
    with database.engine.begin() as connection:
        connection.execute(text("CREATE TABLE canonical_evidence (evidence_id TEXT PRIMARY KEY)"))
        connection.execute(text("INSERT INTO canonical_evidence VALUES ('ev-1')"))

    thread = repository.create_thread(
        "actor-alice", AskMode.GLOBAL, _scope(), thread_id="thread-1"
    )
    repository.append_user_message(thread.thread_id, "actor-alice", "Question")
    repository.record_retrieval_run(
        thread.thread_id,
        "actor-alice",
        source_snapshot_hash="snapshot-sha",
        evidence_ids=("ev-1",),
        source_revision_ids=("sr-1",),
        provider_request_id="provider-request-1",
        prompt_version="ask-grounded-v1",
        schema_version="ask-v1",
        model="model-1",
        validation_outcome="valid",
    )

    assert repository.delete_thread(thread.thread_id, "actor-alice") is True
    with pytest.raises(KeyError):
        repository.get_thread(thread.thread_id, "actor-alice")
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM ask_messages")) == 0
        assert connection.scalar(text("SELECT COUNT(*) FROM retrieval_runs")) == 0
        assert connection.scalar(text("SELECT COUNT(*) FROM retrieval_evidence")) == 0
        assert connection.scalar(text("SELECT COUNT(*) FROM canonical_evidence")) == 1


def test_retention_deletion_is_actor_scoped_and_explicit(
    repository: AskRepository,
) -> None:
    repository.create_thread("actor-alice", AskMode.GLOBAL, _scope(), thread_id="thread-a")
    repository.create_thread("actor-bob", AskMode.GLOBAL, _scope(), thread_id="thread-b")

    cutoff = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    assert repository.delete_threads_before("actor-alice", cutoff) == 1
    with pytest.raises(KeyError):
        repository.get_thread("thread-a", "actor-alice")
    assert repository.get_thread("thread-b", "actor-bob").thread.thread_id == "thread-b"


def test_missing_or_unauthorized_writes_fail_closed(repository: AskRepository) -> None:
    with pytest.raises(KeyError):
        repository.append_user_message("missing", "actor-alice", "Question")
    with pytest.raises(KeyError):
        repository.record_retrieval_run(
            "missing",
            "actor-alice",
            source_snapshot_hash="snapshot-sha",
            prompt_version="ask-grounded-v1",
            schema_version="ask-v1",
            model="model-1",
            validation_outcome="valid",
        )
    with pytest.raises(KeyError):
        repository.delete_thread("missing", "actor-alice")
