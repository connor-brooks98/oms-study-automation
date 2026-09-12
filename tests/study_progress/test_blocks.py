import math
from dataclasses import replace

import pytest

from oms_hub.study_progress.blocks import BlockCandidate, select_block

from .test_sessions import sessions  # noqa: F401


@pytest.fixture
def blocks(sessions):  # noqa: F811
    from oms_hub.models import PublishedQuizModel
    from oms_hub.study_generation.native_quiz import serialize_native_quiz
    from oms_hub.study_progress.blocks import BlockService

    service, bank, database, current = sessions
    first = current[0]
    second = replace(
        first,
        token="second",
        destination_exam_number=2,
        quiz=replace(
            first.quiz,
            title="Exam two",
            questions=(
                replace(first.quiz.questions[0], correct_choice_id="c2", stem="Second source"),
            ),
        ),
    )
    elsewhere = replace(
        first, token="other-course", destination_subject="Heme", destination_subject_key="heme"
    )
    publications = {p.token: p for p in (first, second, elsewhere)}
    with database.session() as session:
        for p in (second, elsewhere):
            session.add(
                PublishedQuizModel(
                    token=p.token,
                    title=p.title,
                    version=p.version,
                    payload_json=serialize_native_quiz(p.quiz),
                )
            )

    def load(owner, token):
        if owner != "owner":
            raise PermissionError
        return publications[token]

    service.load_quiz = load
    block_service = BlockService(
        sessions=service, bank=bank, publications_for=lambda owner: tuple(publications.values())
    )
    return block_service, bank, database, publications


def test_underpracticed_then_weak_and_no_duplicates():
    unseen = BlockCandidate("unseen", 0, None)
    weak = BlockCandidate("weak", 4, 0.25)
    strong = BlockCandidate("strong", 4, 1.0)
    assert select_block((strong, weak, unseen, weak), count=3) == ("unseen", "weak", "strong")


def test_selector_validates_and_rejects_conflicting_duplicate_counts():
    for count in (0, 501, True, 1.5):
        with pytest.raises(ValueError):
            select_block((), count=count)
    for attempts, accuracy in ((-1, None), (True, None), (1, -0.1), (1, 1.1), (1, math.nan)):
        with pytest.raises(ValueError):
            BlockCandidate("key", attempts, accuracy)
    with pytest.raises(ValueError, match="conflict"):
        select_block((BlockCandidate("same", 1, 0.0), BlockCandidate("same", 2, 0.5)), count=1)
    assert select_block(
        (BlockCandidate("b", 0, None), BlockCandidate("a", 0, None)), count=500
    ) == ("a", "b")


def test_cumulative_block_remaps_delivery_ids_and_resumes_same_source_questions(blocks):
    from oms_hub.study_progress.blocks import BlockFilters
    from oms_hub.study_progress.sessions import AnswerSelection, native_reference, question_key

    service, bank, _, publications = blocks
    scope = BlockFilters(course="neuro", exam_numbers=(1, 2), count=2)
    selected = service.preview(service.catalog("owner"), scope)
    assert len(selected) == 2 and {q.exam_number for q in selected} == {1, 2}
    view = service.create("owner", scope, selected_keys=tuple(q.key.question_id for q in selected))
    assert [q["id"] for q in view.questions] == ["q1", "q2"]
    assert len({q["attempt_id"] for q in view.questions}) == 2
    assert all("Neuro" in q["source_label"] for q in view.questions)
    assert "Secret objective" not in str(view.questions)
    for candidate, question in zip(selected, view.questions, strict=True):
        choice = "c1" if candidate.reference.quiz_token == "quiz" else "c2"
        receipt = service.sessions.answer(
            view.id, question["attempt_id"], AnswerSelection(choice_id=choice), owner_id="owner"
        )
        assert receipt.result == "correct"
    assert service.sessions.load(view.id, owner_id="owner").questions[0]["feedback"]["correct"]
    facts = bank.iter_attempts(learner_id="owner")
    assert {f.key for f in facts} == {q.key for q in selected}
    assert facts[0].key in {question_key(p, "q1") for p in publications.values()}
    ref = native_reference(publications["quiz"], "q1")
    with pytest.raises(ValueError):
        service.sessions.create_block("owner", (ref, ref))
    publications["second"] = replace(publications["second"], version=2)
    with pytest.raises(ValueError):
        service.sessions.load(view.id, owner_id="owner")


def test_scope_reviewed_topics_results_only_and_missing_media(blocks):
    import json

    from oms_hub.models import PublishedQuizModel
    from oms_hub.question_bank.contracts import QuestionKey, TopicLabel
    from oms_hub.question_bank.imports import preview_import
    from oms_hub.study_generation.domain import QuizImageRef
    from oms_hub.study_generation.native_quiz import serialize_native_quiz
    from oms_hub.study_progress.blocks import BlockFilters
    from oms_hub.study_progress.sessions import question_key

    service, bank, database, publications = blocks
    publication = publications["quiz"]
    key = question_key(publication, "q1")
    body = json.loads(serialize_native_quiz(publication.quiz))["questions"][0]
    preview = preview_import(
        json.dumps(
            {
                "schema_version": 1,
                "source": key.source,
                "product": key.product,
                "export_id": "fixture",
                "provenance": {
                    "kind": "authorized_question_export",
                    "description": "accepted native fixture",
                },
                "rows": [{"question_id": key.question_id, "question": body}],
            }
        ).encode()
    )
    bank.commit_import(preview, learner_id="owner", expected_digest=preview.digest)
    stored = bank.get_question(key)
    bank.set_topics(
        key,
        (
            TopicLabel(
                axis="topic",
                label="Reviewed",
                canonical_id="reviewed",
                method="user",
                review_state="accepted",
            ),
            TopicLabel(axis="topic", label="Pending", canonical_id="pending", method="model"),
        ),
        stored.content_hash,
        reviewer_context="owner",
    )
    bank.record_attempt(
        learner_id="owner",
        key=QuestionKey(source="uworld", product="step1", question_id="results-only"),
        attempt_id="vendor",
        result="incorrect",
        occurred_at=None,
        elapsed_ms=None,
    )
    catalog = service.catalog("owner")
    scope = BlockFilters(course="neuro", exam_numbers=(1, 2), topic_ids=("reviewed",), count=20)
    assert [q.key for q in service.preview(catalog, scope)] == [key]
    assert service.preview(catalog, scope.model_copy(update={"topic_ids": ("pending",)})) == ()
    assert service.preview(catalog, scope, exclude_keys=(key.question_id,)) == ()
    with pytest.raises(ValueError):
        service.create("owner", scope, selected_keys=("results-only",))
    assert service.catalog("other").questions == ()
    publication = replace(
        publication,
        quiz=replace(
            publication.quiz,
            questions=(
                replace(
                    publication.quiz.questions[0],
                    image_ref=QuizImageRef("missing", "slides", "p1", "image"),
                ),
            ),
        ),
    )
    publications["quiz"] = publication
    with database.session() as session:
        session.get(PublishedQuizModel, "quiz").payload_json = serialize_native_quiz(
            publication.quiz
        )
    catalog = service.catalog("owner")
    assert catalog.unavailable_publications == 1
    assert not any(q.reference.quiz_token == "quiz" for q in catalog.questions)
