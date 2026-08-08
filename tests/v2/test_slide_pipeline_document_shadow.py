import hashlib
import json
from pathlib import Path

import pytest
from pypdf import PdfWriter

from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
    SourceSnapshot,
)
from oms_hub.document_processing.shadow import DocumentShadowEvaluator
from oms_hub.domain import LectureKey
from oms_hub.files.atomic import verified_atomic_copy
from oms_hub.files.office import OfficeTimeoutError
from oms_hub.ingestion.domain import StagedUpload, UploadKind
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.repositories import CatalogRepository, LectureInput
from oms_hub.routing import build_slide_destinations
from oms_hub.slides.pipeline import SlidePipeline


class PdfFixtureConverter:
    def convert(self, source: Path, destination: Path) -> None:
        del source
        destination.parent.mkdir(parents=True, exist_ok=True)
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        with destination.open("wb") as stream:
            writer.write(stream)


class LegacyProcessor:
    name = "legacy"
    version = "1"

    def supports(self, snapshot: SourceSnapshot) -> bool:
        return True

    def parse(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument:
        return ParsedDocument(
            source_id=snapshot.id,
            source_sha256=snapshot.sha256,
            source_format="pptx",
            parser_name=self.name,
            parser_version=self.version,
            segments=(
                ParsedSegment(
                    "slide-1",
                    SegmentKind.PARAGRAPH,
                    "Legacy slide text",
                    DocumentLocator("slide 1", slide_number=1),
                ),
            ),
            assets=(),
            warnings=(),
        )


class RaisingProcessor(LegacyProcessor):
    name = "anydoc"

    def parse(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument:
        raise RuntimeError("bad deck")


def _slide_pipeline(
    tmp_path: Path,
    evaluator: DocumentShadowEvaluator | None = None,
    converter: PdfFixtureConverter | None = None,
) -> tuple[SlidePipeline, str]:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    lecture_id = CatalogRepository(database).upsert_lecture(
        LectureInput("Neuro", 1, 1, "Seizures", "", None)
    )
    payload = b"slide fixture"
    staged = tmp_path / "lecture.pptx"
    staged.write_bytes(payload)
    repository = IngestionRepository(database)
    batch_id = repository.create_batch(UploadKind.SLIDES)
    repository.add_item(
        UploadKind.SLIDES,
        StagedUpload(
            batch_id=batch_id,
            item_id="slide-item",
            path=staged,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            original_filename="lecture.pptx",
        ),
    )
    repository.set_manual_assignment("slide-item", lecture_id)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        study_root=tmp_path / "study",
        icloud_staging_root=tmp_path / "icloud",
        document_parser_mode="shadow",
    )
    return (
        SlidePipeline(
            database,
            settings,
            converter or PdfFixtureConverter(),
            evaluator or DocumentShadowEvaluator(RaisingProcessor(), LegacyProcessor()),
        ),
        "slide-item",
    )


def test_transient_office_failure_keeps_revision_promotable_for_retry(
    tmp_path: Path,
) -> None:
    class TimeoutThenSuccessConverter(PdfFixtureConverter):
        def __init__(self) -> None:
            self.calls = 0

        def convert(self, source: Path, destination: Path) -> None:
            self.calls += 1
            if self.calls == 1:
                raise OfficeTimeoutError("Office timed out")
            super().convert(source, destination)

    converter = TimeoutThenSuccessConverter()
    pipeline, item_id = _slide_pipeline(tmp_path, converter=converter)

    with pytest.raises(OfficeTimeoutError, match="Office timed out"):
        pipeline.process(item_id)

    retryable = pipeline.repository.begin_revision(
        item_id,
        tmp_path / "artifacts" / "v2" / "slides",
    )
    assert retryable.state == "proposed"

    revision = pipeline.process(item_id)

    assert revision.current is True
    assert revision.state == "current"
    assert converter.calls == 2


def test_shadow_failure_does_not_fail_slide_filing(tmp_path: Path) -> None:
    pipeline, item_id = _slide_pipeline(tmp_path)

    revision = pipeline.process(item_id)

    assert revision.current is True
    report_path = next((tmp_path / "document-processing" / "shadow").glob("*.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["candidate_error"] == "candidate_parse_failed"


def test_shadow_report_write_failure_does_not_fail_slide_filing(tmp_path: Path) -> None:
    class RaisingWriterEvaluator(DocumentShadowEvaluator):
        @staticmethod
        def write_report(report: dict[str, object], destination: Path) -> None:
            del report, destination
            raise OSError("report path unavailable")

    pipeline, item_id = _slide_pipeline(
        tmp_path,
        RaisingWriterEvaluator(RaisingProcessor(), LegacyProcessor()),
    )

    revision = pipeline.process(item_id)

    assert revision.current is True


def test_interrupted_group_promotion_recovers_old_files_before_clean_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, item_id = _slide_pipeline(tmp_path)
    destinations = build_slide_destinations(
        pipeline.settings,
        LectureKey("Neuro", 1, 1, "Seizures"),
    )
    canonical = (destinations.source, destinations.pdf, destinations.icloud_pdf)
    for destination in canonical:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"old")

    original_promote = pipeline.promotion.promote

    def crash_after_first_copy(pairs, revision_id, commit):
        del commit
        for _, destination in pairs:
            verified_atomic_copy(
                destination,
                pipeline.promotion.backup_path(destination, revision_id),
            )
        verified_atomic_copy(*pairs[0])
        raise SystemExit("simulated process interruption")

    monkeypatch.setattr(pipeline.promotion, "promote", crash_after_first_copy)
    with pytest.raises(SystemExit, match="simulated process interruption"):
        pipeline.process(item_id)

    interrupted = pipeline.repository.begin_revision(
        item_id,
        tmp_path / "artifacts" / "v2" / "slides",
    )
    assert interrupted.state == "promoting"
    assert canonical[0].read_bytes() != b"old"
    assert canonical[1].read_bytes() == b"old"
    assert canonical[2].read_bytes() == b"old"

    Path(pipeline.repository.require_item(item_id).staged_path).unlink()
    pipeline.settings.study_root = tmp_path / "moved-study"
    pipeline.settings.icloud_staging_root = tmp_path / "moved-icloud"
    monkeypatch.setattr(pipeline.promotion, "promote", original_promote)
    recovered = pipeline.process(item_id)

    assert recovered.current is True
    assert recovered.state == "current"
    assert canonical[0].read_bytes() == recovered.immutable_source_path.read_bytes()
    assert canonical[1].read_bytes() == recovered.immutable_derived_path.read_bytes()
    assert canonical[2].read_bytes() == recovered.immutable_derived_path.read_bytes()
    for destination in canonical:
        assert not pipeline.promotion.backup_path(destination, recovered.id).exists()
