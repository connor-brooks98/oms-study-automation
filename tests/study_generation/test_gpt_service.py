import hashlib
import json
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


def test_automatic_learning_goal_retains_all_evidence_without_per_page_quotas(tmp_path):
    from dataclasses import replace

    from oms_hub.document_processing.domain import DocumentLocator
    from oms_hub.study_generation.service import _lecture_coverage_targets
    from tests.study_generation.test_gpt_lecture import _inputs

    inputs = _inputs(tmp_path)
    slides, transcript = inputs.documents
    slides = replace(slides, segments=tuple(
        replace(slides.segments[0], key=f"fragment-{i}",
                locator=DocumentLocator(f"slide {i % 12 + 1}", slide_number=i % 12 + 1)
                if i < 500 else DocumentLocator(f"block {i}", block_index=i))
        for i in range(600)
    ))
    transcript = replace(transcript, segments=tuple(
        replace(transcript.segments[0], key=f"spoken-{i}",
                locator=DocumentLocator(f"block {i + 1}", block_index=i + 1))
        for i in range(199)
    ))
    slides = replace(slides, assets=(*slides.assets, replace(
        slides.assets[0], key="image-only-slide",
        locator=DocumentLocator("slide 13", slide_number=13),
    )))
    inputs = replace(inputs, documents=(slides, transcript))
    targets = _lecture_coverage_targets(inputs)
    assert len(targets) == 1 and targets[0][0] == 'source-all'
    from oms_hub.study_generation.quiz_evidence import compact_evidence

    evidence = compact_evidence(replace(inputs, objectives=targets))
    assert [segment['key'] for source in evidence['sources'] for segment in source['segments']] == [
        segment.key for document in inputs.documents for segment in document.segments
    ]
    assert any(image['asset_key'] == 'image-only-slide' for image in evidence['images'])
    assert inputs.documents == (slides, transcript)


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
    assert len(auto_inputs.objectives) == 1 and auto_inputs.objectives[0][0] == 'source-all'
    assert auto_inputs.image_required
    from oms_hub.study_generation.quiz_plan import _question_count_bounds

    assert _question_count_bounds(auto_inputs.instructions) == (12, 12)
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
    service.model = 'new-model'
    changed_model = service.queue(lecture_id, owner_id='owner')
    assert changed_model.id != long_title.id
    assert service.queue(lecture_id, owner_id='owner').id == changed_model.id
    original = json.loads(repo.run_artifact(long_title.id, 'gpt:settings').payload_json)
    replacement = json.loads(repo.run_artifact(changed_model.id, 'gpt:settings').payload_json)
    assert original['model'] == 'fixture' and replacement['model'] == 'new-model'
    for text in ('Focus on mechanisms.', '15 questions per objective.', 'fifteen questions'):
        styled = service.queue(lecture_id, owner_id='owner', instructions=text)
        assert service.load_inputs(styled).instructions == text
    custom = service.queue(lecture_id, owner_id='owner', instructions='Generate 8 questions.')
    assert _question_count_bounds(service.load_inputs(custom).instructions) == (8, 8)
    with database.session() as session:
        revision = session.scalar(select(StudyRevisionModel).where(
            StudyRevisionModel.kind == 'transcripts'))
        revision.current = False
    with pytest.raises(ValueError, match='no longer current'):
        service.load_inputs(run)
    with pytest.raises(ValueError, match='no longer current'):
        repo.record_gpt_lifecycle(run.id, SessionLifecycle(run.id + ':late', 'dispatching'))
    database.close()
