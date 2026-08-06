import hashlib
import json
from pathlib import Path

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
from oms_hub.ingestion.domain import StagedUpload, UploadKind
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.repositories import CatalogRepository, LectureInput
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
            PdfFixtureConverter(),
            evaluator or DocumentShadowEvaluator(RaisingProcessor(), LegacyProcessor()),
        ),
        "slide-item",
    )


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
