import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pypdf import PdfWriter

import oms_hub.files.promotion as promotion_module
import oms_hub.slides.pipeline as slides_pipeline_module
from oms_hub.artifact_writes import ArtifactWriteClaimLost
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
from oms_hub.domain import LectureKey, StepStatus
from oms_hub.existing_artifact_import import ExistingArtifactImporter, ExistingArtifactImportRequest
from oms_hub.files.atomic import verified_atomic_copy
from oms_hub.files.office import OfficeTimeoutError, OfficeUnavailableError
from oms_hub.files.promotion import PromotionRecoveryError, PromotionSourceError
from oms_hub.ingestion.domain import StagedUpload, UploadKind, UploadState
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.ingestion.worker import IngestionWorker
from oms_hub.models import ExistingArtifactImportModel, OutlineOutputModel, StudyRevisionModel
from oms_hub.progress import SLIDE_PIPELINE_STEPS
from oms_hub.repositories import CatalogRepository, LectureInput
from oms_hub.routing import build_slide_destinations
from oms_hub.slides.pipeline import SlidePipeline
from oms_hub.study_generation.outline import OutlinePdfRenderer


class PdfFixtureConverter:
    def convert(self, source: Path, destination: Path) -> None:
        del source
        destination.parent.mkdir(parents=True, exist_ok=True)
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        with destination.open("wb") as stream:
            writer.write(stream)


class CountingConverter(PdfFixtureConverter):
    def __init__(self) -> None:
        self.calls = 0

    def convert(self, source: Path, destination: Path) -> None:
        self.calls += 1
        super().convert(source, destination)


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


def _imported_slide_repair_fixture(tmp_path: Path):
    converter = CountingConverter()
    pipeline, item_id = _slide_pipeline(tmp_path, converter=converter)
    old_revision = pipeline.process(item_id)
    assert old_revision.derived_sha256 is not None
    transcript, outline, target = (
        tmp_path / "cleaned.txt",
        tmp_path / "outline.pdf",
        tmp_path / "target.pdf",
    )
    transcript.write_text("clean transcript", encoding="utf-8")
    outline.write_bytes(
        OutlinePdfRenderer().render(
            "outline",
            "# CORE CONCEPTS\n- one\n# DEPTH MAP\n- two\n# PROFESSOR EMPHASIS FLAGS\n- three",
        )
    )
    writer = PdfWriter()
    writer.add_blank_page(width=144, height=144)
    with target.open("wb") as stream:
        writer.write(stream)

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    result = ExistingArtifactImporter(pipeline.database, pipeline.settings).import_artifacts(
        ExistingArtifactImportRequest(
            old_revision.lecture_id,
            old_revision.id,
            old_revision.source_sha256,
            digest(target),
            transcript,
            digest(transcript),
            outline,
            digest(outline),
            target,
            old_revision.derived_sha256,
            "operator",
            "reason",
            True,
        )
    )
    converter.calls = 0
    return pipeline, item_id, result, old_revision, target.read_bytes(), converter


def test_imported_derived_repair_restores_archive_without_converter(tmp_path):
    pipeline, item_id, result, old, target, converter = _imported_slide_repair_fixture(tmp_path)
    revision = IngestionRepository(pipeline.database).get_study_revision(result.slides_revision_id)
    assert revision.canonical_derived_path and revision.icloud_path
    revision.canonical_derived_path.unlink()
    revision.icloud_path.write_bytes(b"bad")
    repaired = pipeline.process(item_id)
    assert repaired.derived_sha256 == hashlib.sha256(target).hexdigest()
    assert revision.canonical_derived_path.read_bytes() == target
    assert revision.icloud_path.read_bytes() == target
    assert converter.calls == 0


