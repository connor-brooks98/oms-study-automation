from dataclasses import replace

import pytest
from sqlalchemy import func, select

from oms_hub.files.atomic import sha256_file
from oms_hub.models import BankImportModel, PublishedQuizMediaModel, PublishedQuizModel
from oms_hub.question_bank.contracts import TopicLabel
from oms_hub.study_generation.domain import QuizImageRef
from oms_hub.study_generation.native_quiz import serialize_native_quiz
from oms_hub.study_progress.sessions import _hash as publication_hash
from oms_hub.study_progress.sessions import question_key
from tests.study_progress.test_sessions import sessions  # noqa: F401


def native_reference(publication, question_id):
    return {
        "quiz_token": publication.token,
        "quiz_version": publication.version,
        "quiz_content_sha256": publication_hash(publication),
        "question_id": question_id,
    }


def test_native_projection_replays_without_attempts_and_preserves_review(sessions, tmp_path):  # noqa: F811
    _, bank, database, current = sessions
    ref = native_reference(current[0], "q1")
    args = ref | {"learner_id": "owner", "trusted_media_root": tmp_path}
    first = bank.register_native_projection(**args)
    assert first.key == question_key(current[0], "q1")
    label = TopicLabel(
        axis="topic",
        label="Reviewed",
        canonical_id="reviewed",
        method="user",
        review_state="accepted",
    )
    bank.set_topics(first.key, (label,), first.content_hash, reviewer_context="owner")
    replay = bank.register_native_projection(**args)
    assert replay.topics == (label,) and replay.content_hash == first.content_hash
    assert bank.iter_attempts(learner_id="owner") == ()
    assert len(bank.list_ready_questions(learner_id="owner")) == 1
    assert bank.list_ready_questions(learner_id="other") == ()
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(BankImportModel)) == 1
        assert session.get(PublishedQuizModel, "quiz").payload_json == serialize_native_quiz(
            current[0].quiz
        )
        session.get(PublishedQuizModel, "quiz").active = False
    with pytest.raises(ValueError):
        bank.register_native_projection(**args)
    with pytest.raises(ValueError):
        bank.register_native_projection(**(args | {"quiz_token": "results-only"}))


def test_projection_rejects_stale_source_and_untrusted_or_changed_media(sessions, tmp_path):  # noqa: F811
    _, bank, database, current = sessions
    publication = current[0]
    question = replace(
        publication.quiz.questions[0], image_ref=QuizImageRef("image", "Slides", "p1", "Image")
    )
    publication = replace(publication, quiz=replace(publication.quiz, questions=(question,)))
    current[0] = publication
    with database.session() as session:
        session.get(PublishedQuizModel, "quiz").payload_json = serialize_native_quiz(
            publication.quiz
        )
    args = native_reference(publication, "q1") | {
        "learner_id": "owner",
        "trusted_media_root": tmp_path,
    }
    with pytest.raises(ValueError):
        bank.register_native_projection(**(args | {"quiz_version": 2}))
    with pytest.raises(ValueError):
        bank.register_native_projection(**args)
    media_path = tmp_path / "image.png"
    media_path.write_bytes(b"approved")
    with database.session() as session:
        media = PublishedQuizMediaModel(
            quiz_token="quiz",
            image_key="image",
            path=str(media_path),
            sha256=sha256_file(media_path),
            media_type="image/png",
            width=10,
            height=10,
            alt_text="Image",
        )
        session.add(media)
    with pytest.raises(ValueError):
        bank.register_native_projection(**(args | {"trusted_media_root": tmp_path / "untrusted"}))
    ready = bank.register_native_projection(**args)
    assert ready.native_question["image_ref"]["key"] == "image"
    media_path.write_bytes(b"changed")
    with pytest.raises(ValueError):
        bank.register_native_projection(**args)
    assert bank.iter_attempts(learner_id="owner") == ()
