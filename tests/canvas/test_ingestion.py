from pathlib import Path

import pytest

from oms_hub.canvas.classifier import classify_attachment
from oms_hub.canvas.domain import CatalogMatch, CourseMappingInput
from oms_hub.canvas.ingestion import IngestionService
from oms_hub.canvas.repository import CanvasRepository
from oms_hub.config import Settings
from tests.canvas.test_classifier import attachment


def prepared(database, tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    settings = Settings(
        _env_file=None,
        canvas_inbox=inbox,
        revision_root=tmp_path / "revisions",
        max_ingest_bytes=1024 * 1024,
    )
    repository = CanvasRepository(database)
    repository.replace_course_mappings(
        [CourseMappingInput("751", "Hematology & Lymph", "HEME", "Heme/Lymph")]
    )
    value = attachment("Anemia.pptx", size=8)
    metadata = repository.ingest_metadata(
        value,
        classify_attachment(value),
        CatalogMatch(7, "Heme/Lymph", 1, 0.99, "exact"),
    )
    monkeypatch.setattr("oms_hub.canvas.ingestion.office_file_is_encrypted", lambda path: False)
    return IngestionService(repository, settings, stability_wait_seconds=0), repository, metadata


def test_rejects_download_outside_managed_inbox(database, tmp_path, monkeypatch) -> None:
    ingestion, _, _ = prepared(database, tmp_path, monkeypatch)
    outside = tmp_path / "outside.pptx"
    outside.write_bytes(b"PK123456")
    with pytest.raises(ValueError, match="managed Canvas inbox"):
        ingestion.complete_download(1, 99, outside)


def test_ingest_promotes_one_immutable_original_and_replay_is_idempotent(
    database, tmp_path, monkeypatch
) -> None:
    ingestion, repository, metadata = prepared(database, tmp_path, monkeypatch)
    source = Path(ingestion.settings.canvas_inbox) / "source.pptx"
    source.write_bytes(b"PK123456")
    first = ingestion.complete_download(metadata.source_item_id, 99, source)
    second = ingestion.complete_download(metadata.source_item_id, 99, source)
    assert first.revision_id == second.revision_id
    assert first.sha256 == second.sha256
    assert first.stored_path.read_bytes() == source.read_bytes()
    assert repository.count_jobs(first.revision_id, "convert") == 1


def test_size_mismatch_enters_review(database, tmp_path, monkeypatch) -> None:
    ingestion, repository, metadata = prepared(database, tmp_path, monkeypatch)
    source = Path(ingestion.settings.canvas_inbox) / "source.pptx"
    source.write_bytes(b"PK-too-large")
    with pytest.raises(ValueError, match="size does not match"):
        ingestion.complete_download(metadata.source_item_id, 99, source)
    assert repository.list_review_items()[0].id == metadata.source_item_id
