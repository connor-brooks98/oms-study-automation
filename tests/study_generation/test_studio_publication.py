import json
from pathlib import Path

import pytest

from oms_hub.db import Database
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.study_generation.studio_domain import (
    StudioRunStage,
    StudioRunState,
    StudioSourceType,
)
from oms_hub.study_generation.studio_repository import StudioRepository
from oms_hub.study_generation.studio_service import StudioService
from oms_hub.study_generation.studio_worker import StudioWorker


def _quiz(title: str = "Notebook title", stem: str = "Question?") -> str:
    return json.dumps(
        {
            "title": title,
            "questions": [
                {
                    "stem": stem,
                    "choices": ["A", "B"],
                    "correct_index": 0,
                    "rationale": "A is correct and B is not.",
                }
            ],
        }
    )


def _image_quiz(title: str = "Notebook title", stem: str = "Use the image.") -> str:
    return json.dumps(
        {
            "title": title,
            "questions": [
                {
                    "stem": stem,
                    "choices": ["A", "B"],
                    "correct_index": 0,
                    "rationale": "A is correct and B is not.",
                    "image_ref": {
                        "key": "image-1",
                        "source_title": "Professor website",
                        "locator": "Image immediately before question 4",
                        "description": "Reference image used for questions 4-7",
                    },
                }
            ],
        }
    )


class Gateway:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def ask_studio(self, subject, exam_number, prompt, source_ids):
        self.calls.append(list(source_ids))
        return "notebook-1", self.responses.pop(0)


class Converter:
    def convert(self, source: Path, destination: Path) -> None:
        raise AssertionError("conversion was not expected")


class Connection:
    def invalidate(self, diagnostic: str) -> object:
        return object()


def _components(tmp_path, responses):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    studio = StudioRepository(database)
    service = StudioService(studio, tmp_path / "studio", 1024 * 1024)
    published = GenerationRepository(database)
    gateway = Gateway(list(responses))
    worker = StudioWorker(
        studio,
        gateway,  # type: ignore[arg-type]
        Converter(),
        Connection(),
        published,
    )
    return database, studio, service, published, gateway, worker


def test_professor_url_chat_answer_is_relabelled_and_published_to_destination(tmp_path):
    database, studio, service, published, gateway, worker = _components(
        tmp_path, [_quiz(stem="Professor-specific question?")]
    )
    source = studio.create_source(
        "Source Course",
        1,
        StudioSourceType.URL,
        "Professor page",
        source_url="https://example.edu/professor",
    )
    studio.complete(source.id, "notebook-1", "remote-professor")
    run = service.queue_run(
        "Source Course",
        1,
        "Create a quiz from the professor page.",
        [source.id],
        "Destination Review",
        "Destination Course",
        2,
    )

    worker.run_once()

    completed = studio.get_run(run.id)
    record = published.published_quiz(completed.published_token or "")
    assert record is not None
    assert record.lecture_id is None
    assert record.studio_run_id == run.id
    assert record.destination_subject == "Destination Course"
    assert record.destination_exam_number == 2
    assert record.quiz.title == "Destination Review"
    assert record.quiz.questions[0].stem == "Professor-specific question?"
    assert gateway.calls == [["remote-professor"]]
    studio.mark_source_deleted(source.id)
    assert published.published_quiz(record.token) is not None
    database.close()


def test_image_dependent_chat_answer_waits_for_private_review(tmp_path):
    database, studio, service, published, _gateway, worker = _components(
        tmp_path,
        [_image_quiz()],
    )
    run = service.queue_run(
        "Neuro",
        1,
        "Create a quiz",
        [],
        "Image review",
        "Neuro",
        1,
    )

    worker.run_once()

    waiting = studio.get_run(run.id)
    assert waiting.state is StudioRunState.AWAITING_IMAGES
    assert waiting.stage is StudioRunStage.IMAGE_REVIEW
    assert waiting.published_token is None
    assert published.published_quizzes() == ()
    assert studio.quiz_review(run.id).requirements[0].question_ids == ("q1",)
    database.close()


