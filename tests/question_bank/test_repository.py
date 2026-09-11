import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import event, func, select

from oms_hub.db import Database
from oms_hub.models import (
    BankAttemptModel,
    BankImportModel,
    BankImportRowModel,
    BankQuestionModel,
    BankTopicReviewModel,
)
from oms_hub.question_bank.contracts import QuestionKey, TopicLabel
from oms_hub.question_bank.imports import preview_import
from oms_hub.question_bank.repository import BankRepository


@pytest.fixture
def bank_repo(tmp_path):
    with Database(f"sqlite:///{tmp_path / 'bank.db'}") as database:
        database.migrate()
        yield BankRepository(database.session), database


def payload():
    return json.loads((Path(__file__).parent / "fixtures/normalized-v1.json").read_bytes())


def preview(data):
    return preview_import(json.dumps(data).encode())


def commit(repo, data, learner="test"):
    candidate = preview(data)
    return repo.commit_import(candidate, learner_id=learner, expected_digest=candidate.digest)


def key(data):
    return QuestionKey(
        source=data["source"], product=data["product"], question_id=data["rows"][0]["question_id"]
    )


def with_body(data):
    data["provenance"]["kind"] = "authorized_question_export"
    data["rows"][0]["question"] = {
        "stem": "Choose A.",
        "choices": ["A", "B"],
        "correct_index": 0,
        "rationale": "A is specified.",
    }
    return data


def counts(database):
    with database.session() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (BankImportModel, BankQuestionModel, BankImportRowModel, BankAttemptModel)
        )


def test_reimport_does_not_invent_attempts(bank_repo):
    repo, database = bank_repo
    data = payload()
    first = commit(repo, data)
    assert commit(repo, data) == first
    assert (first.inserted_questions, first.inserted_attempts, first.duplicate_rows) == (1, 1, 0)
    assert counts(database) == (1, 1, 1, 1)
    facts = repo.iter_attempts(learner_id="test")
    assert len(facts) == 1
    assert facts[0].key.question_id == "00123"
    assert facts[0].result == "incorrect"
    assert repo.iter_attempts(learner_id="other") == ()
    assert (
        repo.review_rows(import_id=first.import_id, learner_id="test")
        == preview(data).envelope.rows
    )
    with pytest.raises(ValueError):
        repo.review_rows(import_id=first.import_id, learner_id="other")


def test_duplicate_rows_exports_and_new_attempts(bank_repo):
    repo, database = bank_repo
    data = payload()
    data["rows"] *= 2
    first = commit(repo, data)
    assert first.duplicate_rows == 1
    assert counts(database) == (1, 1, 2, 1)
    data["export_id"] = "another-export"
    data["rows"] = [data["rows"][0], dict(data["rows"][0], attempt_id="second")]
    second = commit(repo, data)
    assert (second.inserted_questions, second.inserted_attempts, second.duplicate_rows) == (0, 1, 1)
    assert len(repo.iter_attempts(learner_id="test")) == 2


def test_source_product_case_and_learner_are_separate(bank_repo):
    repo, database = bank_repo
    data = payload()
    commit(repo, data)
    commit(repo, data, "other")
    data["source"] = "truelearn"
    commit(repo, data)
    data["product"] = "Step1"
    commit(repo, data)
    data["rows"][0]["question_id"] = "123"
    data["export_id"] = "different-qid-export"
    commit(repo, data)
    assert counts(database) == (5, 4, 5, 5)
    assert len(repo.iter_attempts(learner_id="test")) == 4
    assert len(repo.iter_attempts(learner_id="other")) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"result": "correct"},
        {"occurred_at": None},
        {"elapsed_ms": 1},
        {"user_note": "changed note"},
        {"tags": ["different"]},
    ],
)
def test_changed_attempt_rolls_back_whole_export(bank_repo, change):
    repo, database = bank_repo
    data = payload()
    commit(repo, data)
    before = counts(database)
    data["export_id"] = "new-export"
    original = dict(data["rows"][0])
    data["rows"] = [dict(original, question_id="new-q", attempt_id="new-a"), original | change]
    receipt = commit(repo, data)
    assert receipt.conflicts and receipt.conflicts[0].row == 2
    assert (receipt.inserted_questions, receipt.inserted_attempts) == (0, 0)
    assert counts(database) == before


def test_export_identity_and_within_file_conflict(bank_repo):
    repo, database = bank_repo
    data = payload()
    commit(repo, data)
    data["rows"][0]["user_note"] = "changed"
    assert commit(repo, data).conflicts
    data["export_id"] = "new"
    data["rows"].append(dict(data["rows"][0], result="correct"))
    assert commit(repo, data).conflicts[0].code == "duplicate_conflict"
    assert counts(database) == (1, 1, 1, 1)


