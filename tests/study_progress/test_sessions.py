from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select

from oms_hub.db import Database
from oms_hub.models import PublishedQuizModel, StudySessionQuestionModel
from oms_hub.question_bank.repository import BankRepository
from oms_hub.study_generation.domain import (
    NativeQuiz,
    PublishedQuizRecord,
    QuizChoice,
    QuizQuestion,
)
from oms_hub.study_generation.native_quiz import serialize_native_quiz
from oms_hub.study_progress.sessions import AnswerSelection, StudySessionService, question_key


@pytest.fixture
def sessions(tmp_path):
    quiz = NativeQuiz(
        "Practice",
        (
            QuizQuestion(
                "q1",
                "Which?",
                (
                    QuizChoice("c1", "One"),
                    QuizChoice("c2", "Two"),
                    QuizChoice("c3", "Three"),
                    QuizChoice("c4", "Four"),
                ),
                "c1",
                "Because one.",
                topic="Answer hint",
                learning_objective="Secret objective",
            ),
        ),
    )
    publication = PublishedQuizRecord(
        "quiz", None, None, None, "Neuro", "neuro", 1, "Practice", quiz.title, quiz, 1, True
    )
    with Database(f"sqlite:///{tmp_path / 'sessions.db'}") as database:
        database.migrate()
        with database.session() as session:
            session.add(
                PublishedQuizModel(
                    token="quiz", title=quiz.title, payload_json=serialize_native_quiz(quiz)
                )
            )
        bank = BankRepository(database.session)
        current = [publication]

        def load(owner, token):
            if owner != "owner" or token != "quiz":
                raise PermissionError
            return current[0]

        service = StudySessionService(
            database.session,
            bank=bank,
            load_quiz=load,
            topics_for=lambda owner, key: (),
            media_for=lambda owner, quiz: (),
        )
        yield service, bank, database, current


def test_delivery_binding_and_replay_records_once(sessions):
    service, bank, _, current = sessions
    view = service.create("owner", "quiz")
    question = view.questions[0]
    assert "correct" not in str(question) and "Secret objective" not in str(question)
    assert "Answer hint" not in str(question)
    answer = AnswerSelection(choice_id="c1")
    first = service.answer(view.id, question["attempt_id"], answer, owner_id="owner", elapsed_ms=42)
    assert first.result == "correct"
    assert first == service.answer(
        view.id, question["attempt_id"], answer, owner_id="owner", elapsed_ms=42
    )
    assert len(bank.iter_attempts(learner_id="owner")) == 1
    with pytest.raises(ValueError):
        service.answer(
            view.id,
            question["attempt_id"],
            AnswerSelection(choice_id="c2"),
            owner_id="owner",
            elapsed_ms=42,
        )
    with pytest.raises(ValueError):
        service.answer(view.id, question["attempt_id"], answer, owner_id="owner", elapsed_ms=43)
    with pytest.raises(PermissionError):
        service.answer(view.id, question["attempt_id"], answer, owner_id="other", elapsed_ms=42)
    with pytest.raises(PermissionError):
        service.answer(view.id, str(uuid4()), answer, owner_id="owner", elapsed_ms=42)
    second = service.create("owner", "quiz")
    with pytest.raises(PermissionError):
        service.answer(second.id, question["attempt_id"], answer, owner_id="owner", elapsed_ms=42)
    key = question_key(current[0], "q1")
    assert len(key.question_id) == 71
    assert key != question_key(replace(current[0], version=2), "q1")
    assert key != question_key(replace(current[0], token="else"), "q1")


def test_crash_after_bank_commit_replays_staged_fact_without_regrading(sessions, monkeypatch):
    service, bank, database, current = sessions
    view = service.create("owner", "quiz")
    attempt = view.questions[0]["attempt_id"]
    original = bank.record_attempt

    def fail_after_commit(**kwargs):
        original(**kwargs)
        raise RuntimeError("simulated lost response")

    monkeypatch.setattr(bank, "record_attempt", fail_after_commit)
    with pytest.raises(RuntimeError):
        service.answer(
            view.id, attempt, AnswerSelection(choice_id="c1"), owner_id="owner", elapsed_ms=42
        )
    with database.session() as session:
        row = session.scalar(select(StudySessionQuestionModel))
        assert row.selected_answer_json and row.bank_attempt_id is None
    pending = service.load(view.id, owner_id="owner").questions[0]
    assert pending["answer"]["choice_id"] == "c1" and "feedback" not in pending
    assert pending["elapsed_ms"] == 42
    monkeypatch.setattr(bank, "record_attempt", original)
    current[0] = replace(current[0], version=2)
    assert (
        service.answer(
            view.id, attempt, AnswerSelection(choice_id="c1"), owner_id="owner", elapsed_ms=42
        ).result
        == "correct"
    )
    assert len(bank.iter_attempts(learner_id="owner")) == 1


