import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from oms_hub.models import PublishedQuizModel, StudySessionQuestionModel
from oms_hub.study_generation.native_quiz import serialize_native_quiz
from oms_hub.study_progress.review_queue import ReviewFilters, ReviewQueueService
from oms_hub.study_progress.sessions import AnswerSelection

from .test_blocks import blocks as blocks
from .test_routes import client_for
from .test_sessions import sessions as sessions


def answered(blocks, token="quiz", choice="c2", guessed=False):
    service = blocks[0].sessions
    view = service.create("owner", token)
    receipt = service.answer(
        view.id,
        view.questions[0]["attempt_id"],
        AnswerSelection(choice_id=choice),
        owner_id="owner",
        guessed=guessed,
    )
    return view, receipt


def test_guessed_is_immutable_receipted_and_reloadable_with_legacy_false_replay(blocks):
    service, bank, database, _ = blocks
    view, receipt = answered(blocks, choice="c1", guessed=True)
    attempt = view.questions[0]["attempt_id"]
    assert receipt.guessed and receipt.result == "correct"
    assert service.sessions.load(view.id, owner_id="owner").questions[0]["guessed"] is True
    assert (
        service.sessions.answer(
            view.id, attempt, AnswerSelection(choice_id="c1"), owner_id="owner", guessed=True
        )
        == receipt
    )
    with pytest.raises(ValueError, match="conflict"):
        service.sessions.answer(view.id, attempt, AnswerSelection(choice_id="c1"), owner_id="owner")
    for invalid in (1, "true", None):
        with pytest.raises(ValidationError):
            service.sessions.answer(
                view.id, attempt, AnswerSelection(choice_id="c1"), owner_id="owner", guessed=invalid
            )
    assert len(bank.iter_attempts(learner_id="owner")) == 1
    old, _ = answered(blocks, choice="c1")
    with database.session() as db:
        row = db.get(StudySessionQuestionModel, old.questions[0]["attempt_id"])
        staged = json.loads(row.selected_answer_json)
        staged.pop("guessed")
        row.selected_answer_json = json.dumps(staged)
    assert not service.sessions.answer(
        old.id, old.questions[0]["attempt_id"], AnswerSelection(choice_id="c1"), owner_id="owner"
    ).guessed
    assert service.sessions.load(old.id, owner_id="owner").questions[0]["guessed"] is False


def test_latest_answer_wrong_or_guessed_remains_correct_confident_clears(blocks):
    queue = ReviewQueueService(blocks[0])
    assert queue.catalog("owner", ReviewFilters()).total == 0
    answered(blocks)
    assert queue.catalog("owner", ReviewFilters()).items[0].reason == "missed"
    answered(blocks, choice="c1", guessed=True)
    assert queue.catalog("owner", ReviewFilters()).items[0].reason == "guessed"
    answered(blocks, choice="c2", guessed=True)
    assert queue.catalog("owner", ReviewFilters()).items[0].reason == "missed_and_guessed"
    answered(blocks, choice="c1")
    assert queue.catalog("owner", ReviewFilters()).total == 0
    assert len(blocks[1].iter_attempts(learner_id="owner")) == 4
    assert queue.catalog("other", ReviewFilters()).total == 0


def test_latest_uses_answer_time_not_session_creation_or_replay(blocks):
    service = blocks[0].sessions
    queue = ReviewQueueService(blocks[0])
    early = service.create("owner", "quiz")
    missed, _ = answered(blocks)
    service.answer(
        early.id,
        early.questions[0]["attempt_id"],
        AnswerSelection(choice_id="c1"),
        owner_id="owner",
    )
    assert not queue.catalog("owner", ReviewFilters()).items
    service.answer(
        missed.id,
        missed.questions[0]["attempt_id"],
        AnswerSelection(choice_id="c2"),
        owner_id="owner",
    )
    assert not queue.catalog("owner", ReviewFilters()).items


def test_pending_answer_does_not_clear_until_receipt_and_guessed_replay_survives_crash(
    blocks, monkeypatch
):
    block, bank, _, _ = blocks
    queue = ReviewQueueService(block)
    answered(blocks)
    original = bank.record_attempt
    view = block.sessions.create("owner", "quiz")
    attempt = view.questions[0]["attempt_id"]

    def lost_response(**kwargs):
        original(**kwargs)
        raise RuntimeError("lost receipt")

    monkeypatch.setattr(bank, "record_attempt", lost_response)
    with pytest.raises(RuntimeError):
        block.sessions.answer(
            view.id, attempt, AnswerSelection(choice_id="c1"), owner_id="owner", guessed=True
        )
    assert queue.catalog("owner", ReviewFilters()).items[0].reason == "missed"
    pending = block.sessions.load(view.id, owner_id="owner").questions[0]
    assert pending["guessed"] is True and "feedback" not in pending
    monkeypatch.setattr(bank, "record_attempt", original)
    block.sessions.answer(
        view.id, attempt, AnswerSelection(choice_id="c1"), owner_id="owner", guessed=True
    )
    assert queue.catalog("owner", ReviewFilters()).items[0].reason == "guessed"
    assert len(bank.iter_attempts(learner_id="owner")) == 2


