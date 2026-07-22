from dataclasses import replace
from pathlib import Path

from pypdf import PdfWriter

from oms_hub.canvas.classifier import classify_attachment
from oms_hub.canvas.domain import CatalogMatch, CourseMappingInput
from oms_hub.canvas.pipeline import CanvasPipeline
from oms_hub.canvas.repository import CanvasRepository
from oms_hub.config import Settings
from oms_hub.domain import LectureStepName
from oms_hub.files.atomic import sha256_file
from oms_hub.repositories import CatalogRepository, LectureInput
from tests.canvas.test_classifier import attachment


class FakeConverter:
    def convert(self, source: Path, destination: Path) -> None:
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as stream:
            writer.write(stream)


def prepared(database, tmp_path):
    settings = Settings(
        _env_file=None,
        study_root=tmp_path / "OMS II",
        icloud_staging_root=tmp_path / "iCloud",
        revision_root=tmp_path / "revisions",
    )
    catalog = CatalogRepository(database)
    lecture_id = catalog.upsert_lecture(
        LectureInput("Heme/Lymph", 1, 4, "Anemia I", "Professor", None)
    )
    repository = CanvasRepository(database)
    repository.replace_course_mappings(
        [CourseMappingInput("751", "Hematology & Lymph", "HEME", "Heme/Lymph")]
    )
    pipeline = CanvasPipeline(database, settings, FakeConverter())
    return settings, catalog, lecture_id, repository, pipeline


def add_revision(settings, repository, lecture_id, value):
    stored = repository.ingest_metadata(
        value,
        classify_attachment(value),
        CatalogMatch(lecture_id, "Heme/Lymph", 1, 0.99, "exact"),
    )
    source = Path(settings.revision_root) / str(stored.revision_id) / value.filename
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"PK-source")
    repository.complete_ingestion(stored.revision_id, sha256_file(source), str(source))
    return repository.get_revision(stored.revision_id)


def stored_step(catalog, lecture_id, name):
    lecture = catalog.get_lecture(lecture_id)
    assert lecture is not None
    return next(item for item in lecture.steps if item.name == name.value)


def test_new_lecture_promotes_outputs_and_updates_steps(database, tmp_path) -> None:
    settings, catalog, lecture_id, repository, pipeline = prepared(database, tmp_path)
    revision = add_revision(settings, repository, lecture_id, attachment("Anemia.pptx"))
    result = pipeline.process_revision(revision.id)
    assert result.state == "current"
    assert result.paths.local_source and result.paths.local_source.exists()
    assert result.paths.local_pdf.exists()
    assert result.paths.icloud_pdf.exists()
    assert stored_step(catalog, lecture_id, LectureStepName.CANVAS_PPTX_FOUND).status == "complete"
    assert stored_step(catalog, lecture_id, LectureStepName.PDF_FILED).status == "complete"
    assert stored_step(catalog, lecture_id, LectureStepName.GOODNOTES_DELIVERED).detail.startswith(
        "Staged for import:"
    )


def test_changed_lecture_stays_proposed(database, tmp_path) -> None:
    settings, _, lecture_id, repository, pipeline = prepared(database, tmp_path)
    first = add_revision(settings, repository, lecture_id, attachment("Anemia.pptx"))
    first_result = pipeline.process_revision(first.id)
    old_pdf = first_result.paths.local_pdf.read_bytes()
    second_value = replace(
        attachment("Anemia.pptx"),
        modified_at="2026-07-22T12:00:00Z",
    )
    second = add_revision(settings, repository, lecture_id, second_value)
    result = pipeline.process_revision(second.id)
    assert result.state == "proposed"
    assert first_result.paths.local_pdf.read_bytes() == old_pdf


def test_paused_pipeline_does_not_claim_queued_job(database, tmp_path) -> None:
    settings, _, lecture_id, repository, pipeline = prepared(database, tmp_path)
    revision = add_revision(settings, repository, lecture_id, attachment("Anemia.pptx"))

    worked = pipeline.run_next()

    assert worked is False
    assert repository.get_revision(revision.id).state == "downloaded"
