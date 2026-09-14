"""Real local format processing; cleaner and existing Office boundary are fixtures."""

import hashlib
import json
from io import BytesIO

import pytest
from PIL import Image
from pypdf import PdfReader
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from oms_hub.anki.sources import LectureSourceExtractor
from oms_hub.artifacts import ArtifactConflict, ArtifactRole, ArtifactService
from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.document_processing.lecture_intake import SUPPORTED_LECTURE_SUFFIXES
from oms_hub.ingestion.domain import UploadKind, UploadState
from oms_hub.ingestion.matcher import UploadMatcher
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.ingestion.service import IngestionService
from oms_hub.ingestion.staging import StagingService
from oms_hub.llm.domain import CleanResult
from oms_hub.repositories import CatalogRepository, LectureInput
from oms_hub.slides.pipeline import SlidePipeline
from oms_hub.study_chat.sources import ChatSources
from oms_hub.transcripts.pipeline import TranscriptPipeline
from oms_hub.transcripts.prompt import ApprovedPrompt
from tests.document_processing.test_lecture_intake import source_file


class _Prompt:
    def current(self):
        return ApprovedPrompt("Retain all lecture facts.", "a" * 64)


class _Cleaner:
    def __init__(self):
        self.inputs = []

    def clean(self, raw_text, prompt):
        self.inputs.append(raw_text)
        return CleanResult(
            text=raw_text,
            provider="codex_subscription",
            model="local-test-fixture",
            request_id="local-test-fixture",
            input_tokens=0,
            output_tokens=0,
            cost_microusd=0,
        )


class _Converter:
    def __init__(self):
        self.calls = []

    def convert(self, source, destination):
        self.calls.append(source)
        assert source.suffix == ".pptx"
        canvas = Canvas(str(destination))
        canvas.drawString(40, 750, "Fixture for existing Office boundary only.")
        canvas.save()


def _environment(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    catalog = CatalogRepository(database)
    lecture = catalog.upsert_lecture(LectureInput("Neuro", 1, 1, "Blue ring", "", None))
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        study_root=tmp_path / "study",
        icloud_staging_root=tmp_path / "cloud",
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
    )
    repository = IngestionRepository(database)
    staging = StagingService(tmp_path / "stage", 2_000_000, 4_000_000)
    service = IngestionService(repository, catalog, UploadMatcher(), staging)
    return database, settings, repository, staging, service, lecture


def _stage(repository, staging, path, kind, lecture):
    batch = staging.begin_batch(kind)
    repository.create_batch(kind, batch.id)
    item = staging.stage_file(batch, path.name, BytesIO(path.read_bytes()))
    repository.add_item(kind, item)
    repository.set_manual_assignment(item.item_id, lecture)
    return item