def test_exact_course_exam_bounds_and_fresh_queue_selection(blocks, monkeypatch):
    block, _, _, _ = blocks
    queue = ReviewQueueService(block)
    answered(blocks)
    answered(blocks, token="second", choice="c1")
    answered(blocks, token="other-course")
    filters = ReviewFilters(course="neuro", exam=2, count=1)
    rows = queue.catalog("owner", filters)
    assert (
        rows.total == 3 and len(rows.items) == 1 and rows.items[0].reference.quiz_token == "second"
    )
    key = rows.items[0].key
    session = queue.create("owner", filters, selected_keys=(key,))
    assert len(session.questions) == 1 and "Exam 2" in session.questions[0]["source_label"]
    with pytest.raises(ValueError):
        queue.create("owner", ReviewFilters(course="heme"), selected_keys=(key,))
    with pytest.raises(ValueError):
        queue.create("owner", filters, selected_keys=(key, key))
    with pytest.raises(ValueError):
        queue.create("owner", filters, selected_keys=("invented",))
    create = block.sessions.create_block
    clearing = block.sessions.create("owner", "second")

    def clear_before_transaction(*args, **kwargs):
        block.sessions.answer(
            clearing.id,
            clearing.questions[0]["attempt_id"],
            AnswerSelection(choice_id="c2"),
            owner_id="owner",
        )
        return create(*args, **kwargs)

    monkeypatch.setattr(block.sessions, "create_block", clear_before_transaction)
    with pytest.raises(ValueError, match="changed"):
        queue.create("owner", filters, selected_keys=(key,))


@pytest.mark.parametrize("change", ["version", "hash", "withdrawn"])
def test_stale_or_withdrawn_questions_keep_history_but_cannot_play(blocks, change):
    block, bank, database, publications = blocks
    queue = ReviewQueueService(block)
    original, _ = answered(blocks)
    key = queue.catalog("owner", ReviewFilters()).items[0].key
    publication = publications["quiz"]
    if change == "version":
        publication = replace(publication, version=2)
    elif change == "hash":
        publication = replace(publication, quiz=replace(publication.quiz, title="Changed content"))
    else:
        publication = replace(publication, active=False)
    publications["quiz"] = publication
    with database.session() as db:
        row = db.get(PublishedQuizModel, "quiz")
        row.active, row.version = publication.active, publication.version
        row.payload_json = serialize_native_quiz(publication.quiz)
    report = queue.catalog("owner", ReviewFilters())
    assert report.total == 0 and report.unavailable == 1
    with pytest.raises(ValueError):
        queue.create("owner", ReviewFilters(), selected_keys=(key,))
    with pytest.raises(ValueError):
        block.sessions.load(original.id, owner_id="owner")
    assert len(bank.iter_attempts(learner_id="owner")) == 1


def test_routes_are_private_csrf_checked_bounded_and_do_not_leak_answers(blocks):
    block, bank, _, publications = blocks
    client, app = client_for(block.sessions, bank, [publications["quiz"]])
    app.state.study_block_service = block
    with client:
        assert "Nothing to review right now" in client.get("/study/review").text
        view = block.sessions.create("owner", "quiz")
        path = f"/study/sessions/{view.id}/answers/{view.questions[0]['attempt_id']}"
        assert client.post(path, json={"choice_id": "c1", "guessed": "true"}).status_code == 422
        result = client.post(path, json={"choice_id": "c1", "guessed": True})
        assert result.status_code == 200 and result.json()["guessed"] is True
        assert (
            client.get(f"/study/sessions/{view.id}/content").json()["questions"][0]["guessed"]
            is True
        )
        data = client.get("/study/review/data?course=neuro&exam=1&count=1")
        assert data.status_code == 200 and data.json()["matching"] == 1
        assert data.headers["cache-control"] == "private, no-store"
        assert all(
            secret not in data.text
            for secret in ("Because one", "correct_choice_id", "Secret objective", "Answer hint")
        )
        page = client.get("/study/review?course=neuro&exam=1")
        assert "Missed or guessed" in page.text and "Start review" in page.text
        assert client.get("/study/review/data?count=501").status_code == 422
        csrf = client.headers.pop("X-CSRF-Token")
        form = {
            "course": "neuro",
            "exam": 1,
            "count": 1,
            "selected_key": data.json()["items"][0]["key"],
            "csrf_token": csrf,
        }
        assert (
            client.post("/study/review/start", data=form | {"csrf_token": "bad"}).status_code == 403
        )
        created = client.post("/study/review/start", data=form, follow_redirects=False)
        assert created.status_code == 303
        assert client.get(created.headers["location"] + "/content").status_code == 200
        app.state.test_owner = "other"
        assert client.get("/study/review/data").json()["total"] == 0
        assert client.get(f"/study/sessions/{view.id}/content").status_code == 403
        app.state.test_owner = None
        assert client.get("/study/review").status_code == 401
        assert client.post("/study/review/start", data=form).status_code == 401
