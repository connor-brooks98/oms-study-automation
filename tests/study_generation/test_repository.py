import json
from datetime import UTC, datetime

from oms_hub.db import Database
from oms_hub.models import (
    PublishedQuizMediaModel,
    PublishedQuizModel,
    StudioRunModel,
    StudyRevisionModel,
    UploadBatchModel,
    UploadItemModel,
)
from oms_hub.repositories import CatalogRepository, LectureInput
from oms_hub.study_generation.domain import (
    GenerationKind,
    GenerationStage,
    GenerationState,
    PublishedQuizLibrarySection,
    PublishedQuizOrderDirection,
    SourceKind,
)
from oms_hub.study_generation.native_quiz import parse_native_quiz
from oms_hub.study_generation.practice_domain import QuizContentKind
from oms_hub.study_generation.repository import GenerationRepository


def prepared_repository(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    lecture_id = CatalogRepository(database).upsert_lecture(
        LectureInput("Neuro", 1, 1, "Seizures", "Dr Test", None)
    )
    return GenerationRepository(database), lecture_id


def _create_study_revision(
    repository: GenerationRepository,
    lecture_id: int,
    *,
    upload_item_id: str,
    source_sha256: str,
) -> int:
    with repository.database.session() as session:
        batch = UploadBatchModel(id=f"batch-{upload_item_id}", kind="slides")
        session.add(batch)
        session.flush()
        session.add(
            UploadItemModel(
                id=upload_item_id,
                batch_id=batch.id,
                kind="slides",
                original_filename=f"{upload_item_id}.pdf",
                staged_path=f"/tmp/{upload_item_id}.pdf",
                sha256=source_sha256,
                size_bytes=1,
                lecture_id=lecture_id,
            )
        )
        session.flush()
        revision = StudyRevisionModel(
            upload_item_id=upload_item_id,
            lecture_id=lecture_id,
            kind="slides",
            source_sha256=source_sha256,
            immutable_source_path=f"/tmp/{upload_item_id}-immutable.pdf",
            state="accepted",
            current=True,
        )
        session.add(revision)
        session.flush()
        return revision.id


def test_queue_reuses_active_job_but_separates_generation_kinds(tmp_path):
    repository, lecture_id = prepared_repository(tmp_path)

    first = repository.queue(lecture_id, GenerationKind.OUTLINE)
    second = repository.queue(lecture_id, GenerationKind.OUTLINE)
    quiz = repository.queue(lecture_id, GenerationKind.QUIZ)

    assert second.id == first.id
    assert quiz.id != first.id
    assert first.state is GenerationState.QUEUED
    assert first.stage is GenerationStage.VALIDATE


def test_claim_and_recovery_preserve_recorded_stage(tmp_path):
    repository, lecture_id = prepared_repository(tmp_path)
    queued = repository.queue(lecture_id, GenerationKind.QUIZ)

    claimed = repository.claim_next(datetime.now(UTC))
    assert claimed is not None
    assert claimed.id == queued.id
    repository.advance(claimed.id, GenerationStage.GEMINI)

    assert repository.recover_interrupted() == 1
    recovered = repository.get(claimed.id)
    assert recovered.state is GenerationState.QUEUED
    assert recovered.stage is GenerationStage.GEMINI


def test_recovery_requeues_paused_legacy_docs_job_with_published_url(tmp_path):
    repository, lecture_id = prepared_repository(tmp_path)
    job = repository.queue(lecture_id, GenerationKind.QUIZ)
    repository.advance(
        job.id,
        GenerationStage.DOCS,
        quiz_url="https://study.example.com/public/quizzes/" + "a" * 64,
    )
    repository.fail(job.id, "Google Docs authorization expired", paused=True)

    assert repository.recover_interrupted() == 1
    recovered = repository.get(job.id)
    assert recovered.state is GenerationState.QUEUED
    assert recovered.stage is GenerationStage.DOCS
    assert recovered.quiz_url is not None


def _quiz(title="Seizures"):
    return parse_native_quiz(
        json.dumps(
            {
                "title": title,
                "questions": [
                    {
                        "stem": "Which choice is correct?",
                        "choices": ["First", "Second"],
                        "correct_index": 0,
                        "rationale": "The first choice is correct.",
                    }
                ],
            }
        )
    )


def test_publish_keeps_token_and_increments_version_for_new_job(tmp_path):
    repository, lecture_id = prepared_repository(tmp_path)
    try:
        first_job = repository.queue(lecture_id, GenerationKind.QUIZ)

        first = repository.publish_quiz(lecture_id, first_job.id, _quiz())
        retried = repository.publish_quiz(lecture_id, first_job.id, _quiz())
        repository.complete(first_job.id)
        second_job = repository.queue(lecture_id, GenerationKind.QUIZ)
        regenerated = repository.publish_quiz(
            lecture_id,
            second_job.id,
            _quiz("Seizures Review"),
        )

        assert len(first.token) == 64
        assert retried.token == first.token
        assert retried.version == 1
        assert regenerated.token == first.token
        assert regenerated.version == 2
        assert regenerated.title == "Seizures Review"
        assert repository.published_quiz(first.token) == regenerated
    finally:
        repository.database.engine.dispose()


def test_studio_publication_preserves_the_run_content_kind(tmp_path):
    repository, _ = prepared_repository(tmp_path)
    with repository.database.session() as session:
        session.add(
            StudioRunModel(
                id="practice-publication-run",
                subject="Neuro",
                subject_key="neuro",
                exam_number=1,
                destination_subject="Neuro",
                destination_subject_key="neuro",
                destination_exam_number=1,
                label="Practice Questions",
                label_key="practice questions",
                prompt="",
                workflow_kind="direct_import",
                content_kind=QuizContentKind.PRACTICE_QUESTIONS.value,
                state="awaiting_review",
                stage="review",
            )
        )

    try:
        published = repository.publish_studio_quiz(
            "practice-publication-run",
            _quiz("Practice Questions"),
        )

        assert published.content_kind == QuizContentKind.PRACTICE_QUESTIONS
    finally:
        repository.database.engine.dispose()


def test_replacement_studio_publication_uses_the_successor_content_kind(tmp_path):
    repository, _ = prepared_repository(tmp_path)
    with repository.database.session() as session:
        session.add_all(
            [
                StudioRunModel(
                    id="exam-review-run",
                    subject="Neuro",
                    subject_key="neuro",
                    exam_number=1,
                    destination_subject="Neuro",
                    destination_subject_key="neuro",
                    destination_exam_number=1,
                    label="Review Set",
                    label_key="review set",
                    prompt="",
                    content_kind=QuizContentKind.EXAM_REVIEW.value,
                    state="awaiting_images",
                    stage="image_review",
                ),
                StudioRunModel(
                    id="practice-successor-run",
                    subject="Neuro",
                    subject_key="neuro",
                    exam_number=1,
                    destination_subject="Neuro",
                    destination_subject_key="neuro",
                    destination_exam_number=1,
                    label="Review Set",
                    label_key="review set",
                    prompt="",
                    workflow_kind="direct_import",
                    content_kind=QuizContentKind.PRACTICE_QUESTIONS.value,
                    state="awaiting_review",
                    stage="review",
                    supersedes_run_id="exam-review-run",
                ),
            ]
        )

    try:
        original = repository.publish_studio_quiz("exam-review-run", _quiz("Review Set"))
        replacement = repository.publish_studio_quiz(
            "practice-successor-run",
            _quiz("Practice Questions"),
        )

        assert replacement.token == original.token
        assert replacement.version == original.version + 1
        assert replacement.content_kind == QuizContentKind.PRACTICE_QUESTIONS
        assert repository.published_quizzes(
            frozenset({QuizContentKind.PRACTICE_QUESTIONS})
        ) == (replacement,)
    finally:
        repository.database.engine.dispose()


def test_unknown_public_quiz_token_returns_none(tmp_path):
    repository, _ = prepared_repository(tmp_path)

    try:
        assert repository.published_quiz("f" * 64) is None
    finally:
        repository.database.engine.dispose()


def test_published_quiz_management_preserves_content_and_orders_canonical_scope(tmp_path):
    repository, lecture_id = prepared_repository(tmp_path)
    catalog = CatalogRepository(repository.database)
    second_lecture_id = catalog.upsert_lecture(
        LectureInput("Neuro", 1, 2, "Cerebrovascular disease", "", None)
    )
    try:
        first = repository.publish_quiz(
            lecture_id,
            repository.queue(lecture_id, GenerationKind.QUIZ).id,
            _quiz("First title"),
        )
        second = repository.publish_quiz(
            second_lecture_id,
            repository.queue(second_lecture_id, GenerationKind.QUIZ).id,
            _quiz("Second title"),
        )

        renamed = repository.rename_published_quiz(first.token, "Renamed title")
        reordered = repository.reorder_published_quiz(
            first.token,
            PublishedQuizOrderDirection.DOWN,
        )
        moved = repository.move_published_quiz(
            first.token,
            PublishedQuizLibrarySection.PRACTICE_QUESTIONS,
        )

        assert renamed.token == first.token
        assert renamed.version == first.version
        assert renamed.quiz.title == "Renamed title"
        assert reordered.display_order == 2
        assert moved.content_kind == QuizContentKind.PRACTICE_QUESTIONS
        assert moved.display_order == 1
        assert tuple(
            row.token
            for row in repository.published_quizzes(
                frozenset({QuizContentKind.LECTURE_QUIZ})
            )
        ) == (second.token,)
        assert tuple(
            row.token
            for row in repository.published_quizzes(
                frozenset({QuizContentKind.PRACTICE_QUESTIONS})
            )
        ) == (moved.token,)
        with repository.database.session() as session:
            model = session.get(PublishedQuizModel, first.token)
            assert model is not None
            assert parse_native_quiz(model.payload_json).title == "Renamed title"
    finally:
        repository.database.engine.dispose()


def test_moving_middle_quiz_compacts_the_source_section_order(tmp_path):
    repository, first_lecture_id = prepared_repository(tmp_path)
    catalog = CatalogRepository(repository.database)
    second_lecture_id = catalog.upsert_lecture(
        LectureInput("Neuro", 1, 2, "Stroke", "", None)
    )
    third_lecture_id = catalog.upsert_lecture(
        LectureInput("Neuro", 1, 3, "Tumors", "", None)
    )
    try:
        first = repository.publish_quiz(
            first_lecture_id,
            repository.queue(first_lecture_id, GenerationKind.QUIZ).id,
            _quiz("First"),
        )
        middle = repository.publish_quiz(
            second_lecture_id,
            repository.queue(second_lecture_id, GenerationKind.QUIZ).id,
            _quiz("Middle"),
        )
        third = repository.publish_quiz(
            third_lecture_id,
            repository.queue(third_lecture_id, GenerationKind.QUIZ).id,
            _quiz("Third"),
        )

        repository.move_published_quiz(
            middle.token,
            PublishedQuizLibrarySection.PRACTICE_QUESTIONS,
        )

        with repository.database.session() as session:
            remaining = [
                session.get(PublishedQuizModel, token)
                for token in (first.token, third.token)
            ]
            assert [row.display_order for row in remaining if row is not None] == [1, 2]
    finally:
        repository.database.engine.dispose()


def test_quiz_management_preserves_media_and_restores_source_content_kinds(tmp_path):
    repository, lecture_id = prepared_repository(tmp_path)
    try:
        lecture = repository.publish_quiz(
            lecture_id,
            repository.queue(lecture_id, GenerationKind.QUIZ).id,
            _quiz("Lecture title"),
        )
        with repository.database.session() as session:
            session.add(
                PublishedQuizMediaModel(
                    quiz_token=lecture.token,
                    image_key="lecture-image",
                    path="/tmp/lecture-image.png",
                    sha256="a" * 64,
                    media_type="image/png",
                    width=100,
                    height=50,
                    alt_text="Lecture image",
                )
            )
        original_lecture_token = lecture.token
        original_lecture_version = lecture.version
        original_questions = lecture.quiz.questions
        lecture = repository.rename_published_quiz(lecture.token, "Edited lecture")
        repository.reorder_published_quiz(
            lecture.token,
            PublishedQuizOrderDirection.UP,
        )
        lecture = repository.move_published_quiz(
            lecture.token,
            PublishedQuizLibrarySection.PRACTICE_QUESTIONS,
        )
        lecture = repository.move_published_quiz(
            lecture.token,
            PublishedQuizLibrarySection.QUIZZES,
        )
        stored_lecture = repository.published_quiz(lecture.token)

        assert lecture.content_kind == QuizContentKind.LECTURE_QUIZ
        assert stored_lecture is not None
        assert lecture.token == original_lecture_token
        assert lecture.version == original_lecture_version
        assert lecture.quiz.questions == original_questions
        assert repository.published_quiz_media(lecture.token)[0].image_key == "lecture-image"

        run_id = "management-studio-run"
        with repository.database.session() as session:
            session.add(
                StudioRunModel(
                    id=run_id,
                    subject="Neuro",
                    subject_key="neuro",
                    exam_number=1,
                    destination_subject="Neuro",
                    destination_subject_key="neuro",
                    destination_exam_number=1,
                    label="Immutable run label",
                    label_key="immutable run label",
                    prompt="",
                    state="awaiting_images",
                    stage="image_review",
                )
            )
        studio = repository.publish_studio_quiz(run_id, _quiz("Studio title"))
        original_studio_token = studio.token
        original_studio_version = studio.version
        original_studio_questions = studio.quiz.questions
        studio = repository.move_published_quiz(
            studio.token,
            PublishedQuizLibrarySection.PRACTICE_QUESTIONS,
        )
        studio = repository.move_published_quiz(
            studio.token,
            PublishedQuizLibrarySection.QUIZZES,
        )

        assert studio.content_kind == QuizContentKind.EXAM_REVIEW
        assert studio.token == original_studio_token
        assert studio.version == original_studio_version
        assert studio.quiz.questions == original_studio_questions
    finally:
        repository.database.engine.dispose()


def test_reordering_uses_current_lecture_subject_and_exam_scope(tmp_path):
    repository, lecture_id = prepared_repository(tmp_path)
    catalog = CatalogRepository(repository.database)
    try:
        moved = repository.publish_quiz(
            lecture_id,
            repository.queue(lecture_id, GenerationKind.QUIZ).id,
            _quiz("Moved lecture"),
        )
        catalog.update_lecture(
            lecture_id,
            LectureInput("Cardio", 2, 1, "Arrhythmias", "", None),
        )
        peer_lecture_id = catalog.upsert_lecture(
            LectureInput("Cardio", 2, 2, "Heart failure", "", None)
        )
        peer = repository.publish_quiz(
            peer_lecture_id,
            repository.queue(peer_lecture_id, GenerationKind.QUIZ).id,
            _quiz("Peer lecture"),
        )

        reordered = repository.reorder_published_quiz(
            moved.token,
            PublishedQuizOrderDirection.DOWN,
        )
        stored_peer = repository.published_quiz(peer.token)

        assert reordered.display_order == 2
        assert stored_peer is not None
        assert stored_peer.display_order == 1
        with repository.database.session() as session:
            stale = session.get(PublishedQuizModel, moved.token)
            assert stale is not None
            assert stale.destination_subject == "Neuro"
            assert stale.destination_exam_number == 1
    finally:
        repository.database.engine.dispose()


def test_unpublish_lecture_preserves_publication_history_and_can_republish(tmp_path):
    repository, lecture_id = prepared_repository(tmp_path)
    try:
        job = repository.queue(lecture_id, GenerationKind.QUIZ)
        published = repository.publish_quiz(lecture_id, job.id, _quiz())

        assert repository.unpublish_quiz(published.token) == published.token
        assert repository.published_quiz(published.token) is None
        with repository.database.session() as session:
            inactive = session.get(PublishedQuizModel, published.token)
            assert inactive is not None
            assert inactive.active is False

        later_job = repository.queue(lecture_id, GenerationKind.QUIZ)
        republished = repository.publish_quiz(lecture_id, later_job.id, _quiz())

        assert repository.published_quiz(republished.token) == republished
        with repository.database.session() as session:
            assert session.get(PublishedQuizModel, published.token) is not None
    finally:
        repository.database.engine.dispose()


def test_unpublish_studio_preserves_run_and_clears_matching_token(tmp_path):
    repository, _ = prepared_repository(tmp_path)
    run_id = "unpublish-studio-run"
    with repository.database.session() as session:
        session.add(
            StudioRunModel(
                id=run_id,
                subject="Neuro",
                subject_key="neuro",
                exam_number=1,
                destination_subject="Neuro",
                destination_subject_key="neuro",
                destination_exam_number=1,
                label="Review Set",
                label_key="review set",
                prompt="",
                state="awaiting_images",
                stage="image_review",
            )
        )
    try:
        published = repository.publish_studio_quiz(run_id, _quiz("Review Set"))
        with repository.database.session() as session:
            session.get(StudioRunModel, run_id).published_token = published.token

        assert repository.unpublish_quiz(published.token) == published.token
        assert repository.published_quiz(published.token) is None
        with repository.database.session() as session:
            inactive = session.get(PublishedQuizModel, published.token)
            run = session.get(StudioRunModel, run_id)
            assert inactive is not None
            assert inactive.active is False
            assert run is not None
            assert run.published_token is None

        republished = repository.publish_studio_quiz(run_id, _quiz("Review Set"))

        assert repository.published_quiz(republished.token) == republished
    finally:
        repository.database.engine.dispose()


def test_binding_new_revision_supersedes_prior_ready_source(tmp_path):
    repository, lecture_id = prepared_repository(tmp_path)
    notebook = repository.save_notebook_mapping(
        "Neuro",
        "neuro",
        1,
        "nb-1",
        "Neuro · Exam 1",
    )

    old_revision_id = _create_study_revision(
        repository,
        lecture_id,
        upload_item_id="revision-old",
        source_sha256="a" * 64,
    )
    new_revision_id = _create_study_revision(
        repository,
        lecture_id,
        upload_item_id="revision-new",
        source_sha256="b" * 64,
    )

    first = repository.bind_source(
        notebook.id,
        lecture_id,
        revision_id=old_revision_id,
        source_kind=SourceKind.LECTURE_PDF,
        source_sha256="a" * 64,
        remote_source_id="remote-old",
        display_title="Lecture 01 - Seizures",
    )
    second = repository.bind_source(
        notebook.id,
        lecture_id,
        revision_id=new_revision_id,
        source_kind=SourceKind.LECTURE_PDF,
        source_sha256="b" * 64,
        remote_source_id="remote-new",
        display_title="Lecture 01 - Seizures",
    )

    assert first.remote_source_id == "remote-old"
    assert repository.source_binding(
        notebook.id,
        lecture_id,
        SourceKind.LECTURE_PDF,
    ) == second


def test_notebook_mapping_is_upserted_by_course_and_exam(tmp_path):
    repository, _ = prepared_repository(tmp_path)

    first = repository.save_notebook_mapping(
        "Neuro",
        "neuro",
        1,
        "nb-old",
        "Neuro · Exam 1",
    )
    second = repository.save_notebook_mapping(
        "Neuro",
        "neuro",
        1,
        "nb-new",
        "Neuro · Exam 1",
    )

    assert second.id == first.id
    assert second.remote_notebook_id == "nb-new"
    assert repository.notebook_mapping("neuro", 1) == second
    assert repository.notebook_mapping_by_remote_id("nb-new") == second
