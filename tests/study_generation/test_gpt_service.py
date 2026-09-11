import hashlib
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from oms_hub.db import Database
from oms_hub.document_processing.pdf_adapter import PdfProcessor
from oms_hub.document_processing.presentation_render import PresentationRenderer
from oms_hub.document_processing.router import DocumentProcessorRouter, ParserMode
from oms_hub.document_processing.text_adapter import TextProcessor
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.llm.codex_session import SessionLifecycle
from oms_hub.models import StudyRevisionModel, UploadBatchModel, UploadItemModel
from oms_hub.repositories import CatalogRepository, LectureInput
from oms_hub.study_generation.service import GptLectureService
from oms_hub.study_generation.studio_repository import StudioRepository
from tests.study_generation.test_gpt_lecture import _isolated_native_check


def test_gpt_queue_freezes_sources_without_google_and_rechecks_scope(tmp_path, request):
    if _isolated_native_check(request):
        return
    import fitz

    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    catalog = CatalogRepository(database)
    lecture_id = catalog.upsert_lecture(LectureInput('Heme', 3, 1, 'Fixture', 'Teacher', None))
    slides = tmp_path / 'slides.pdf'
    with fitz.open() as pdf:
        page = pdf.new_page(width=400, height=300)
        page.insert_text((30, 30), 'Objective: compare red cells.')
        page.draw_rect(fitz.Rect(50, 70, 100, 100), color=(1, 0, 0), fill=(1, 0, 0))
        pdf.save(slides)
    transcript = tmp_path / 'cleaned.txt'
    transcript.write_text('Red cells carry oxygen. Compare their appearance.')
    with database.session() as session:
        for kind, path in [('slides', slides), ('transcripts', transcript)]:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            session.add(UploadBatchModel(id=kind, kind=kind, state='complete'))
            session.flush()
            session.add(UploadItemModel(id=kind, batch_id=kind, kind=kind,
                original_filename=path.name, staged_path=str(path), sha256=digest,
                size_bytes=path.stat().st_size, state='complete', lecture_id=lecture_id))
            session.flush()
            session.add(StudyRevisionModel(upload_item_id=kind, lecture_id=lecture_id,
                kind=kind, source_sha256=digest, immutable_source_path=str(path),
                derived_sha256=digest, immutable_derived_path=str(path),
                canonical_derived_path=str(path), state='current', current=True))
    repo = StudioRepository(database)
    router = DocumentProcessorRouter(TextProcessor(), (PdfProcessor(),), ParserMode.ANYDOC)
    service = GptLectureService(catalog, IngestionRepository(database), repo, router,
        PresentationRenderer(SimpleNamespace()), tmp_path / 'managed-work',
        owner_id='owner', model='fixture')
    args = dict(owner_id='owner', label='Fixture quiz', objectives=(('lo-1', 'Compare red cells'),))
    with pytest.raises(PermissionError):
        service.queue(lecture_id, **(args | {'owner_id': 'other'}))
    run = service.queue(lecture_id, **args)
    assert run.backend == 'codex_subscription'
    assert run.workflow_kind.value == 'lecture_generation'
    assert repo.claim_next_run().id == run.id
    inputs = service.load_inputs(run)
    assert inputs.lecture_id == lecture_id and inputs.documents[0].assets
    assert repo.run_artifact(run.id, 'parse:' + inputs.slide_source_id)
    with database.session() as session:
        revision = session.scalar(select(StudyRevisionModel).where(
            StudyRevisionModel.kind == 'transcripts'))
        revision.current = False
    with pytest.raises(ValueError, match='no longer current'):
        service.load_inputs(run)
    with pytest.raises(ValueError, match='no longer current'):
        repo.record_gpt_lifecycle(run.id, SessionLifecycle(run.id + ':late', 'dispatching'))
    database.close()
