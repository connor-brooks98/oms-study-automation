from dataclasses import replace
from pathlib import Path

from oms_hub.canvas.classifier import classify_attachment
from oms_hub.canvas.domain import CatalogMatch, SourceKind
from oms_hub.canvas.matcher import match_attachment
from oms_hub.domain import LectureStepName
from oms_hub.files.atomic import sha256_file
from tests.canvas.test_classifier import attachment
from tests.canvas.test_matcher import lecture
from tests.canvas.test_pipeline import add_revision, prepared, stored_step


def test_pq_is_converted_and_filed_without_claiming_notebooklm_upload(database, tmp_path) -> None:
    settings, catalog, lecture_id, repository, pipeline = prepared(database, tmp_path)
    value = attachment("Practice questions for anemia.docx")
    revision = add_revision(settings, repository, lecture_id, value)
    result = pipeline.process_revision(revision.id)
    assert result.paths.local_pdf.parent.name == "Practice Questions"
    assert result.paths.local_pdf.exists()
    assert result.paths.icloud_pdf.exists()
    assert stored_step(
        catalog, lecture_id, LectureStepName.PRACTICE_QUESTIONS_UPLOADED
    ).status == "waiting"


def test_professor_lecture_pdf_is_ignored() -> None:
    assert classify_attachment(attachment("Anemia.pdf")).kind is SourceKind.IGNORE


def test_exam_level_pq_routes_without_forcing_a_lecture_match(database, tmp_path) -> None:
    settings, _, _, repository, pipeline = prepared(database, tmp_path)
    value = attachment(
        "Exam 1 Practice Questions.docx",
        item_title="Exam 1 Practice Questions",
        page_title="Exam 1 Practice Questions",
    )
    stored = repository.ingest_metadata(
        value,
        classify_attachment(value),
        CatalogMatch(None, "Heme/Lymph", 1, 0.95, "exam module"),
    )
    source = Path(settings.revision_root) / str(stored.revision_id) / value.filename
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"PK-source")
    repository.complete_ingestion(stored.revision_id, sha256_file(source), str(source))
    result = pipeline.process_revision(stored.revision_id)
    assert result.paths.local_pdf.name == "Exam 1 Practice Questions.pdf"
    assert result.paths.local_pdf.exists()


def test_epc_unique_topic_matches_and_competing_topic_reviews() -> None:
    unique = attachment(
        "Assessment.pptx",
        module_title="Giving the Assessment and Plan",
        item_title="Giving the Assessment and Plan Lecture",
        page_title="Giving the Assessment and Plan Lecture",
    )
    catalog = [
        lecture(1, "EPC", 1, 1, "The Difficult Patient"),
        lecture(2, "EPC", 1, 2, "Giving the Assessment and Plan"),
    ]
    assert match_attachment(unique, "EPC", catalog).lecture_id == 2
    competing = replace(
        unique,
        module_title="Communication",
        item_title="Communication Lecture",
        page_title="Communication Lecture",
    )
    choices = [
        lecture(3, "EPC", 1, 3, "Communication I"),
        lecture(4, "EPC", 2, 7, "Communication II"),
    ]
    assert match_attachment(competing, "EPC", choices).lecture_id is None