@pytest.mark.parametrize(
    "tamper",
    [
        "archive-missing",
        "archive-tamper",
        "old-missing",
        "old-tamper",
        "audit-hash",
        "audit-path",
        "previous-hash",
        "previous-path",
        "provenance",
        "slide-import",
        "slide-provenance",
        "transcript-edge",
        "outline-edge",
    ],
)
def test_imported_derived_repair_tampering_fails_closed(tmp_path, tamper):
    pipeline, item_id, result, _old, _target, converter = _imported_slide_repair_fixture(tmp_path)
    with pipeline.database.session() as session:
        audit = session.get(ExistingArtifactImportModel, result.import_id)
        slide = session.get(StudyRevisionModel, result.slides_revision_id)
        outline = session.get(OutlineOutputModel, result.outline_id)
        assert audit is not None and slide is not None and outline is not None
        if tamper == "archive-missing":
            Path(audit.imported_immutable_pdf_path).unlink()
        elif tamper == "archive-tamper":
            Path(audit.imported_immutable_pdf_path).write_bytes(b"bad")
        elif tamper == "old-missing":
            Path(audit.previous_immutable_pdf_path).unlink()
        elif tamper == "old-tamper":
            Path(audit.previous_immutable_pdf_path).write_bytes(b"bad")
        elif tamper == "audit-hash":
            audit.imported_pdf_sha256 = "a" * 64
        elif tamper == "audit-path":
            audit.imported_immutable_pdf_path = str(tmp_path / "wrong.pdf")
        elif tamper == "previous-hash":
            audit.previous_pdf_sha256 = "a" * 64
        elif tamper == "previous-path":
            audit.previous_immutable_pdf_path = str(tmp_path / "wrong.pdf")
        elif tamper == "provenance":
            audit.derived_provenance = "other"
        elif tamper == "slide-import":
            slide.import_id = None
        elif tamper == "slide-provenance":
            slide.provenance_kind = "llm_cleaned"
        elif tamper == "transcript-edge":
            audit.transcript_revision_id = None
        else:
            outline.slide_sha256 = "a" * 64
    revision = IngestionRepository(pipeline.database).get_study_revision(result.slides_revision_id)
    assert revision.canonical_derived_path is not None
    before = revision.canonical_derived_path.read_bytes()
    with pytest.raises(PromotionSourceError):
        pipeline.process(item_id)
    assert revision.canonical_derived_path.read_bytes() == before
    assert converter.calls == 0


@pytest.mark.parametrize("destination", ["canonical", "icloud"])
def test_imported_derived_repair_rejects_symlinked_destination_before_write(
    tmp_path, monkeypatch, destination
):
    pipeline, item_id, result, _old, _target, converter = _imported_slide_repair_fixture(tmp_path)
    revision = IngestionRepository(pipeline.database).get_study_revision(result.slides_revision_id)
    assert revision.canonical_derived_path is not None and revision.icloud_path is not None
    target = revision.canonical_derived_path if destination == "canonical" else revision.icloud_path
    before = target.read_bytes()
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: original_is_symlink(path) or path == target,
    )

    with pytest.raises(PromotionSourceError, match="audit is incomplete"):
        pipeline.process(item_id)

    assert target.read_bytes() == before
    assert converter.calls == 0


@pytest.mark.parametrize("component", ["v2-root", "study-root", "icloud-root"])
def test_imported_derived_repair_rejects_mocked_windows_junction_before_write(
    tmp_path, monkeypatch, component
):
    pipeline, item_id, result, _old, target, converter = _imported_slide_repair_fixture(tmp_path)
    revision = IngestionRepository(pipeline.database).get_study_revision(result.slides_revision_id)
    assert revision.canonical_derived_path is not None
    components = {
        "v2-root": pipeline.settings.data_dir / "artifacts" / "v2",
        "study-root": pipeline.settings.study_root,
        "icloud-root": pipeline.settings.icloud_staging_root,
    }
    junction = components[component]
    original_is_junction = getattr(Path, "is_junction", None)

    def is_junction(path: Path) -> bool:
        return path == junction or (
            callable(original_is_junction) and bool(original_is_junction(path))
        )

    monkeypatch.setattr(Path, "is_junction", is_junction, raising=False)
    before = revision.canonical_derived_path.read_bytes()

    with pytest.raises(PromotionSourceError, match="audit is incomplete"):
        pipeline.process(item_id)

    assert revision.canonical_derived_path.read_bytes() == before
    assert target == before
    assert converter.calls == 0