def test_replacement_waiting_for_images_leaves_current_quiz_active(tmp_path):
    database, studio, service, published, _gateway, worker = _components(
        tmp_path,
        [_quiz(stem="Original question?"), _image_quiz(stem="Replacement question?")],
    )
    first = service.queue_run(
        "Neuro",
        1,
        "Create a quiz",
        [],
        "Exam review",
        "Neuro",
        1,
    )
    worker.run_once()
    original_run = studio.get_run(first.id)
    original = published.published_quiz(original_run.published_token or "")
    assert original is not None

    replacement = studio.rerun(first.id)
    worker.run_once()

    waiting = studio.get_run(replacement.id)
    still_public = published.published_quiz(original.token)
    assert waiting.state is StudioRunState.AWAITING_IMAGES
    assert waiting.published_token is None
    assert still_public is not None
    assert still_public.version == 1
    assert still_public.studio_run_id == first.id
    assert still_public.quiz.questions[0].stem == "Original question?"
    database.close()


def test_malformed_chat_json_retries_once_and_retains_both_raw_answers(tmp_path):
    database, studio, service, _published, _gateway, worker = _components(
        tmp_path,
        ["not json one", '{"title":"Still invalid","extra":true}'],
    )
    run = service.queue_run("Neuro", 1, "Create a quiz", [], "Bad response", "Neuro", 1)

    worker.run_once()
    with studio.database.session() as session:
        from oms_hub.models import StudioRunModel

        model = session.get(StudioRunModel, run.id)
        assert model is not None
        model.next_attempt_at = None
    worker.run_once()

    failed = studio.get_run(run.id)
    attempts = studio.list_run_attempts(run.id)
    assert failed.state is StudioRunState.FAILED
    assert [attempt.raw_response for attempt in attempts] == [
        "not json one",
        '{"title":"Still invalid","extra":true}',
    ]
    assert all(attempt.diagnostic_source == "contract" for attempt in attempts)
    database.close()


def test_duplicate_label_is_rejected_but_explicit_rerun_reuses_token(tmp_path):
    database, studio, service, published, _gateway, worker = _components(
        tmp_path,
        [_quiz(stem="First?"), _quiz(stem="Second?")],
    )
    first = service.queue_run("Neuro", 1, "Create a quiz", [], "Exam Review", "Neuro", 1)
    worker.run_once()
    initial = studio.get_run(first.id)
    initial_record = published.published_quiz(initial.published_token or "")
    assert initial_record is not None

    with pytest.raises(ValueError, match="label"):
        service.queue_run("Neuro", 1, "Duplicate", [], " exam review ", "Neuro", 1)

    successor = studio.rerun(first.id)
    worker.run_once()
    updated = published.published_quiz(initial_record.token)
    assert updated is not None
    assert updated.token == initial_record.token
    assert updated.version == 2
    assert updated.studio_run_id == successor.id
    assert updated.quiz.questions[0].stem == "Second?"

    published.unpublish_studio_quiz(successor.id)
    assert published.published_quiz(initial_record.token) is None
    assert len(studio.list_run_attempts(first.id)) == 1
    database.close()


def test_zero_question_response_retries_then_publishes_valid_chat_answer(tmp_path):
    database, studio, service, published, _gateway, worker = _components(
        tmp_path,
        ['{"title":"Empty","questions":[]}', _quiz(stem="Recovered?")],
    )
    run = service.queue_run(
        "Neuro", 1, "Create a quiz", [], "Recovered quiz", "Neuro", 1
    )

    worker.run_once()
    with studio.database.session() as session:
        from oms_hub.models import StudioRunModel

        model = session.get(StudioRunModel, run.id)
        assert model is not None
        model.next_attempt_at = None
    worker.run_once()

    completed = studio.get_run(run.id)
    record = published.published_quiz(completed.published_token or "")
    assert record is not None
    assert record.quiz.questions[0].stem == "Recovered?"
    attempts = studio.list_run_attempts(run.id)
    assert [attempt.diagnostic_source for attempt in attempts] == [
        "contract",
        "notebook_chat",
    ]
    database.close()
