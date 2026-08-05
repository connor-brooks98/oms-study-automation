from pathlib import Path
from typing import cast

import pytest

from oms_hub.db import Database
from oms_hub.document_processing.domain import SourceSnapshot
from oms_hub.files.atomic import verified_atomic_write
from oms_hub.study_generation.practice_domain import (
    ImportSourceRole,
    ImportSourceSelection,
    QuizContentKind,
    StudioSourcePurpose,
)
from oms_hub.study_generation.studio_domain import StudioSourceState
from oms_hub.study_generation.studio_repository import StudioRepository
from oms_hub.study_generation.studio_service import StudioService, URLSnapshotter


class Snapshotter:
    def __init__(self, root: Path, *, error: str | None = None) -> None:
        self.root = root
        self.error = error

    def fetch(self, source_id: str, title: str, url: str) -> SourceSnapshot:
        if self.error is not None:
            raise ValueError(self.error)
        path = self.root / source_id / "snapshot.html"
        digest = verified_atomic_write(b"<h1>Questions</h1>", path)
        return SourceSnapshot(source_id, title, path, "text/html", digest, url)


def _service(tmp_path: Path, *, snapshotter: Snapshotter | None = None) -> StudioService:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    return StudioService(
        StudioRepository(database),
        tmp_path / "sources",
        1024,
        url_snapshot_service=cast(URLSnapshotter, snapshotter) if snapshotter else None,
    )


def test_import_file_is_ready_locally_without_notebook_attachment(tmp_path: Path) -> None:
    service = _service(tmp_path)

    source = service.add_import_file(
        "Neuro", 1, "Questions", "questions.docx", b"PK fixture"
    )

    assert source.purpose is StudioSourcePurpose.LOCAL_IMPORT
    assert source.state is StudioSourceState.READY
    assert source.remote_source_id is None
    assert source.snapshot_sha256 is not None
    assert source.payload_path is not None
    assert source.payload_path.read_bytes() == b"PK fixture"
    assert service.repository.claim_next() is None


def test_import_url_snapshots_before_becoming_ready(tmp_path: Path) -> None:
    snapshotter = Snapshotter(tmp_path / "snapshots")
    service = _service(tmp_path, snapshotter=snapshotter)

    source = service.add_import_url(
        "Neuro", 1, "Questions", "https://example.test/questions"
    )

    assert source.state is StudioSourceState.READY
    assert source.final_url == "https://example.test/questions"
    assert source.media_type == "text/html"
    assert source.snapshot_sha256 is not None


def test_failed_import_url_never_becomes_ready(tmp_path: Path) -> None:
    service = _service(tmp_path, snapshotter=Snapshotter(tmp_path, error="download failed"))

    with pytest.raises(ValueError, match="download failed"):
        service.add_import_url("Neuro", 1, "Questions", "https://example.test/questions")

    source = service.repository.list_sources("Neuro", 1)[0]
    assert source.state is StudioSourceState.FAILED
    assert source.diagnostic_source == "source_processing"
    assert source.payload_path is None


def test_queue_import_run_rejects_invalid_source_selections(tmp_path: Path) -> None:
    service = _service(tmp_path)
    questions = service.add_import_text("Neuro", 1, "Questions", "What is this?")
    answers = service.add_import_text("Neuro", 1, "Answers", "It is this.")

    with pytest.raises(ValueError, match="duplicates"):
        service.queue_import_run(
            "Neuro",
            1,
            "Practice",
            "Neuro",
            1,
            QuizContentKind.PRACTICE_QUESTIONS,
            (
                ImportSourceSelection(questions.id, ImportSourceRole.QUESTIONS),
                ImportSourceSelection(questions.id, ImportSourceRole.ANSWER_KEY),
            ),
        )
    with pytest.raises(ValueError, match="Questions or Combined"):
        service.queue_import_run(
            "Neuro",
            1,
            "Practice",
            "Neuro",
            1,
            QuizContentKind.PRACTICE_QUESTIONS,
            (ImportSourceSelection(answers.id, ImportSourceRole.ANSWER_KEY),),
        )
    with pytest.raises(ValueError, match="only Supporting Reference or Combined"):
        service.queue_import_run(
            "Neuro",
            1,
            "Practice",
            "Neuro",
            1,
            QuizContentKind.PRACTICE_QUESTIONS,
            (ImportSourceSelection(questions.id, ImportSourceRole.QUESTIONS, True),),
        )
