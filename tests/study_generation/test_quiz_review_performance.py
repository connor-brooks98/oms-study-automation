import json
from dataclasses import replace

import pytest

from oms_hub.study_generation import gpt_lecture
from oms_hub.study_generation.practice_review import PracticeReviewService
from tests.study_generation.test_gpt_lecture import (
    _inputs,
    _quiz_payload,
    gpt_review_run,  # noqa: F401 - shared offline fixture
)


@pytest.mark.parametrize("question_count", [3, 15])
def test_draft_conversion_validates_complete_sources_once_for_all_images(
    tmp_path, monkeypatch, question_count,
):
    inputs = _inputs(tmp_path)
    payload = _quiz_payload(inputs)
    template = payload["questions"][0]
    payload["questions"] = [dict(
        template, id=f"q-{index}", stem=f"Independent patient case {index}.",
        objective_ids=[key for key, _ in inputs.objectives],
    ) for index in range(question_count)]
    quiz = gpt_lecture.GeneratedLectureQuiz.model_validate(payload)
    validations = []
    validate = gpt_lecture.validate_lecture_inputs
    normalizations = []
    sanitize = gpt_lecture.sanitize_quiz_image

    def counted(current):
        validations.append(current)
        validate(current)

    def counted_sanitize(payload):
        normalizations.append(len(payload))
        return sanitize(payload)

    monkeypatch.setattr(gpt_lecture, "validate_lecture_inputs", counted)
    monkeypatch.setattr(gpt_lecture, "sanitize_quiz_image", counted_sanitize)
    drafts = gpt_lecture.to_review_drafts(quiz, inputs)
    assert len(drafts) == question_count and all(draft.image_ref for draft in drafts)
    assert validations == [inputs]
    assert len(normalizations) == 1
    # No validation result survives into a later request using these same objects.
    inputs.documents[0].assets[0].path.write_bytes(b"changed source image")
    with pytest.raises(ValueError, match="hash"):
        gpt_lecture.to_review_drafts(quiz, inputs)
    assert validations == [inputs, inputs]


@pytest.mark.parametrize("image_count", [1, 15])
def test_review_full_validation_work_does_not_grow_with_image_questions(
    gpt_review_run, monkeypatch, image_count,  # noqa: F811 - shared pytest fixture
):
    repository, run, worker, client, inputs = gpt_review_run
    generate = client.generate

    def fifteen_questions(request, **kwargs):
        result = generate(request, **kwargs)
        payload = json.loads(result.text)
        template = payload["questions"][0]
        payload["questions"] = [dict(
            template, id=f"q-{index}", stem=f"Independent patient case {index}.",
            objective_ids=[key for key, _ in inputs.objectives],
            image=template["image"] if index < image_count else None,
        ) for index in range(15)]
        return replace(result, text=json.dumps(payload))

    client.generate = fifteen_questions
    worker.run(run)
    assert repository.get_run(run.id).state.value == "awaiting_review"
    review = PracticeReviewService(repository, worker.image_service,
                                   lecture_validator=worker.validate_review)
    questions = review.review(run.id)
    assert sum(question.chosen_image is not None for question in questions) == image_count
    validations = []
    validate = gpt_lecture.validate_lecture_inputs
    normalizations = []
    sanitize = gpt_lecture.sanitize_quiz_image

    def counted(current):
        validations.append(current)
        validate(current)

    def counted_sanitize(payload):
        normalizations.append(len(payload))
        return sanitize(payload)

    monkeypatch.setattr(gpt_lecture, "validate_lecture_inputs", counted)
    monkeypatch.setattr(gpt_lecture, "sanitize_quiz_image", counted_sanitize)
    worker.validate_review(run.id, questions)
    # Existing manifest/coverage boundaries perform nine complete checks. The
    # image-question loop must not add another whole-deck check per question.
    assert 1 <= len(validations) <= 9
    assert len(normalizations) == 1
    assert gpt_lecture._review_verified_images.get() is None
    worker.validate_review(run.id, questions)
    assert len(normalizations) == 2
    completed_checks = len(validations)
    inputs.documents[0].assets[0].path.write_bytes(b"changed source image after review")
    with pytest.raises(ValueError, match="hash"):
        worker.validate_review(run.id, questions)
    assert len(validations) > completed_checks
    assert gpt_lecture._review_verified_images.get() is None


def test_review_rechecks_hash_after_normalization_within_the_same_request(
    gpt_review_run, monkeypatch,  # noqa: F811 - shared pytest fixture
):
    repository, run, worker, client, inputs = gpt_review_run
    worker.run(run)
    questions = PracticeReviewService(repository).review(run.id)
    sanitize = gpt_lecture.sanitize_quiz_image

    def mutate_after_normalization(payload):
        image = sanitize(payload)
        inputs.documents[0].assets[0].path.write_bytes(b"changed during this review request")
        return image

    monkeypatch.setattr(gpt_lecture, "sanitize_quiz_image", mutate_after_normalization)
    with pytest.raises(ValueError, match="hash"):
        worker.validate_review(run.id, questions)
    assert gpt_lecture._review_verified_images.get() is None


def test_standalone_source_asset_keeps_fresh_full_validation(tmp_path):
    inputs = _inputs(tmp_path)
    assert gpt_lecture.source_asset(inputs, inputs.slide_source_id, "figure-1") == (
        inputs.documents[0].assets[0]
    )
    inputs.bindings[1].snapshot.path.write_text("changed transcript source")
    with pytest.raises(ValueError, match="hash"):
        gpt_lecture.source_asset(inputs, inputs.slide_source_id, "figure-1")
