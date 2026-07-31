import json
from pathlib import Path

import pytest

from oms_hub.db import Database
from oms_hub.study_generation.native_quiz import parse_native_quiz
from oms_hub.study_generation.studio_domain import (
    StudioRunStage,
    StudioRunState,
    StudioStoredImage,
)
from oms_hub.study_generation.studio_repository import StudioRepository


def _quiz_with_shared_image(question_count: int = 2):
    image_ref = {
        "key": "image-1",
        "source_title": "Dr. Wang's website",
        "locator": "Image immediately before question 4",
        "description": "Reference image used for questions 4-7",
    }
    return parse_native_quiz(
        json.dumps(
            {
                "title": "Practice questions",
                "questions": [
                    {
                        "stem": f"Question {number}",
                        "choices": ["A", "B"],
                        "correct_index": 0,
                        "rationale": "A is correct.",
                        "image_ref": image_ref,
                    }
                    for number in range(1, question_count + 1)
                ],
            }
        )
    )


def _queued_run(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    repository = StudioRepository(database)
    run = repository.queue_run(
        "Neuro",
        1,
        "Prompt with contract",
        [],
        "Image review",
        "Neuro",
        1,
    )
    return database, repository, run


def _awaiting_run(tmp_path, question_count: int = 2):
    database, repository, run = _queued_run(tmp_path)
    repository.await_image_review(
        run.id,
        "notebook-1",
        "raw NotebookLM response",
        _quiz_with_shared_image(question_count),
    )
    return database, repository, run


def _stored_image(tmp_path: Path) -> StudioStoredImage:
    path = tmp_path / "image.png"
    path.write_bytes(b"sanitized png")
    return StudioStoredImage(
        path=path,
        sha256="a" * 64,
        media_type="image/png",
        width=1200,
        height=800,
        original_filename="figure.jpg",
    )


def test_image_review_groups_shared_key_and_survives_repository_reload(tmp_path):
    database, repository, run = _queued_run(tmp_path)

    repository.await_image_review(
        run.id,
        "notebook-1",
        "raw NotebookLM response",
        _quiz_with_shared_image(4),
    )
    review = StudioRepository(database).quiz_review(run.id)

    assert review.run.state is StudioRunState.AWAITING_IMAGES
    assert review.run.stage is StudioRunStage.IMAGE_REVIEW
    assert review.run.notebook_id == "notebook-1"
    assert review.run.raw_response == "raw NotebookLM response"
    assert review.requirements[0].image_key == "image-1"
    assert review.requirements[0].source_title == "Dr. Wang's website"
    assert review.requirements[0].question_ids == ("q1", "q2", "q3", "q4")
    assert review.requirements[0].image is None
    assert review.unresolved_keys == ("image-1",)
    database.close()


def test_no_image_override_affects_only_one_question_and_is_reversible(tmp_path):
    database, repository, run = _awaiting_run(tmp_path)

    repository.set_image_override(run.id, "q1", True)
    overridden = repository.quiz_review(run.id)

    assert overridden.overridden_question_ids == frozenset({"q1"})
    assert overridden.unresolved_keys == ("image-1",)

    repository.set_image_override(run.id, "q1", False)
    restored = repository.quiz_review(run.id)

    assert restored.overridden_question_ids == frozenset()
    assert restored.unresolved_keys == ("image-1",)
    database.close()


def test_one_bound_image_resolves_every_question_using_the_shared_key(tmp_path):
    database, repository, run = _awaiting_run(tmp_path, question_count=4)
    image = _stored_image(tmp_path)

    repository.bind_image(run.id, "image-1", image)
    review = repository.quiz_review(run.id)
    resolved = repository.resolved_quiz(run.id)

    assert review.requirements[0].image == image
    assert review.unresolved_keys == ()
    assert [question.image_ref.key for question in resolved.questions] == [
        "image-1",
        "image-1",
        "image-1",
        "image-1",
    ]
    database.close()


def test_overriding_every_linked_question_resolves_key_without_upload(tmp_path):
    database, repository, run = _awaiting_run(tmp_path)
    repository.set_image_override(run.id, "q1", True)
    repository.set_image_override(run.id, "q2", True)

    review = repository.quiz_review(run.id)
    resolved = repository.resolved_quiz(run.id)

    assert review.unresolved_keys == ()
    assert [question.image_ref for question in resolved.questions] == [None, None]
    database.close()


def test_unresolved_review_cannot_produce_publishable_quiz(tmp_path):
    database, repository, run = _awaiting_run(tmp_path)

    with pytest.raises(ValueError, match="image-1"):
        repository.resolved_quiz(run.id)

    database.close()


@pytest.mark.parametrize(
    ("question_id", "message"),
    [("q99", "question"), ("bad", "question")],
)
def test_override_rejects_question_not_in_draft(tmp_path, question_id, message):
    database, repository, run = _awaiting_run(tmp_path)

    with pytest.raises(ValueError, match=message):
        repository.set_image_override(run.id, question_id, True)

    database.close()


def test_binding_rejects_unknown_image_key_without_changing_review(tmp_path):
    database, repository, run = _awaiting_run(tmp_path)

    with pytest.raises(KeyError):
        repository.bind_image(run.id, "image-9", _stored_image(tmp_path))

    assert repository.quiz_review(run.id).requirements[0].image is None
    database.close()


def test_restart_recovery_leaves_awaiting_images_run_untouched(tmp_path):
    database, repository, run = _awaiting_run(tmp_path)

    assert repository.recover_interrupted_jobs() == 0

    waiting = repository.get_run(run.id)
    assert waiting.state is StudioRunState.AWAITING_IMAGES
    assert waiting.stage is StudioRunStage.IMAGE_REVIEW
    database.close()
