from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from oms_hub.db import Database
from oms_hub.files.atomic import verified_atomic_write
from oms_hub.models import (
    PublishedQuizMediaModel,
    PublishedQuizModel,
    StudioRunModel,
    StudioSourceModel,
    StudioSourceOperationModel,
)
from oms_hub.study_generation.domain import (
    NativeQuiz,
    QuizChoice,
    QuizImageRef,
    QuizQuestion,
)
from oms_hub.study_generation.practice_domain import (
    AnswerProvenance,
    DiagnosticSeverity,
    DraftDiagnostic,
    ImportSourceRole,
    ImportSourceSelection,
    QuestionDraft,
    QuestionSourceRef,
    QuizContentKind,
    StudioSourcePurpose,
)
from oms_hub.study_generation.studio_domain import (
    StudioSourceState,
    StudioSourceType,
    StudioStoredImage,
)
from oms_hub.study_generation.studio_repository import StudioRepository

_OPEN_DATABASES: list[Database] = []


@pytest.fixture(autouse=True)
def _close_databases() -> None:
    yield
    while _OPEN_DATABASES:
        _OPEN_DATABASES.pop().close()


def _repository(tmp_path: Path) -> StudioRepository:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    _OPEN_DATABASES.append(database)
    database.migrate()
    return StudioRepository(database)


def _quiz_with_image(image_key: str = "img-1") -> NativeQuiz:
    return NativeQuiz(
        "Quiz title",
        (
            QuizQuestion(
                "q1",
                "Which finding is shown?",
                (QuizChoice("c1", "A"), QuizChoice("c2", "B")),
                "c1",
                "Because.",
                image_ref=QuizImageRef(image_key, "Source", "p1", "desc"),
            ),
        ),
    )


def _queued_run(repository: StudioRepository, *, label: str = "Practice Quiz"):
    source = repository.create_source(
        "Neuro",
        1,
        StudioSourceType.TEXT,
        "Lecture notes",
    )
    repository.complete(source.id, "notebook-1", "remote-source-1")
    return repository.queue_run(
        "Neuro",
        1,
        "Draft a quiz.",
        [source.id],
        label,
        "Neuro",
        1,
    )


def _stored_image(tmp_path: Path, name: str) -> StudioStoredImage:
    path = tmp_path / name
    path.write_bytes(b"fake-image-bytes")
    return StudioStoredImage(
        path=path,
        sha256="a" * 64,
        media_type="image/png",
        width=100,
        height=100,
        original_filename=name,
    )


def _ready_local_source(repository: StudioRepository, title: str):
    source = repository.create_source(
        "Neuro",
        1,
        StudioSourceType.FILE,
        title,
        purpose=StudioSourcePurpose.LOCAL_IMPORT,
    )
    with repository.database.session() as session:
        stored = session.get(StudioSourceModel, source.id)
        assert stored is not None
        stored.state = StudioSourceState.READY.value
    return repository.get(source.id)


def _pending_local_source(repository: StudioRepository, title: str):
    return repository.create_source(
        "Neuro",
        1,
        StudioSourceType.FILE,
        title,
        purpose=StudioSourcePurpose.LOCAL_IMPORT,
    )


@pytest.mark.parametrize("transition", ["failed", "deleted"])
def test_mark_import_ready_cannot_resurrect_changed_source_state(
    tmp_path: Path,
    transition: str,
) -> None:
    repository = _repository(tmp_path)
    source = _pending_local_source(repository, "Questions")
    path = tmp_path / "snapshot.txt"
    digest = verified_atomic_write(b"Question", path)
    if transition == "failed":
        repository.fail(source.id, "source_processing", "download failed", retry=False)
    else:
        repository.mark_source_deleted(source.id)

    with pytest.raises(ValueError, match="no longer pending"):
        repository.mark_import_ready(
            source.id, path, digest, media_type="text/plain"
        )

    stored = repository.get(source.id)
    assert stored is not None
    assert stored.state.value == transition
    assert stored.payload_path is None
    assert stored.snapshot_sha256 is None