def test_changed_content_and_closed_sessions_reject_new_answers(sessions):
    service, bank, _, current = sessions
    view = service.create("owner", "quiz")
    current[0] = replace(current[0], version=2)
    with pytest.raises(ValueError):
        service.answer(
            view.id,
            view.questions[0]["attempt_id"],
            AnswerSelection(choice_id="c1"),
            owner_id="owner",
        )
    current[0] = replace(current[0], version=1)
    service.close(view.id, owner_id="owner")
    with pytest.raises(ValueError):
        service.answer(
            view.id,
            view.questions[0]["attempt_id"],
            AnswerSelection(choice_id="c1"),
            owner_id="owner",
        )
    assert bank.iter_attempts(learner_id="owner") == ()


def test_concurrent_identical_answers_share_receipt(sessions):
    from concurrent.futures import ThreadPoolExecutor

    service, bank, _, _ = sessions
    view = service.create("owner", "quiz")
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(
            pool.map(
                lambda _: service.answer(
                    view.id,
                    view.questions[0]["attempt_id"],
                    AnswerSelection(choice_id="c1"),
                    owner_id="owner",
                ),
                range(2),
            )
        )
    assert answers[0] == answers[1]
    assert len(bank.iter_attempts(learner_id="owner")) == 1


def test_matching_grading_missing_media_and_hash_binding(sessions, tmp_path):
    from oms_hub.files.atomic import sha256_file
    from oms_hub.study_generation.domain import (
        PublishedQuizMediaRecord,
        QuizImageRef,
        QuizMatchingPrompt,
        QuizMatchingQuestion,
    )

    service, bank, database, current = sessions
    original = current[0]
    matching = QuizMatchingQuestion(
        "q1",
        "Match",
        (QuizMatchingPrompt("p1", "A", "Alpha", "c2"), QuizMatchingPrompt("p2", "B", "Beta", "c1")),
        original.quiz.questions[0].choices,
        "Because.",
        image_ref=QuizImageRef("image1", "Slides", "p1", "Answer hint"),
    )
    current[0] = replace(original, quiz=NativeQuiz("Matching", (matching,)))
    assert question_key(original, "q1") != question_key(current[0], "q1")
    with database.session() as session:
        session.get(PublishedQuizModel, "quiz").payload_json = serialize_native_quiz(
            current[0].quiz
        )
    with pytest.raises(ValueError, match="media"):
        service.create("owner", "quiz")
    image = tmp_path / "question.png"
    image.write_bytes(b"test media")
    media = PublishedQuizMediaRecord(
        "quiz", "image1", image, sha256_file(image), "image/png", 100, 100, "Answer hint"
    )
    service.media_for = lambda owner, quiz: (media,)
    view = service.create("owner", "quiz")
    assert view.questions[0]["image_alt"] == "Question image"
    assert "Answer hint" not in str(view.questions)
    with pytest.raises(ValueError):
        service.answer(
            view.id,
            view.questions[0]["attempt_id"],
            AnswerSelection(kind="matching", matches={"p1": "c2"}),
            owner_id="owner",
        )
    answer = service.answer(
        view.id,
        view.questions[0]["attempt_id"],
        AnswerSelection(kind="matching", matches={"p1": "c2", "p2": "c1"}),
        owner_id="owner",
    )
    assert answer.result == "correct" and answer.feedback["kind"] == "matching"
    image.write_bytes(b"changed")
    with pytest.raises(ValueError, match="media"):
        service.load(view.id, owner_id="owner")
    assert len(bank.iter_attempts(learner_id="owner")) == 1