def test_digest_and_forged_preview_cannot_bypass_validation(bank_repo):
    repo, database = bank_repo
    candidate = preview(payload())
    with pytest.raises(ValueError):
        repo.commit_import(candidate, learner_id="test", expected_digest="0" * 64)
    tampered = replace(candidate, envelope=candidate.envelope.model_copy(update={"export_id": "x"}))
    with pytest.raises(ValueError):
        repo.commit_import(tampered, learner_id="test", expected_digest=candidate.digest)
    for learner in ("", " test", None):
        with pytest.raises(ValueError):
            repo.commit_import(candidate, learner_id=learner, expected_digest=candidate.digest)
    assert counts(database) == (0, 0, 0, 0)


def test_held_bodies_and_conflicting_content(bank_repo):
    repo, database = bank_repo
    data = with_body(payload())
    data["rows"][0]["question"]["correct_index"] = -1
    assert not commit(repo, data).conflicts
    assert repo.get_question(key(data)) is None
    assert repo.list_ready_questions(learner_id="test") == ()
    data["export_id"] = "changed-body"
    data["rows"][0]["question"]["correct_index"] = 0
    assert commit(repo, data).conflicts
    assert counts(database) == (1, 1, 1, 1)


def test_metadata_identity_enriches_once_without_changing_original_rows(bank_repo):
    repo, database = bank_repo
    data = payload()
    first = commit(repo, data)
    original = repo.review_rows(import_id=first.import_id, learner_id="test")
    data = with_body(data)
    data["export_id"] = "first-body"
    data["rows"][0]["attempt_id"] = "second-attempt"
    assert not commit(repo, data).conflicts
    assert repo.get_question(key(data)) is not None
    assert repo.review_rows(import_id=first.import_id, learner_id="test") == original
    assert counts(database) == (2, 1, 2, 2)


def test_nested_nonfinite_mutation_cannot_hide_as_original_null(bank_repo):
    repo, database = bank_repo
    data = with_body(payload())
    data["rows"][0]["question"]["image_ref"] = None
    candidate = preview(data)
    candidate.envelope.rows[0].question["image_ref"] = float("inf")
    with pytest.raises(ValueError):
        repo.commit_import(candidate, learner_id="test", expected_digest=candidate.digest)
    assert counts(database) == (0, 0, 0, 0)


def test_native_requires_attempt_id_before_any_write(bank_repo):
    repo, database = bank_repo
    with pytest.raises(ValueError):
        repo.record_attempt(
            learner_id="test",
            key=key(payload()),
            attempt_id=None,
            result="unknown",
            occurred_at=None,
            elapsed_ms=None,
        )
    assert counts(database) == (0, 0, 0, 0)


def test_qid_notes_never_invent_attempts_and_images_remain_held(bank_repo):
    repo, database = bank_repo
    data = payload()
    data["rows"][0].update(attempt_id=None, result="unknown", occurred_at=None, elapsed_ms=None)
    first = commit(repo, data)
    assert first.inserted_attempts == 0
    assert repo.iter_attempts(learner_id="test") == ()
    data = with_body(data)
    data["export_id"] = "image-body"
    data["rows"][0]["question"]["image_ref"] = {
        "key": "image-1",
        "source_title": "Fixture",
        "locator": "page 1",
        "description": "Test",
    }
    assert not commit(repo, data).conflicts
    assert repo.get_question(key(data)) is None
    assert repo.list_ready_questions(learner_id="test") == ()
    assert counts(database) == (2, 1, 2, 0)


def test_reviewed_topics_hash_guard_filters_and_raw_provenance(bank_repo):
    repo, database = bank_repo
    data = with_body(payload())
    first = commit(repo, data)
    q = repo.get_question(key(data))
    assert q is not None and len(q.content_hash) == 64
    assert repo.list_ready_questions(learner_id="other") == ()
    assert repo.list_ready_questions(learner_id="test", topic_ids=("topic:a",)) == ()
    topics = (
        TopicLabel(
            axis="topic", label="A", canonical_id="topic:a", method="user", review_state="accepted"
        ),
        TopicLabel(
            axis="course",
            label="C",
            canonical_id="course:c",
            method="user",
            review_state="accepted",
        ),
        TopicLabel(
            axis="exam", label="E", canonical_id="exam:e", method="user", review_state="accepted"
        ),
    )
    with pytest.raises(ValueError):
        repo.set_topics(key(data), topics, "0" * 64, reviewer_context="test")
    assert repo.get_question(key(data)) == q
    repo.set_topics(key(data), topics, q.content_hash, reviewer_context="test")
    assert repo.get_question(key(data)).topics == topics
    assert (
        len(
            repo.list_ready_questions(
                learner_id="test", course="course:c", exam="exam:e", topic_ids=("topic:a",)
            )
        )
        == 1
    )
    assert (
        repo.review_rows(import_id=first.import_id, learner_id="test")
        == preview(data).envelope.rows
    )
    with database.session() as session:
        audit = session.scalars(select(BankTopicReviewModel)).one()
        assert json.loads(audit.previous_topics_json) == []
        assert json.loads(audit.reviewer_context_json) == {"actor": "test"}
    data["export_id"] = "new"
    data["rows"][0]["attempt_id"] = "new"
    commit(repo, data)
    assert repo.get_question(key(data)).topics == topics
    assert any(t.canonical_id == "topic:a" for t in repo.iter_attempts(learner_id="test")[0].topics)