def test_mark_import_ready_cannot_replace_an_existing_snapshot(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    source = _pending_local_source(repository, "Questions")
    first = tmp_path / "first.txt"
    first_digest = verified_atomic_write(b"first", first)
    ready = repository.mark_import_ready(
        source.id, first, first_digest, media_type="text/plain"
    )
    second = tmp_path / "second.txt"
    second_digest = verified_atomic_write(b"second", second)

    with pytest.raises(ValueError, match="no longer pending"):
        repository.mark_import_ready(
            source.id, second, second_digest, media_type="text/plain"
        )

    stored = repository.get(source.id)
    assert stored == ready


def test_import_run_persists_ordered_source_roles_and_stage_artifact(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    questions = _ready_local_source(repository, "Questions")
    answers = _ready_local_source(repository, "Answers")
    assert questions is not None
    assert answers is not None

    run = repository.queue_import_run(
        "Neuro",
        1,
        "Exam review",
        "Neuro",
        1,
        QuizContentKind.PRACTICE_QUESTIONS,
        (
            ImportSourceSelection(questions.id, ImportSourceRole.QUESTIONS),
            ImportSourceSelection(answers.id, ImportSourceRole.ANSWER_KEY),
        ),
    )
    repository.save_run_artifact(run.id, f"parse:{questions.id}", "a" * 64, "{}")
    repository.save_run_artifact(run.id, f"parse:{questions.id}", "b" * 64, "{\"v\": 2}")

    assert [source.role for source in repository.import_sources(run.id)] == [
        ImportSourceRole.QUESTIONS,
        ImportSourceRole.ANSWER_KEY,
    ]
    with repository.database.session() as session:
        artifact = session.execute(
            text(
                "SELECT signature_sha256, payload_json FROM studio_run_artifacts "
                "WHERE run_id = :run_id"
            ),
            {"run_id": run.id},
        ).one()
    assert artifact == ("b" * 64, '{"v": 2}')


def test_rerun_import_clones_import_bindings_and_can_replace_published_predecessor(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    questions = _ready_local_source(repository, "Questions")
    supporting = _ready_local_source(repository, "Reference")
    assert questions is not None and supporting is not None
    original = repository.queue_import_run(
        "Neuro",
        1,
        "Exam review",
        "Neuro",
        1,
        QuizContentKind.PRACTICE_QUESTIONS,
        (
            ImportSourceSelection(questions.id, ImportSourceRole.QUESTIONS),
            ImportSourceSelection(
                supporting.id, ImportSourceRole.SUPPORTING_REFERENCE, attach_to_notebook=True
            ),
        ),
    )
    with repository.database.session() as session:
        previous = session.get(StudioRunModel, original.id)
        assert previous is not None
        previous.state = "complete"
        previous.published_token = "published-token"
        session.add(
            PublishedQuizModel(
                token="published-token",
                title="Exam review",
                payload_json="{}",
                studio_run_id=original.id,
                destination_subject="Neuro",
                destination_subject_key="neuro",
                destination_exam_number=1,
                label="Exam review",
                label_key="exam review",
                active=True,
            )
        )

    successor = repository.rerun(original.id)

    assert successor.workflow_kind.value == "direct_import"
    assert successor.content_kind is QuizContentKind.PRACTICE_QUESTIONS
    assert successor.prompt == ""
    assert successor.supersedes_run_id == original.id
    bindings = repository.import_sources(successor.id)
    assert [
        (binding.source_id, binding.role, binding.attach_to_notebook)
        for binding in bindings
    ] == [
        (questions.id, ImportSourceRole.QUESTIONS, False),
        (supporting.id, ImportSourceRole.SUPPORTING_REFERENCE, True),
    ]
    with repository.database.session() as session:
        published = session.get(PublishedQuizModel, "published-token")
        assert published is not None and published.active


def test_remove_run_from_history_hides_it_without_removing_its_publication(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    run = _queued_run(repository)
    with repository.database.session() as session:
        stored = session.get(StudioRunModel, run.id)
        assert stored is not None
        stored.state = "complete"
        stored.published_token = "published-token"
        session.add(
            PublishedQuizModel(
                token="published-token",
                title="Practice Quiz",
                payload_json="{}",
                studio_run_id=run.id,
                destination_subject="Neuro",
                destination_subject_key="neuro",
                destination_exam_number=1,
                label="Practice Quiz",
                label_key="practice quiz",
                active=True,
            )
        )

    repository.hide_run(run.id)

    assert all(item.id != run.id for item in repository.list_runs())
    with repository.database.session() as session:
        published = session.get(PublishedQuizModel, "published-token")
        assert published is not None and published.active


def test_save_question_reviews_replaces_prior_provenance_for_the_run(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    source = _ready_local_source(repository, "Questions")
    assert source is not None
    run = repository.queue_import_run(
        "Neuro",
        1,
        "Exam review",
        "Neuro",
        1,
        QuizContentKind.PRACTICE_QUESTIONS,
        (ImportSourceSelection(source.id, ImportSourceRole.QUESTIONS),),
    )
    initial = QuestionDraft(
        "q1",
        "1",
        "What is the diagnosis?",
        ("A", "B"),
        0,
        "Because.",
        None,
        (QuestionSourceRef(source.id, "s1", "p1"),),
        AnswerProvenance.GENERATED_BY_AI,
        0.5,
        (DraftDiagnostic("needs-review", "Verify answer", DiagnosticSeverity.BLOCKER),),
        True,
        None,
    )
    corrected = QuestionDraft(
        "q1",
        "1",
        "What is the diagnosis?",
        ("A", "B"),
        0,
        "Because.",
        None,
        (QuestionSourceRef(source.id, "s1", "p1"),),
        AnswerProvenance.MANUALLY_CORRECTED,
        1.0,
        (),
        False,
        "2026-08-05T12:00:00+00:00",
    )

    repository.save_question_reviews(run.id, (initial,))
    repository.save_question_reviews(run.id, (corrected,))

    with repository.database.session() as session:
        review = session.execute(
            text(
                "SELECT answer_provenance, verification_required, diagnostics_json "
                "FROM studio_question_reviews WHERE run_id = :run_id AND question_id = 'q1'"
            ),
            {"run_id": run.id},
        ).one()
    assert review == ("manually_corrected", 0, "[]")


def test_await_image_review_reentry_deletes_orphaned_asset(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    run = _queued_run(repository)
    quiz = _quiz_with_image()

    repository.await_image_review(run.id, "notebook-1", "raw", quiz)
    image = _stored_image(tmp_path, "orphan.png")
    repository.bind_image(run.id, "img-1", image)
    assert image.path.is_file()

    # Re-entry replaces the requirement rows for the same run; the
    # previously bound asset is no longer referenced by anything.
    repository.await_image_review(run.id, "notebook-1", "raw again", quiz)

    assert not image.path.exists()


def test_await_image_review_reentry_preserves_published_asset(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    run = _queued_run(repository)
    quiz = _quiz_with_image()

    repository.await_image_review(run.id, "notebook-1", "raw", quiz)
    image = _stored_image(tmp_path, "published.png")
    repository.bind_image(run.id, "img-1", image)

    with repository.database.session() as session:
        session.add(
            PublishedQuizModel(
                token="tok-1",
                title="Published quiz",
                payload_json="{}",
            )
        )
        session.flush()
        session.add(
            PublishedQuizMediaModel(
                quiz_token="tok-1",
                image_key="img-1",
                path=str(image.path),
                sha256=image.sha256,
                media_type=image.media_type,
                width=image.width,
                height=image.height,
                alt_text="desc",
            )
        )

    repository.await_image_review(run.id, "notebook-1", "raw again", quiz)

    assert image.path.is_file()


def test_list_runs_batches_source_lookups_without_mixing_runs(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    first = _queued_run(repository, label="First Quiz")
    second = _queued_run(repository, label="Second Quiz")

    runs = {run.id: run for run in repository.list_runs("Neuro", 1)}

    assert set(runs) == {first.id, second.id}
    assert [source.source_id for source in runs[first.id].sources] == [
        source.source_id for source in first.sources
    ]
    assert [source.source_id for source in runs[second.id].sources] == [
        source.source_id for source in second.sources
    ]
    # Each run was created with its own freshly-created source, so a
    # regression that mixed the batched sources across runs would surface
    # here as identical (or empty) source lists.
    assert runs[first.id].sources[0].source_id != runs[second.id].sources[0].source_id


def test_list_runs_honors_limit(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _queued_run(repository, label="First Quiz")
    _queued_run(repository, label="Second Quiz")
    _queued_run(repository, label="Third Quiz")

    assert len(repository.list_runs("Neuro", 1, limit=2)) == 2
    assert len(repository.list_runs("Neuro", 1, limit=50)) == 3


def test_queue_run_rejects_conflicting_active_label_past_the_precheck(
    tmp_path: Path,
) -> None:
    """Guards the race the in-memory pre-check in queue_run cannot see.

    ``supersedes_run_id`` intentionally skips the active-label pre-check (it
    is meant to replace a specific prior run), so it is also the path that
    exercises the partial unique index / IntegrityError translation when two
    inserts target the same active destination+label concurrently.
    """
    repository = _repository(tmp_path)
    held = _queued_run(repository, label="Practice Quiz")
    other = _queued_run(repository, label="Other Quiz")
    other_source_id = other.sources[0].source_id

    with pytest.raises(ValueError, match="already in use"):
        repository.queue_run(
            "Neuro",
            1,
            "Draft a quiz.",
            [other_source_id],
            "Practice Quiz",
            "Neuro",
            1,
            supersedes_run_id=other.id,
        )

    # The original active run for the label is untouched.
    assert repository.get_run(held.id).label == "Practice Quiz"


def test_queue_run_does_not_mislabel_a_genuine_fk_violation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supersedes_run_id that vanishes between the pre-check and the
    flush (a TOCTOU race the in-memory pre-check cannot close) trips the
    ``supersedes_run_id`` foreign key, not the active-label unique index.
    That must surface as the underlying IntegrityError, not get
    mislabeled as the (unrelated) "label already in use" ValueError.
    """
    repository = _repository(tmp_path)
    held = _queued_run(repository, label="Practice Quiz")
    held_source_id = held.sources[0].source_id

    original_get = OrmSession.get

    def _fake_get(
        self: OrmSession, entity: object, ident: object, *args: object, **kwargs: object
    ) -> object:
        if entity is StudioRunModel and ident == "ghost-run":
            # Pretend the run still exists so the in-memory pre-check
            # passes, simulating a delete that lands after the check but
            # before the flush.
            return held
        return original_get(self, entity, ident, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(OrmSession, "get", _fake_get)

    with pytest.raises(IntegrityError) as excinfo:
        repository.queue_run(
            "Neuro",
            1,
            "Draft a quiz.",
            [held_source_id],
            "Brand New Label",
            "Neuro",
            1,
            supersedes_run_id="ghost-run",
        )

    assert "already in use" not in str(excinfo.value)


def _claim_add_operation(
    repository: StudioRepository,
    source_id: str,
):
    claimed_source = repository.claim_next()
    assert claimed_source is not None
    assert claimed_source.id == source_id
    claimed = repository.claim_next_source_operation()
    assert claimed is not None
    return claimed


def test_delayed_source_operation_does_not_starve_later_work(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    first = repository.create_source(
        "Neuro", 1, StudioSourceType.TEXT, "First source"
    )
    operation, _ = _claim_add_operation(repository, first.id)
    repository.record_attach_baseline(operation.id, "notebook-1", set())
    repository.mark_attach_reconciling(operation.id, "notebook", "list unavailable")

    delayed = repository.get(first.id)
    assert delayed is not None
    assert delayed.next_attempt_at is not None
    assert repository.claim_next_source_operation(now=datetime.now(UTC)) is None

    second = repository.create_source(
        "Neuro", 1, StudioSourceType.TEXT, "Second source"
    )
    assert repository.claim_next() is not None
    later_operation = repository.claim_next_source_operation(now=datetime.now(UTC))
    assert later_operation is not None
    assert later_operation[1].id == second.id

    later_run = _queued_run(repository, label="Later queued run")
    claimed_run = repository.claim_next_run()
    assert claimed_run is not None
    assert claimed_run.id == later_run.id


def test_persistent_add_reconciliation_needs_review_after_bounded_attempts(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    source = repository.create_source("Neuro", 1, StudioSourceType.TEXT, "Source")
    operation, _ = _claim_add_operation(repository, source.id)
    repository.record_attach_baseline(operation.id, "notebook-1", set())

    future = datetime.now(UTC) + timedelta(minutes=1)
    for _ in range(2):
        repository.mark_attach_reconciling(
            operation.id,
            "notebook",
            "list unavailable",
            during_reconciliation=True,
        )
        assert repository.claim_next_source_operation(now=datetime.now(UTC)) is None
        claimed = repository.claim_next_source_operation(now=future)
        assert claimed is not None
        operation, _ = claimed

    repository.mark_attach_reconciling(
        operation.id,
        "notebook",
        "list unavailable",
        during_reconciliation=True,
    )

    stored = repository.get(source.id)
    assert stored is not None
    assert stored.state is StudioSourceState.NEEDS_REVIEW
    assert stored.next_attempt_at is None
    with repository.database.session() as session:
        stored_operation = session.get(StudioSourceOperationModel, operation.id)
        assert stored_operation is not None
        assert stored_operation.state == "needs_review"
    assert repository.claim_next_source_operation(now=future + timedelta(days=1)) is None
    later_run = _queued_run(repository, label="Run after terminal add reconciliation")
    claimed_run = repository.claim_next_run()
    assert claimed_run is not None
    assert claimed_run.id == later_run.id


def test_persistent_delete_failures_become_needs_review_without_starving_runs(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    source = repository.create_source("Neuro", 1, StudioSourceType.TEXT, "Attached")
    repository.complete(source.id, "notebook-1", "remote-1")
    repository.queue_source_delete(source.id)

    operation_claim = repository.claim_next_source_operation()
    assert operation_claim is not None
    operation, _ = operation_claim
    repository.retry_delete_operation(operation.id, "notebook", "delete unavailable")
    assert repository.claim_next_source_operation(now=datetime.now(UTC)) is None

    later_run = _queued_run(repository, label="Run after delayed delete")
    claimed_run = repository.claim_next_run()
    assert claimed_run is not None
    assert claimed_run.id == later_run.id

    future = datetime.now(UTC) + timedelta(minutes=1)
    for _ in range(2):
        claimed = repository.claim_next_source_operation(now=future)
        assert claimed is not None
        operation, _ = claimed
        repository.retry_delete_operation(operation.id, "notebook", "delete unavailable")

    stored = repository.get(source.id)
    assert stored is not None
    assert stored.state is StudioSourceState.NEEDS_REVIEW
    assert stored.next_attempt_at is None
    with repository.database.session() as session:
        stored_operation = session.get(StudioSourceOperationModel, operation.id)
        assert stored_operation is not None
        assert stored_operation.state == "needs_review"
    assert repository.claim_next_source_operation(now=future + timedelta(days=1)) is None


def test_recovery_preserves_queued_and_reconciling_operation_backoff(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    queued_source = repository.create_source(
        "Neuro", 1, StudioSourceType.TEXT, "Queued source"
    )
    assert repository.claim_next() is not None
    queued_retry_at = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    with repository.database.session() as session:
        stored = session.get(StudioSourceModel, queued_source.id)
        assert stored is not None
        stored.next_attempt_at = queued_retry_at

    reconciling_source = repository.create_source(
        "Neuro", 1, StudioSourceType.TEXT, "Reconciling source"
    )
    assert repository.claim_next() is not None
    claimed = repository.claim_next_source_operation()
    assert claimed is not None
    operation, source = claimed
    assert source.id == reconciling_source.id
    repository.record_attach_baseline(operation.id, "notebook-1", set())
    repository.mark_attach_reconciling(operation.id, "notebook", "list unavailable")

    before_recovery = repository.get(reconciling_source.id)
    assert before_recovery is not None
    assert before_recovery.next_attempt_at is not None
    repository.recover_interrupted_jobs()

    queued_after = repository.get(queued_source.id)
    reconciling_after = repository.get(reconciling_source.id)
    assert queued_after is not None
    assert reconciling_after is not None
    assert queued_after.state is StudioSourceState.ATTACHING
    assert queued_after.next_attempt_at == queued_retry_at
    assert reconciling_after.state is StudioSourceState.ATTACHING
    assert reconciling_after.next_attempt_at == before_recovery.next_attempt_at
    with repository.database.session() as session:
        states = dict(
            session.execute(
                text(
                    "SELECT source_id, state FROM studio_source_operations "
                    "WHERE source_id IN (:queued_source_id, :reconciling_source_id)"
                ),
                {
                    "queued_source_id": queued_source.id,
                    "reconciling_source_id": reconciling_source.id,
                },
            ).all()
        )
    assert states == {
        queued_source.id: "queued",
        reconciling_source.id: "reconciling",
    }
