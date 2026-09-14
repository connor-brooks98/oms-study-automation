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
from oms_hub.models import (
    PublishedQuizModel,
    StudioRunModel,
    StudyRevisionModel,
    UploadBatchModel,
    UploadItemModel,
)
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
    automatic = service.queue(lecture_id, owner_id='owner')
    assert automatic.label == 'Lecture 01 - Fixture - Quiz'
    assert (automatic.destination_subject, automatic.destination_exam_number) == ('Heme', 3)
    auto_inputs = service.load_inputs(automatic)
    assert auto_inputs.objectives and auto_inputs.objectives[0][0] == 'source-1-1'
    assert 'source segment' in auto_inputs.objectives[0][1]
    assert any(key.startswith('source-2-') for key, _ in auto_inputs.objectives)
    assert auto_inputs.image_required
    with pytest.raises(ValueError, match='quiz title'):
        service.queue(lecture_id, owner_id='owner', require_images=False)
    with pytest.raises(ValueError, match='quiz title'):
        service.queue(lecture_id, owner_id='owner', objectives=(('custom', 'Different objective'),))
    assert service.queue(lecture_id, owner_id='owner').id == automatic.id
    # Model the completed publication's retained label reservation. Publication payload
    # validation is covered by the end-to-end lecture acceptance and export suites.
    with database.session() as session:
        session.get(StudioRunModel, automatic.id).state = 'complete'
        session.add(PublishedQuizModel(token='a' * 64, lecture_id=lecture_id,
            studio_run_id=automatic.id, destination_subject='Heme',
            destination_subject_key='heme', destination_exam_number=3,
            label=automatic.label, label_key=automatic.label.casefold(),
            title=automatic.label, payload_json='{}'))
    successor = service.queue(lecture_id, owner_id='owner')
    assert successor.id != automatic.id and successor.label == automatic.label + ' (2)'
    assert service.queue(lecture_id, owner_id='owner').id == successor.id
    with database.session() as session:
        assert session.get(PublishedQuizModel, 'a' * 64).active
        session.get(StudioRunModel, successor.id).state = 'complete'
    catalog.upsert_lecture(LectureInput('Heme', 3, 1, 'T' * 300, 'Teacher', None))
    long_title = service.queue(lecture_id, owner_id='owner')
    assert len(long_title.label) == 300 and long_title.label.endswith(' - Quiz')
    with database.session() as session:
        revision = session.scalar(select(StudyRevisionModel).where(
            StudyRevisionModel.kind == 'transcripts'))
        revision.current = False
    with pytest.raises(ValueError, match='no longer current'):
        service.load_inputs(run)
    with pytest.raises(ValueError, match='no longer current'):
        repo.record_gpt_lifecycle(run.id, SessionLifecycle(run.id + ':late', 'dispatching'))
    database.close()
