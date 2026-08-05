from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from oms_hub.db import Database
from oms_hub.models import PublishedQuizMediaModel, PublishedQuizModel, StudioRunModel
from oms_hub.study_generation.domain import (
    NativeQuiz,
    QuizChoice,
    QuizImageRef,
    QuizQuestion,
)
from oms_hub.study_generation.studio_domain import StudioSourceType, StudioStoredImage
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