def test_imported_review_claim_is_only_a_proposal_and_no_body_leak(bank_repo):
    repo, _ = bank_repo
    data = with_body(payload())
    data["rows"][0]["topics"][0].update(canonical_id="system:heme", review_state="accepted")
    commit(repo, data)
    assert repo.list_ready_questions(learner_id="test", topic_ids=("system:heme",)) == ()
    assert all(
        t.review_state != "accepted" for t in repo.iter_attempts(learner_id="test")[0].topics
    )
    data["rows"][0]["question"] = None
    commit(repo, data, "other")
    assert repo.list_ready_questions(learner_id="other") == ()


def test_unknown_omitted_null_times_and_keyset_order(bank_repo):
    repo, _ = bank_repo
    data = payload()
    data["rows"] = [
        dict(data["rows"][0], attempt_id=str(i), result=result, occurred_at=None)
        for i, result in enumerate(("unknown", "omitted", "incorrect", "correct"))
    ]
    commit(repo, data)
    first = repo.iter_attempts(learner_id="test", limit=2)
    rest = repo.iter_attempts(learner_id="test", after_id=first[-1].id)
    assert [f.result for f in first + rest] == ["unknown", "omitted", "incorrect", "correct"]
    assert all(f.occurred_at is None and f.imported_at.tzinfo is not None for f in first + rest)
    for limit in (0, 501, True):
        with pytest.raises(ValueError):
            repo.iter_attempts(learner_id="test", limit=limit)


def test_native_attempt_without_question_body(bank_repo):
    repo, database = bank_repo
    kwargs = dict(
        learner_id="test",
        key=QuestionKey(source="study_hub", product="lecture_quiz", question_id="revision-7:q1"),
        attempt_id="answer-event-1",
        result="correct",
        occurred_at=datetime(2026, 9, 11, tzinfo=UTC),
        elapsed_ms=1500,
    )
    first = repo.record_attempt(**kwargs)
    assert repo.record_attempt(**kwargs) == first
    assert repo.iter_attempts(learner_id="test") == (first,)
    with pytest.raises(ValueError):
        repo.record_attempt(**(kwargs | {"result": "incorrect"}))
    assert counts(database) == (1, 1, 1, 1)
    with database.session() as session:
        record = session.get(BankImportModel, first.import_id)
        assert json.loads(record.provenance_json)["kind"] == "study_hub_attempt"


@pytest.mark.parametrize("mode", ["same-export", "different-exports", "native"])
def test_concurrent_duplicate_writes(bank_repo, mode):
    repo, database = bank_repo
    barrier = Barrier(2)

    def write(index):
        barrier.wait()
        if mode == "native":
            return repo.record_attempt(
                learner_id="test",
                key=QuestionKey(
                    source="study_hub", product="lecture_quiz", question_id="revision:q1"
                ),
                attempt_id="event",
                result="correct",
                occurred_at=None,
                elapsed_ms=None,
            )
        data = payload()
        if mode == "different-exports":
            data["export_id"] = f"export-{index}"
        return commit(repo, data)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, index) for index in range(2)]
        results = [future.result() for future in futures]
    if mode == "different-exports":
        assert sorted(r.inserted_attempts for r in results) == [0, 1]
        assert sorted(r.duplicate_rows for r in results) == [0, 1]
        assert counts(database) == (2, 1, 2, 1)
    else:
        assert results[0] == results[1]
        assert counts(database) == (1, 1, 1, 1)


def test_insert_failure_rolls_back_all_rows(bank_repo):
    repo, database = bank_repo

    def fail_attempt(mapper, connection, target):
        raise RuntimeError("injected write failure")

    event.listen(BankAttemptModel, "before_insert", fail_attempt)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            commit(repo, payload())
    finally:
        event.remove(BankAttemptModel, "before_insert", fail_attempt)
    assert counts(database) == (0, 0, 0, 0)