@pytest.mark.parametrize("kind", list(UploadKind))
@pytest.mark.parametrize("suffix", SUPPORTED_LECTURE_SUFFIXES)
def test_format_reaches_filed_revision_and_duplicate_guard(tmp_path, kind, suffix):
    database, settings, repository, staging, service, lecture = _environment(tmp_path)
    source = source_file(tmp_path, suffix)
    original = source.read_bytes()
    item = _stage(repository, staging, source, kind, lecture)
    # Matching must use original format even though staged paths end in .ready.
    evidence = service._evidence(repository.require_item(item.item_id))
    assert "blue ring" in evidence.opening_text.casefold()
    cleaner, converter = _Cleaner(), _Converter()
    pipeline = (
        SlidePipeline(database, settings, converter)
        if kind is UploadKind.SLIDES
        else TranscriptPipeline(database, settings, _Prompt(), cleaner)
    )
    revision = pipeline.process(item.item_id)
    assert revision.current
    assert revision.immutable_source_path.suffix == suffix
    assert revision.immutable_source_path.read_bytes() == original
    assert revision.source_sha256 == hashlib.sha256(original).hexdigest()
    assert revision.canonical_derived_path.is_file()
    authorized = []
    chat = ChatSources(
        database.session,
        LectureSourceExtractor(repository),
        authorize_revision=lambda owner, revision_id: authorized.append((owner, revision_id)),
    )
    snapshots = chat.snapshot("local-owner", (revision.id,))
    passages = chat.passages("local-owner", snapshots)
    assert any("blue ring" in passage.text for passage in passages)
    assert {revision_id for _, revision_id in authorized} == {revision.id}
    if kind is UploadKind.SLIDES and suffix != ".pptx":
        assert all(passage.slide_number is None for passage in passages)
        assert all("transcript" not in passage.citation for passage in passages)
        assert len({p.source_id for p in passages}) == len(passages)
        assert any(("page" if suffix == ".pdf" else "block") in p.citation for p in passages)
    if kind is UploadKind.SLIDES:
        assert len(converter.calls) == (1 if suffix == ".pptx" else 0)
        assert revision.canonical_source_path.suffix == suffix
        assert revision.canonical_source_path.read_bytes() == original
        assert revision.canonical_source_path != revision.canonical_derived_path
        assert len(PdfReader(revision.canonical_derived_path).pages) > 0
        if suffix == ".pdf":
            assert revision.canonical_derived_path.read_bytes() == original
    else:
        assert len(cleaner.inputs) == 1
        assert "blue ring" in cleaner.inputs[0]
        assert revision.canonical_derived_path.read_text() == cleaner.inputs[0]
        assert (
            revision.immutable_source_path.parent / "extracted.txt"
        ).read_text() == cleaner.inputs[0]
        if suffix not in {".txt", ".md"}:
            report = json.loads(
                (
                    revision.immutable_source_path.parent / "document-assets/document.json"
                ).read_text()
            )
            assert report["source_sha256"] == revision.source_sha256
            assert all(segment["locator"]["label"] for segment in report["segments"])
    duplicate = _stage(repository, staging, source, kind, lecture)
    assert repository.require_item(duplicate.item_id).state is UploadState.COMPLETE
    assert repository.count_jobs(duplicate.item_id, "process") == 0
    artifacts = ArtifactService(database, settings)
    role = ArtifactRole.ORIGINAL if kind is UploadKind.SLIDES else ArtifactRole.RAW
    resolved = artifacts.resolve(revision.id, role)
    assert resolved.path.read_bytes() == original
    assert resolved.path.suffix == suffix
    assert hashlib.sha256(resolved.path.read_bytes()).hexdigest() == revision.source_sha256
    import mimetypes

    is_raw_text = kind is UploadKind.TRANSCRIPTS and suffix in {".txt", ".md"}
    assert resolved.media_type == (
        "text/plain; charset=utf-8" if is_raw_text else mimetypes.guess_type(source.name)[0]
    )
    assert resolved.disposition == ("inline" if is_raw_text else "attachment")
    assert resolved.text is is_raw_text
    resolved.path.write_bytes(b"changed after filing")
    with pytest.raises(ArtifactConflict, match="checksum"):
        artifacts.resolve(revision.id, role)


def test_image_only_pdf_files_unchanged_but_cannot_clean_without_ocr(tmp_path):
    database, settings, repository, staging, _service, lecture = _environment(tmp_path)
    source = tmp_path / "diagram.pdf"
    canvas = Canvas(str(source))
    canvas.drawImage(ImageReader(Image.new("RGB", (100, 100), "blue")), 40, 600, 100, 100)
    canvas.save()
    materials = _stage(repository, staging, source, UploadKind.SLIDES, lecture)
    converter = _Converter()
    revision = SlidePipeline(database, settings, converter).process(materials.item_id)
    assert revision.canonical_derived_path.read_bytes() == source.read_bytes()
    assert len(PdfReader(revision.canonical_derived_path).pages[0].images) == 1
    assert converter.calls == []
    transcript = _stage(repository, staging, source, UploadKind.TRANSCRIPTS, lecture)
    cleaner = _Cleaner()
    with pytest.raises(ValueError, match="OCR|extractable text"):
        TranscriptPipeline(database, settings, _Prompt(), cleaner).process(transcript.item_id)
    assert cleaner.inputs == []
    assert repository.require_item(transcript.item_id).state is UploadState.NEEDS_REVIEW


