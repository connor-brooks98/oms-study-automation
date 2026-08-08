import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pypdf import PdfWriter

import oms_hub.files.promotion as promotion_module
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
from oms_hub.files.office import OfficeTimeoutError, OfficeUnavailableError
from oms_hub.files.promotion import PromotionRecoveryError, PromotionSourceError
from oms_hub.ingestion.domain import StagedUpload, UploadKind, UploadState
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.ingestion.worker import IngestionWorker
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
    damaged = getattr(current, artifact_name)
    assert isinstance(damaged, Path)
    damaged.write_bytes(b"corrupt filed artifact")

    staged = tmp_path / "repair-slide.pptx"
    payload = current.immutable_source_path.read_bytes()
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
    assert (
        current.canonical_source_path.read_bytes()
        == current.immutable_source_path.read_bytes()
    )
    assert (
        current.canonical_derived_path.read_bytes()
        == current.immutable_derived_path.read_bytes()
    )
    assert (
        current.icloud_path.read_bytes()
        == current.immutable_derived_path.read_bytes()
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
    assert (
        pipeline.repository.require_item("recovery-duplicate").state
        is UploadState.COMPLETE
    )
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

    def interrupt_promotion(_pairs, _revision_id, _commit):
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