@pytest.mark.parametrize("root_name", ["v2", "study", "icloud"])
@pytest.mark.parametrize("indirection", ["junction", "symlink"])
def test_imported_derived_repair_rejects_mocked_parent_indirection_before_write(
    tmp_path, monkeypatch, root_name, indirection
):
    pipeline, item_id, result, _old, target, converter = _imported_slide_repair_fixture(tmp_path)
    revision = IngestionRepository(pipeline.database).get_study_revision(result.slides_revision_id)
    assert revision.canonical_derived_path is not None
    parents = {
        "v2": pipeline.settings.data_dir / "artifacts",
        "study": pipeline.settings.study_root.parent,
        "icloud": pipeline.settings.icloud_staging_root.parent,
    }
    junction = parents[root_name]
    if indirection == "junction":
        original_is_junction = getattr(Path, "is_junction", None)

        def is_junction(path: Path) -> bool:
            return path == junction or (
                callable(original_is_junction) and bool(original_is_junction(path))
            )

        monkeypatch.setattr(Path, "is_junction", is_junction, raising=False)
    else:
        original_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda path: path == junction or original_is_symlink(path),
        )
    before = revision.canonical_derived_path.read_bytes()

    with pytest.raises(PromotionSourceError, match="audit is incomplete"):
        pipeline.process(item_id)

    assert revision.canonical_derived_path.read_bytes() == before
    assert target == before
    assert converter.calls == 0