@pytest.mark.parametrize("suffix", [".docx", ".txt", ".md", ".rtf"])
def test_materials_queue_real_parser_manifest_without_provider(tmp_path, suffix):
    from dataclasses import replace

    from oms_hub.document_processing.anydoc_adapter import AnydocProcessor
    from oms_hub.document_processing.domain import DocumentLocator
    from oms_hub.document_processing.pdf_adapter import PdfProcessor
    from oms_hub.document_processing.pptx_locator import PptxLocatorEnricher
    from oms_hub.document_processing.presentation_render import PresentationRenderer
    from oms_hub.document_processing.router import DocumentProcessorRouter, ParserMode
    from oms_hub.document_processing.text_adapter import TextProcessor
    from oms_hub.study_generation.gpt_lecture import (
        GeneratedLectureQuiz,
        lecture_inputs_from_manifest,
        source_manifest,
        validate_generated_quiz,
        validate_lecture_inputs,
    )
    from oms_hub.study_generation.service import GptLectureService
    from oms_hub.study_generation.studio_repository import StudioRepository
    from tests.study_generation.test_gpt_lecture import _quiz_payload

    database, settings, repository, staging, _service, lecture = _environment(tmp_path)
    original = source_file(tmp_path, suffix)
    materials = _stage(repository, staging, original, UploadKind.SLIDES, lecture)
    revision = SlidePipeline(database, settings, _Converter()).process(materials.item_id)
    transcript_path = tmp_path / "spoken.txt"
    transcript_path.write_text("The blue ring binds probe A. The orange disk binds probe B.")
    transcript = _stage(repository, staging, transcript_path, UploadKind.TRANSCRIPTS, lecture)
    TranscriptPipeline(database, settings, _Prompt(), _Cleaner()).process(transcript.item_id)
    studio = StudioRepository(database)
    service = GptLectureService(
        CatalogRepository(database),
        repository,
        studio,
        DocumentProcessorRouter(
            primary=AnydocProcessor(PptxLocatorEnricher()),
            fallbacks=(PdfProcessor(), TextProcessor()),
            mode=ParserMode.ANYDOC,
        ),
        PresentationRenderer(_Converter()),
        tmp_path / "quiz-work",
        owner_id="local-owner",
        model="gpt-5.5",
    )
    run = service.queue(lecture, owner_id="local-owner")
    assert run.state == "queued"
    inputs = service.load_inputs(run)
    materials_document = inputs.documents[0]
    assert inputs.bindings[0].snapshot.path == revision.immutable_source_path
    assert inputs.bindings[0].snapshot.path.suffix == suffix
    assert inputs.bindings[0].snapshot.sha256 == hashlib.sha256(original.read_bytes()).hexdigest()
    assert all(
        s.locator.page_number is None and s.locator.slide_number is None
        for s in materials_document.segments
    )
    manifest = json.loads(studio.run_artifact(run.id, "gpt:manifest").payload_json)
    assert source_manifest(lecture_inputs_from_manifest(manifest)) == manifest
    assert inputs.image_required is bool(materials_document.assets)
    if suffix != ".docx":
        assert not inputs.image_required
        return
    asset = materials_document.assets[0]
    image_segment = next(s for s in materials_document.segments if asset.key in s.asset_keys)
    assert asset.locator.block_index == image_segment.locator.block_index
    assert asset.locator.page_number is None and asset.locator.slide_number is None
    assert (
        manifest["sources"][0]["assets"][0]["locator"]["block_index"] == asset.locator.block_index
    )
    payload = _quiz_payload(inputs)
    for question in payload["questions"]:
        question["objective_ids"] = [key for key, _text in inputs.objectives]
        question["source_segments"] = [
            {"source_id": inputs.slide_source_id, "segment_key": image_segment.key}
        ]
        if question["image"]:
            question["image"]["asset_key"] = asset.key
    validate_generated_quiz(
        GeneratedLectureQuiz.model_validate(payload), inputs, require_images=True
    )
    # An unrelated text block has no page/slide either; None==None is not evidence.
    payload["questions"][0]["source_segments"][0]["segment_key"] = "block-1"
    with pytest.raises(ValueError, match="image is not associated"):
        validate_generated_quiz(
            GeneratedLectureQuiz.model_validate(payload), inputs, require_images=True
        )
    invalid = replace(asset, locator=DocumentLocator("invented block", block_index=999))
    with pytest.raises(ValueError, match="linked block locator"):
        validate_lecture_inputs(
            replace(
                inputs,
                documents=(
                    replace(materials_document, assets=(invalid,)),
                    inputs.documents[1],
                ),
            )
        )
