import pytest

from oms_hub.study_generation.native_quiz import grade_answer
from oms_hub.study_generation.practice_review import PracticeReviewService
from oms_hub.study_generation.repository import GenerationRepository
from tests.study_generation.test_gpt_lecture import gpt_review_run as gpt_review_run


def test_gpt_publication_requires_review_keeps_media_and_never_calls_paid_gate(gpt_review_run):
    repository, run, worker, client, inputs = gpt_review_run
    worker.run(run)
    review = PracticeReviewService(repository, lecture_validator=worker.validate_review)

    class PaidGate:
        def validate(self, quiz):
            raise AssertionError("paid fallback must not run")

    publisher = GenerationRepository(repository.database, PaidGate(), review)
    with pytest.raises(ValueError):
        publisher.publish_reviewed_studio_quiz(run.id)
    for question in review.review(run.id):
        review.verify_generated_answer(run.id, question.draft.question_id)
    published = publisher.publish_reviewed_studio_quiz(run.id)
    assert published.lecture_id == inputs.lecture_id
    assert published.studio_run_id == run.id
    assert publisher.publish_reviewed_studio_quiz(run.id).token == published.token
    media = publisher.published_quiz_media(published.token)
    assert media and all(image.path.is_file() for image in media)
    quiz = published.quiz
    question = quiz.questions[0]
    assert grade_answer(quiz, question.id, question.correct_choice_id).correct