def test_imported_derived_repair_rejects_real_artifacts_parent_symlink_before_write(tmp_path):
    pipeline, item_id, result, _old, target, converter = _imported_slide_repair_fixture(tmp_path)
    revision = IngestionRepository(pipeline.database).get_study_revision(result.slides_revision_id)
    assert revision.canonical_derived_path is not None
    artifacts = pipeline.settings.data_dir / "artifacts"
    moved = tmp_path / "artifacts-real"
    try:
        artifacts.rename(moved)
        artifacts.symlink_to(moved, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink support unavailable: {error}")
    before = revision.canonical_derived_path.read_bytes()

    with pytest.raises(PromotionSourceError, match="audit is incomplete"):
        pipeline.process(item_id)

    assert revision.canonical_derived_path.read_bytes() == before
    assert target == before
    assert converter.calls == 0


@pytest.mark.parametrize(
    ("loss_call", "boundary"),
    [(7, "first-destination"), (8, "second-destination")],
)
def test_imported_repair_claim_loss_preserves_successor_and_recovers_on_retry(
    tmp_path: Path,
    loss_call: int,
    boundary: str,
) -> None:
    pipeline, item_id, result, _old, target, converter = _imported_slide_repair_fixture(tmp_path)
    repository = IngestionRepository(pipeline.database)
    revision = repository.get_study_revision(result.slides_revision_id)
    assert revision.immutable_derived_path is not None
    assert revision.canonical_derived_path is not None
    assert revision.icloud_path is not None
    destinations = (revision.canonical_derived_path, revision.icloud_path)
    for destination in destinations:
        destination.write_bytes(b"predecessor mutable PDF")
    successor_bytes = (
        b"successor canonical PDF",
        b"successor iCloud PDF",
    )

    class LostDuringPromotion:
        def __init__(self) -> None:
            self.calls = 0
            self.replaced = False

        def assert_owned(self) -> None:
            self.calls += 1
            if self.calls >= loss_call:
                if not self.replaced:
                    for destination, content in zip(destinations, successor_bytes, strict=True):
                        destination.write_bytes(content)
                    self.replaced = True
                raise ArtifactWriteClaimLost(f"successor owns {boundary}")

    with pytest.raises(ArtifactWriteClaimLost, match=f"successor owns {boundary}"):
        pipeline._repair_current_revision(  # noqa: SLF001
            pipeline.repository.require_item(item_id).staged_path,
            revision,
            revision.immutable_derived_path,
            LostDuringPromotion(),
        )
    assert tuple(destination.read_bytes() for destination in destinations) == successor_bytes
    assert all(
        pipeline.promotion.backup_path(destination, revision.id).is_file()
        for destination in destinations
    )
    stored = repository.get_study_revision(revision.id)
    assert (stored.current, stored.provenance_kind, stored.import_id) == (
        True,
        "imported_derived",
        result.import_id,
    )
    with pipeline.database.session() as session:
        audit = session.get(ExistingArtifactImportModel, result.import_id)
        assert audit is not None
        assert (audit.status, audit.recovery_phase) == ("complete", "committed")
    repaired = pipeline._repair_current_revision(  # noqa: SLF001
        pipeline.repository.require_item(item_id).staged_path,
        stored,
        stored.immutable_derived_path,
        _OwnedClaim(),
    )
    assert repaired.id == revision.id
    assert all(destination.read_bytes() == target for destination in destinations)
    assert all(
        not pipeline.promotion.backup_path(destination, revision.id).exists()
        for destination in destinations
    )
    assert converter.calls == 0


class _OwnedClaim:
    def assert_owned(self) -> None:
        return None


@pytest.mark.parametrize("failure", ["copy", "commit"])
def test_imported_repair_owned_promotion_failure_restores_mutable_destinations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    pipeline, item_id, result, _old, _target, converter = _imported_slide_repair_fixture(tmp_path)
    revision = IngestionRepository(pipeline.database).get_study_revision(result.slides_revision_id)
    assert revision.immutable_derived_path is not None
    assert revision.canonical_derived_path is not None
    assert revision.icloud_path is not None
    destinations = (revision.canonical_derived_path, revision.icloud_path)
    prior = (b"prior canonical PDF", b"prior iCloud PDF")
    for destination, content in zip(destinations, prior, strict=True):
        destination.write_bytes(content)

    if failure == "copy":
        def fail_imported_copy(*_args: object, **_kwargs: object) -> object:
            raise OSError("destination is locked")

        monkeypatch.setattr(
            slides_pipeline_module,
            "hardened_promote_with_rollback",
            fail_imported_copy,
        )
        with pytest.raises(PromotionRecoveryError, match="could not complete"):
            pipeline._repair_current_revision(  # noqa: SLF001
                pipeline.repository.require_item(item_id).staged_path,
                revision,
                revision.immutable_derived_path,
                _OwnedClaim(),
            )
    else:
        with pytest.raises(RuntimeError, match="commit failed"):
            pipeline.promotion.promote(
                [(revision.immutable_derived_path, destination) for destination in destinations],
                revision.id,
                lambda: (_ for _ in ()).throw(RuntimeError("commit failed")),
                _OwnedClaim(),
            )
    assert tuple(destination.read_bytes() for destination in destinations) == prior
    assert all(
        not pipeline.promotion.backup_path(destination, revision.id).exists()
        for destination in destinations
    )
    stored = IngestionRepository(pipeline.database).get_study_revision(revision.id)
    assert (stored.current, stored.provenance_kind, stored.import_id) == (
        True,
        "imported_derived",
        result.import_id,
    )
    assert converter.calls == 0


def test_ordinary_current_slide_repair_reconverts_only_missing_immutable_pdf(
    tmp_path: Path,
) -> None:
    converter = CountingConverter()
    pipeline, item_id = _slide_pipeline(tmp_path, converter=converter)
    revision = pipeline.process(item_id)
    assert revision.immutable_derived_path is not None
    converter.calls = 0
    revision.immutable_derived_path.unlink()
    repaired = pipeline.process(item_id)
    assert repaired.provenance_kind != "imported_derived"
    assert converter.calls == 1


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


def test_exhausted_office_retries_retire_incomplete_revision(
    tmp_path: Path,
) -> None:
    class UnavailableConverter(PdfFixtureConverter):
        available = False

        def convert(self, source: Path, destination: Path) -> None:
            if not self.available:
                raise OfficeUnavailableError("Office is unavailable")
            super().convert(source, destination)

    converter = UnavailableConverter()
    pipeline, item_id = _slide_pipeline(
        tmp_path,
        converter=converter,
    )
    started = datetime(2026, 8, 8, tzinfo=UTC)
    attempts = iter(
        (
            started,
            started + timedelta(seconds=10),
            started + timedelta(seconds=30),
            started + timedelta(seconds=40),
        )
    )
    worker = IngestionWorker(
        pipeline.repository,
        pipeline,
        pipeline,
        now=lambda: next(attempts),
    )

    assert worker.run_once() is True
    revision = pipeline.repository.list_proposed_revisions()[0]
    assert revision.state == "proposed"
    assert worker.run_once() is True
    assert worker.run_once() is True

    retired = pipeline.repository.get_study_revision(revision.id)
    assert retired.state == "failed"
    assert retired.derived_sha256 is None
    assert retired not in pipeline.repository.list_proposed_revisions()
    assert pipeline.repository.require_item(item_id).state is UploadState.NEEDS_REVIEW

    payload = pipeline.repository.require_item(item_id).staged_path.read_bytes()
    retry_staged = tmp_path / "manual-retry.pptx"
    retry_staged.write_bytes(payload)
    retry_batch = pipeline.repository.create_batch(UploadKind.SLIDES)
    pipeline.repository.add_item(
        UploadKind.SLIDES,
        StagedUpload(
            batch_id=retry_batch,
            item_id="manual-retry",
            path=retry_staged,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            original_filename="manual-retry.pptx",
        ),
    )
    lecture_id = pipeline.repository.require_item(item_id).lecture_id
    assert lecture_id is not None
    pipeline.repository.set_manual_assignment("manual-retry", lecture_id)
    converter.available = True

    assert worker.run_once() is True
    promoted = pipeline.repository.get_study_revision(revision.id)
    assert promoted.current is True
    assert promoted.state == "current"
    assert pipeline.repository.require_item(item_id).state is UploadState.COMPLETE
    assert pipeline.repository.require_item("manual-retry").state is UploadState.COMPLETE


def test_exhausted_failure_retires_revision_with_corrupt_immutable_pdf(
    tmp_path: Path,
) -> None:
    pipeline, item_id = _slide_pipeline(tmp_path)
    pipeline.process(item_id)
    replacement_payload = b"replacement slide fixture"
    replacement_staged = tmp_path / "replacement.pptx"
    replacement_staged.write_bytes(replacement_payload)
    replacement_batch = pipeline.repository.create_batch(UploadKind.SLIDES)
    pipeline.repository.add_item(
        UploadKind.SLIDES,
        StagedUpload(
            batch_id=replacement_batch,
            item_id="replacement",
            path=replacement_staged,
            sha256=hashlib.sha256(replacement_payload).hexdigest(),
            size_bytes=len(replacement_payload),
            original_filename="replacement.pptx",
        ),
    )
    lecture_id = pipeline.repository.require_item(item_id).lecture_id
    assert lecture_id is not None
    pipeline.repository.set_manual_assignment("replacement", lecture_id)
    proposed = pipeline.process("replacement")
    assert proposed.state == "proposed"
    lecture = pipeline.catalog.get_lecture(proposed.lecture_id)
    assert lecture is not None
    filed = next(step for step in lecture.steps if step.name == "slides_filed")
    assert filed.status == StepStatus.NEEDS_REVIEW.value
    assert filed.detail == "A slide replacement is ready for approval"
    assert proposed.immutable_derived_path is not None
    proposed.immutable_derived_path.write_bytes(b"corrupt PDF")

    pipeline.repository.fail_incomplete_study_revision("replacement")

    retired = pipeline.repository.get_study_revision(proposed.id)
    assert retired.state == "failed"
    assert retired not in pipeline.repository.list_proposed_revisions()


@pytest.mark.parametrize(
    "artifact_name",
    ("canonical_source_path", "canonical_derived_path", "icloud_path"),
)
def test_exact_current_slide_repairs_filed_artifacts_without_state_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_name: str,
) -> None:
    pipeline, item_id = _slide_pipeline(tmp_path)
    current = pipeline.process(item_id)
    payload = current.immutable_source_path.read_bytes()
    damaged = getattr(current, artifact_name)
    assert isinstance(damaged, Path)
    damaged.write_bytes(b"corrupt filed artifact")
    if artifact_name == "canonical_source_path":
        current.immutable_source_path.write_bytes(b"corrupt immutable PowerPoint")
    elif artifact_name == "canonical_derived_path":
        assert current.immutable_derived_path is not None
        current.immutable_derived_path.write_bytes(b"corrupt immutable PDF")

    staged = tmp_path / "repair-slide.pptx"
    staged.write_bytes(payload)
    batch_id = pipeline.repository.create_batch(UploadKind.SLIDES)
    pipeline.repository.add_item(
        UploadKind.SLIDES,
        StagedUpload(
            batch_id=batch_id,
            item_id="repair-slide",
            path=staged,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            original_filename="repair-slide.pptx",
        ),
    )
    pipeline.repository.set_manual_assignment("repair-slide", current.lecture_id)

    def reject_state_transition(_revision_id: int) -> None:
        raise AssertionError("current repair must not begin a state transition")

    monkeypatch.setattr(
        pipeline.repository,
        "begin_study_promotion",
        reject_state_transition,
    )
    repaired = pipeline.process("repair-slide")

    assert repaired.id == current.id
    assert repaired.current is True
    assert repaired.state == "current"
    assert pipeline.repository.require_item("repair-slide").state is UploadState.COMPLETE
    assert current.immutable_derived_path is not None
    assert current.canonical_source_path is not None
    assert current.canonical_derived_path is not None
    assert current.icloud_path is not None
    assert current.canonical_source_path.read_bytes() == current.immutable_source_path.read_bytes()
    assert (
        current.canonical_derived_path.read_bytes() == current.immutable_derived_path.read_bytes()
    )
    assert current.icloud_path.read_bytes() == current.immutable_derived_path.read_bytes()


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
    original_recover = pipeline.promotion.recover

    def crash_after_first_copy(pairs, revision_id, commit, claim):
        del commit, claim
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

    recovery_started = datetime(2026, 8, 8, tzinfo=UTC)
    recovery_attempts = iter(
        (
            recovery_started,
            recovery_started + timedelta(seconds=10),
            recovery_started + timedelta(seconds=30),
        )
    )
    worker = IngestionWorker(
        pipeline.repository,
        pipeline,
        pipeline,
        now=lambda: next(recovery_attempts),
    )
    assert worker.recover_interrupted_jobs() == 1
    monkeypatch.setattr(
        pipeline.promotion,
        "recover",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PromotionRecoveryError("destination is locked")
        ),
    )
    assert worker.run_once() is True
    assert worker.run_once() is True
    assert worker.run_once() is True
    assert pipeline.repository.require_item(item_id).state is UploadState.QUEUED
    assert pipeline.repository.get_study_revision(interrupted.id).state == "promoting"

    payload = pipeline.repository.require_item(item_id).staged_path.read_bytes()
    duplicate_staged = tmp_path / "recovery-duplicate.pptx"
    duplicate_staged.write_bytes(payload)
    duplicate_batch = pipeline.repository.create_batch(UploadKind.SLIDES)
    pipeline.repository.add_item(
        UploadKind.SLIDES,
        StagedUpload(
            batch_id=duplicate_batch,
            item_id="recovery-duplicate",
            path=duplicate_staged,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            original_filename="recovery-duplicate.pptx",
        ),
    )
    lecture_id = pipeline.repository.require_item(item_id).lecture_id
    assert lecture_id is not None
    pipeline.repository.set_manual_assignment("recovery-duplicate", lecture_id)
    Path(pipeline.repository.require_item(item_id).staged_path).unlink()
    pipeline.settings.study_root = tmp_path / "moved-study"
    pipeline.settings.icloud_staging_root = tmp_path / "moved-icloud"
    monkeypatch.setattr(pipeline.promotion, "promote", original_promote)
    monkeypatch.setattr(pipeline.promotion, "recover", original_recover)
    recovered = pipeline.process("recovery-duplicate")

    assert recovered.current is True
    assert recovered.state == "current"
    assert canonical[0].read_bytes() == recovered.immutable_source_path.read_bytes()
    assert canonical[1].read_bytes() == recovered.immutable_derived_path.read_bytes()
    assert canonical[2].read_bytes() == recovered.immutable_derived_path.read_bytes()
    assert pipeline.repository.require_item(item_id).state is UploadState.COMPLETE
    assert pipeline.repository.require_item("recovery-duplicate").state is UploadState.COMPLETE
    lecture = pipeline.catalog.get_lecture(recovered.lecture_id)
    assert lecture is not None
    step_statuses = {step.name: step.status for step in lecture.steps}
    for step in SLIDE_PIPELINE_STEPS:
        assert step_statuses[step.value] == StepStatus.COMPLETE.value
    for destination in canonical:
        assert not pipeline.promotion.backup_path(destination, recovered.id).exists()


def test_post_commit_promotion_crash_requeues_finalization(
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

    def crash_after_database_commit(pairs, revision_id, commit, claim):
        del claim
        for _, destination in pairs:
            if destination.exists():
                verified_atomic_copy(
                    destination,
                    pipeline.promotion.backup_path(destination, revision_id),
                )
        for source, destination in pairs:
            verified_atomic_copy(source, destination)
        commit()
        raise SystemExit("simulated post-commit process interruption")

    monkeypatch.setattr(
        pipeline.promotion,
        "promote",
        crash_after_database_commit,
    )
    with pytest.raises(SystemExit, match="post-commit process interruption"):
        pipeline.process(item_id)

    stored_item = pipeline.repository.require_item(item_id)
    assert stored_item.lecture_id is not None
    committed = pipeline.repository.list_current_revisions(stored_item.lecture_id)[0]
    assert committed.current is True
    assert committed.state == "current"
    assert pipeline.repository.require_item(item_id).state is UploadState.PROCESSING
    for destination in canonical:
        assert pipeline.promotion.backup_path(destination, committed.id).is_file()

    assert pipeline.repository.recover_interrupted_jobs() == 1
    assert pipeline.repository.require_item(item_id).state is UploadState.QUEUED
    monkeypatch.setattr(pipeline.promotion, "promote", original_promote)
    recovered = pipeline.process(item_id)

    assert recovered.current is True
    assert pipeline.repository.require_item(item_id).state is UploadState.COMPLETE
    lecture = pipeline.catalog.get_lecture(recovered.lecture_id)
    assert lecture is not None
    step_statuses = {step.name: step.status for step in lecture.steps}
    for step in SLIDE_PIPELINE_STEPS:
        assert step_statuses[step.value] == StepStatus.COMPLETE.value
    for destination in canonical:
        assert not pipeline.promotion.backup_path(destination, recovered.id).exists()


def test_initial_group_promotion_lock_preserves_backup_and_retries(
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

    original_copy = promotion_module.verified_atomic_copy
    original_replace = promotion_module.os.replace

    def locked_copy(source: Path, destination: Path) -> str:
        if destination == canonical[1]:
            raise OSError("destination is locked")
        return original_copy(source, destination)

    def locked_restore(source: Path, destination: Path) -> None:
        if destination == canonical[1]:
            raise OSError("rollback destination is locked")
        original_replace(source, destination)

    monkeypatch.setattr(promotion_module, "verified_atomic_copy", locked_copy)
    monkeypatch.setattr(promotion_module.os, "replace", locked_restore)
    worker = IngestionWorker(pipeline.repository, pipeline, pipeline)

    assert worker.run_once() is True
    assert pipeline.repository.require_item(item_id).state is UploadState.QUEUED
    interrupted = pipeline.repository.begin_revision(
        item_id,
        tmp_path / "artifacts" / "v2" / "slides",
    )
    assert interrupted.state == "promoting"
    assert pipeline.promotion.backup_path(canonical[1], interrupted.id).is_file()

    monkeypatch.setattr(promotion_module, "verified_atomic_copy", original_copy)
    monkeypatch.setattr(promotion_module.os, "replace", original_replace)
    recovered = pipeline.process(item_id)

    assert recovered.current is True
    assert pipeline.repository.require_item(item_id).state is UploadState.COMPLETE
    assert canonical[0].read_bytes() == recovered.immutable_source_path.read_bytes()
    assert canonical[1].read_bytes() == recovered.immutable_derived_path.read_bytes()
    assert canonical[2].read_bytes() == recovered.immutable_derived_path.read_bytes()
    for destination in canonical:
        assert not pipeline.promotion.backup_path(destination, recovered.id).exists()


def test_missing_immutable_source_terminates_promotion_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, item_id = _slide_pipeline(tmp_path)
    original_promote = pipeline.promotion.promote

    def interrupt_promotion(_pairs, _revision_id, _commit, _claim):
        raise SystemExit("simulated process interruption")

    monkeypatch.setattr(pipeline.promotion, "promote", interrupt_promotion)
    with pytest.raises(SystemExit, match="simulated process interruption"):
        pipeline.process(item_id)

    interrupted = pipeline.repository.begin_revision(
        item_id,
        tmp_path / "artifacts" / "v2" / "slides",
    )
    assert interrupted.state == "promoting"
    interrupted.immutable_source_path.unlink()
    monkeypatch.setattr(pipeline.promotion, "promote", original_promote)

    with pytest.raises(PromotionSourceError, match="upload the file again"):
        pipeline.process(item_id)

    terminal = pipeline.repository.get_study_revision(interrupted.id)
    assert terminal.state == "failed"
    assert pipeline.repository.require_item(item_id).state is UploadState.FAILED


@pytest.mark.parametrize(
    "artifact_name",
    ("immutable_source_path", "immutable_derived_path"),
)
def test_corrupt_immutable_artifact_terminates_promotion_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_name: str,
) -> None:
    pipeline, item_id = _slide_pipeline(tmp_path)
    original_promote = pipeline.promotion.promote
    destinations = build_slide_destinations(
        pipeline.settings,
        LectureKey("Neuro", 1, 1, "Seizures"),
    )
    canonical = (destinations.source, destinations.pdf, destinations.icloud_pdf)
    for destination in canonical:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"old")

    def interrupt_promotion(pairs, revision_id, _commit, _claim):
        for _, destination in pairs:
            verified_atomic_copy(
                destination,
                pipeline.promotion.backup_path(destination, revision_id),
            )
        verified_atomic_copy(*pairs[0])
        raise SystemExit("simulated process interruption")

    monkeypatch.setattr(pipeline.promotion, "promote", interrupt_promotion)
    with pytest.raises(SystemExit, match="simulated process interruption"):
        pipeline.process(item_id)

    interrupted = pipeline.repository.begin_revision(
        item_id,
        tmp_path / "artifacts" / "v2" / "slides",
    )
    assert interrupted.state == "promoting"
    assert canonical[0].read_bytes() != b"old"
    corrupted = getattr(interrupted, artifact_name)
    assert isinstance(corrupted, Path)
    original_payload = pipeline.repository.require_item(item_id).staged_path.read_bytes()
    corrupted.write_bytes(b"corrupt immutable artifact")
    monkeypatch.setattr(pipeline.promotion, "promote", original_promote)

    with pytest.raises(PromotionSourceError, match="checksum mismatch"):
        pipeline.process(item_id)

    terminal = pipeline.repository.get_study_revision(interrupted.id)
    assert terminal.state == "failed"
    assert pipeline.repository.require_item(item_id).state is UploadState.FAILED
    for destination in canonical:
        assert destination.read_bytes() == b"old"
        assert not pipeline.promotion.backup_path(destination, terminal.id).exists()

    retry_path = tmp_path / f"retry-{artifact_name}.pptx"
    retry_path.write_bytes(original_payload)
    retry_batch = pipeline.repository.create_batch(UploadKind.SLIDES)
    retry_id = f"retry-{artifact_name}"
    pipeline.repository.add_item(
        UploadKind.SLIDES,
        StagedUpload(
            batch_id=retry_batch,
            item_id=retry_id,
            path=retry_path,
            sha256=hashlib.sha256(original_payload).hexdigest(),
            size_bytes=len(original_payload),
            original_filename="retry.pptx",
        ),
    )
    lecture_id = pipeline.repository.require_item(item_id).lecture_id
    assert lecture_id is not None
    pipeline.repository.set_manual_assignment(retry_id, lecture_id)

    repaired = pipeline.process(retry_id)

    assert repaired.current is True
    assert repaired.immutable_source_path.read_bytes() == original_payload
    assert pipeline.repository.require_item(retry_id).state is UploadState.COMPLETE
