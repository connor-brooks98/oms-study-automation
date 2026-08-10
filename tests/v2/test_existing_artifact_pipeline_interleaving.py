"""Writer-fence regressions exercised through the production pipelines."""

# ruff: noqa: E501

import hashlib
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread

import pytest
from pypdf import PdfWriter
from reportlab.pdfgen.canvas import Canvas

from oms_hub.artifact_writes import (
    ArtifactWriteClaimLost,
    ArtifactWriteContended,
    ArtifactWriteCoordinator,
)
from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.domain import LectureKey
from oms_hub.existing_artifact_import import (
    ExistingArtifactImporter,
    ExistingArtifactImportRequest,
)
from oms_hub.ingestion.domain import StagedUpload, UploadKind, UploadState
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.ingestion.worker import IngestionWorker
from oms_hub.llm.domain import CleanResult, ProviderName
from oms_hub.models import (
    IngestionJobModel,
    StudyRevisionModel,
    UploadBatchModel,
    UploadItemModel,
)
from oms_hub.repositories import CatalogRepository, LectureInput
from oms_hub.routing import build_transcript_destination
from oms_hub.slides.pipeline import SlidePipeline
from oms_hub.transcripts.pipeline import TranscriptPipeline
from oms_hub.transcripts.prompt import ApprovedPrompt


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _PdfConverter:
    def convert(self, _source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        with destination.open("wb") as stream:
            writer.write(stream)


class _Prompt:
    def current(self) -> ApprovedPrompt:
        return ApprovedPrompt("keep content", "a" * 64)


class _Cleaner:
    def clean(self, raw_text: str, _prompt: ApprovedPrompt) -> CleanResult:
        return CleanResult(raw_text, ProviderName.OPENAI, "test", "request", 1, 1, 0)


class _UnfencedClaim:
    def assert_owned(self) -> None:
        return None


class _CheckpointWrites:
    @contextmanager
    def claim(self, _lecture_id: int, _purpose: str):
        yield _UnfencedClaim()


class _RecoveryClaim:
    def __init__(self, on_loss) -> None:  # type: ignore[no-untyped-def]
        self.calls = 0
        self.on_loss = on_loss

    def assert_owned(self) -> None:
        self.calls += 1
        if self.calls == 7:
            self.on_loss()
            raise ArtifactWriteClaimLost("successor replaced slide recovery owner")


class _RecoveryWrites:
    def __init__(self, claim: _RecoveryClaim) -> None:
        self._claim = claim

    @contextmanager
    def claim(self, _lecture_id: int, _purpose: str):
        yield self._claim


class _LostAfterOtherWriter:
    def __init__(self, ready: Event, other_done: Event) -> None:
        self.ready = ready
        self.other_done = other_done
        self.calls = 0

    def assert_owned(self) -> None:
        self.calls += 1
        if self.calls == 1:
            self.ready.set()
            assert self.other_done.wait(5)
            raise ArtifactWriteClaimLost("A writer was replaced")


class _OwnedClaim:
    def assert_owned(self) -> None:
        return None


def _outline(path: Path) -> None:
    canvas = Canvas(str(path))
    for line in ("CORE CONCEPTS", "One", "DEPTH MAP", "Two", "PROFESSOR EMPHASIS FLAGS", "Three"):
        canvas.drawString(72, 720, line)
        canvas.translate(0, -24)
    canvas.save()


def _prepared_import(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        study_root=tmp_path / "study",
        icloud_staging_root=tmp_path / "icloud",
        transcript_min_clean_ratio=0.1,
        transcript_max_clean_ratio=2.0,
    )
    database = Database(settings.database_url)
    database.migrate()
    lecture_id = CatalogRepository(database).upsert_lecture(
        LectureInput("Neuro", 1, 1, "Synapse", "", None)
    )
    repository = IngestionRepository(database)
    slide_staged = tmp_path / "slide.pptx"
    slide_staged.write_bytes(b"slide source")
    batch = repository.create_batch(UploadKind.SLIDES)
    repository.add_item(
        UploadKind.SLIDES,
        StagedUpload(
            batch,
            "slide",
            slide_staged,
            _digest(slide_staged),
            slide_staged.stat().st_size,
            "slide.pptx",
        ),
    )
    repository.set_manual_assignment("slide", lecture_id)
    slides = SlidePipeline(database, settings, _PdfConverter())
    slide = slides.process("slide")
    transcript = tmp_path / "imported.txt"
    transcript.write_text("Imported cleaned transcript.", encoding="utf-8")
    outline = tmp_path / "imported-outline.pdf"
    _outline(outline)
    importer = ExistingArtifactImporter(database, settings)
    request = ExistingArtifactImportRequest(
        lecture_id,
        slide.id,
        slide.source_sha256,
        slide.derived_sha256 or "",
        transcript,
        _digest(transcript),
        outline,
        _digest(outline),
    )
    return database, settings, repository, slides, importer, request


def _add_transcript(
    repository: IngestionRepository, root: Path, lecture_id: int, item_id: str
) -> None:
    staged = root / f"{item_id}.txt"
    staged.write_text("A separate complete transcript.", encoding="utf-8")
    batch = repository.create_batch(UploadKind.TRANSCRIPTS)
    repository.add_item(
        UploadKind.TRANSCRIPTS,
        StagedUpload(batch, item_id, staged, _digest(staged), staged.stat().st_size, staged.name),
    )
    repository.set_manual_assignment(item_id, lecture_id)
    # This is the worker retry path after a job was already queued when a
    # concurrent importer commits.  The normal duplicate gate intentionally
    # prevents creating this job after that commit.
    if repository.count_jobs(item_id, "process"):
        return
    with repository.database.session() as session:
        item = session.get(UploadItemModel, item_id)
        assert item is not None
        item.state = UploadState.QUEUED.value
        session.add(IngestionJobModel(upload_item_id=item_id, action="process", state="queued"))


def test_importer_claim_contends_real_slide_repair_then_stale_import_is_revalidated(
    tmp_path: Path,
) -> None:
    database, settings, repository, slides, importer, request = _prepared_import(tmp_path)
    current = repository.get_study_revision(request.slides_revision_id)
    assert current.canonical_derived_path is not None
    current.canonical_derived_path.write_bytes(b"damaged canonical PDF")
    duplicate = tmp_path / "repair.pptx"
    duplicate.write_bytes(current.immutable_source_path.read_bytes())
    batch = repository.create_batch(UploadKind.SLIDES)
    repository.add_item(
        UploadKind.SLIDES,
        StagedUpload(
            batch, "repair", duplicate, _digest(duplicate), duplicate.stat().st_size, duplicate.name
        ),
    )
    repository.set_manual_assignment("repair", request.lecture_id)
    before = current.canonical_derived_path.read_bytes()
    with importer.writes.claim(request.lecture_id, "test-import-holder"):
        with pytest.raises(ArtifactWriteContended):
            slides.process("repair")
    assert current.canonical_derived_path.read_bytes() == before
    assert repository.get_study_revision(request.slides_revision_id).current is True
    assert repository.require_item("repair").state is UploadState.PROCESSING
    assert repository.recover_interrupted_jobs() == 1
    assert repository.require_item("repair").state is UploadState.QUEUED
    repaired = slides.process("repair")
    assert repaired.current is True
    assert (
        current.canonical_derived_path.read_bytes() == repaired.immutable_derived_path.read_bytes()
    )

    # The importer request is pinned to the pre-repair filed PDF.  A filed
    # mutation is therefore rejected at its lock-time slide revalidation.
    current.canonical_derived_path.write_bytes(b"not the pinned PDF")
    with pytest.raises(Exception, match="slides revision"):
        importer.import_artifacts(request)
    database.close()


def test_importer_claim_contends_interrupted_slide_recovery_without_canonical_mutation(
    tmp_path: Path,
) -> None:
    database, _settings, repository, slides, importer, request = _prepared_import(tmp_path)
    revision = repository.get_study_revision(request.slides_revision_id)
    pairs = slides._persisted_promotion_pairs(revision, revision.immutable_derived_path)  # noqa: SLF001
    with database.session() as session:
        stored = session.get(StudyRevisionModel, revision.id)
        assert stored is not None
        stored.current = False
        stored.state = "promoting"
    for _source, destination in pairs:
        destination.write_bytes(b"interrupted canonical bytes")
        slides.promotion.backup_path(destination, revision.id).write_bytes(b"prior canonical bytes")
    before = tuple(destination.read_bytes() for _source, destination in pairs)
    with importer.writes.claim(request.lecture_id, "import-recovery-holder"):
        with pytest.raises(ArtifactWriteContended):
            slides.process("slide")
    assert tuple(destination.read_bytes() for _source, destination in pairs) == before
    recovered = repository.get_study_revision(revision.id)
    assert recovered.state == "promoting"
    assert recovered.current is False
    assert repository.require_item("slide").state is UploadState.PROCESSING
    assert repository.recover_interrupted_jobs() == 1
    assert repository.require_item("slide").state is UploadState.QUEUED
    database.close()


def test_slide_recovery_claim_loss_cannot_restore_over_successor_current_bytes(
    tmp_path: Path,
) -> None:
    database, _settings, repository, slides, _importer, request = _prepared_import(tmp_path)
    revision = repository.get_study_revision(request.slides_revision_id)
    pairs = slides._persisted_promotion_pairs(revision, revision.immutable_derived_path)  # noqa: SLF001
    with database.session() as session:
        stored = session.get(StudyRevisionModel, revision.id)
        assert stored is not None
        stored.current = False
        stored.state = "promoting"
        session.add(UploadBatchModel(id="successor-batch", kind="slides", state="complete"))
        session.add(
            UploadItemModel(
                id="successor-item",
                batch_id="successor-batch",
                kind="slides",
                original_filename="successor.pptx",
                staged_path="/successor.pptx",
                sha256="f" * 64,
                size_bytes=1,
                state="complete",
                lecture_id=request.lecture_id,
                confidence=1,
                manual_assignment=True,
            )
        )
        session.flush()
        successor = StudyRevisionModel(
            upload_item_id="successor-item",
            lecture_id=request.lecture_id,
            kind="slides",
            source_sha256="f" * 64,
            immutable_source_path="/successor.pptx",
            derived_sha256="e" * 64,
            immutable_derived_path="/successor.pdf",
            canonical_source_path=str(pairs[0][1]),
            canonical_derived_path=str(pairs[1][1]),
            icloud_path=str(pairs[2][1]),
            state="proposed",
            current=False,
        )
        session.add(successor)
        session.flush()
        successor_id = successor.id
    for _source, destination in pairs:
        destination.write_bytes(b"partial canonical bytes")
        slides.promotion.backup_path(destination, revision.id).write_bytes(b"prior canonical bytes")

    def successor_wins() -> None:
        for _source, destination in pairs:
            destination.write_bytes(b"successor canonical bytes")
        with database.session() as session:
            stored = session.get(StudyRevisionModel, successor_id)
            assert stored is not None
            stored.current = True
            stored.state = "current"

    slides.writes = _RecoveryWrites(_RecoveryClaim(successor_wins))
    with pytest.raises(ArtifactWriteClaimLost, match="successor replaced slide recovery owner"):
        slides.process("slide")
    assert {destination.read_bytes() for _source, destination in pairs} == {
        b"successor canonical bytes"
    }
    assert repository.get_study_revision(revision.id).state == "promoting"
    successor = repository.get_study_revision(successor_id)
    assert successor.current is True
    assert successor.state == "current"
    database.close()


def test_one_slide_pipeline_interleaves_distinct_lecture_claims_without_cross_talk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, _settings, repository, slides, _importer, request = _prepared_import(tmp_path)
    lecture_b = CatalogRepository(database).upsert_lecture(
        LectureInput("Neuro", 1, 2, "Second", "", None)
    )
    staged_b = tmp_path / "second.pptx"
    staged_b.write_bytes(b"second slide source")
    batch_b = repository.create_batch(UploadKind.SLIDES)
    repository.add_item(
        UploadKind.SLIDES,
        StagedUpload(batch_b, "second", staged_b, _digest(staged_b), staged_b.stat().st_size, staged_b.name),
    )
    repository.set_manual_assignment("second", lecture_b)
    revision_b = slides.process("second")
    revision_a = repository.get_study_revision(request.slides_revision_id)
    assert revision_a.immutable_derived_path is not None
    assert revision_b.immutable_derived_path is not None
    assert revision_a.canonical_derived_path is not None
    successor_bytes = b"A successor canonical bytes"
    revision_a.canonical_derived_path.write_bytes(successor_bytes)
    used_claims: list[object] = []
    original_promote = slides.promotion.promote

    def promote_with_trace(pairs, revision_id, commit, claim):  # type: ignore[no-untyped-def]
        used_claims.append(claim)
        return original_promote(pairs, revision_id, commit, claim)

    monkeypatch.setattr(slides.promotion, "promote", promote_with_trace)
    a_ready, b_done = Event(), Event()
    lost_a = _LostAfterOtherWriter(a_ready, b_done)
    owned_b = _OwnedClaim()
    errors: list[BaseException] = []

    def repair_a() -> None:
        try:
            slides._repair_current_revision(  # noqa: SLF001
                repository.require_item("slide").staged_path,
                revision_a,
                revision_a.immutable_derived_path,
                lost_a,
            )
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=repair_a)
    thread.start()
    assert a_ready.wait(5)
    repaired_b = slides._repair_current_revision(  # noqa: SLF001
        repository.require_item("second").staged_path,
        revision_b,
        revision_b.immutable_derived_path,
        owned_b,
    )
    b_done.set()
    thread.join(5)
    assert errors and isinstance(errors[0], ArtifactWriteClaimLost)
    assert used_claims == [lost_a, owned_b]
    assert revision_a.canonical_derived_path.read_bytes() == successor_bytes
    assert repaired_b.current is True
    assert repository.get_study_revision(revision_a.id).current is True
    assert repository.get_study_revision(revision_b.id).current is True
    database.close()


def test_transcript_pipeline_rechecks_after_import_and_worker_defers_contention(
    tmp_path: Path,
) -> None:
    database, settings, repository, _slides, importer, request = _prepared_import(tmp_path)
    imported = importer.import_artifacts(request)
    _add_transcript(repository, tmp_path, request.lecture_id, "candidate")
    pipeline = TranscriptPipeline(database, settings, _Prompt(), _Cleaner())
    started = datetime(2026, 8, 9, tzinfo=UTC)
    moments = iter((started, started + timedelta(seconds=5)))
    worker = IngestionWorker(repository, pipeline, pipeline, now=lambda: next(moments))
    with ArtifactWriteCoordinator(database, settings).claim(request.lecture_id, "holder"):
        assert worker.run_once() is True
    assert repository.require_item("candidate").state is UploadState.QUEUED
    assert worker.run_once() is True
    proposed = repository.get_study_revision(
        next(
            revision.id
            for revision in repository.list_proposed_revisions()
            if revision.upload_item_id == "candidate"
        )
    )
    assert proposed.current is False
    assert proposed.state == "proposed"
    assert imported.transcript_path.read_text(encoding="utf-8") == "Imported cleaned transcript."
    database.close()


def test_transcript_post_copy_current_collision_restores_prior_canonical_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, settings, repository, _slides, _importer, request = _prepared_import(tmp_path)
    _add_transcript(repository, tmp_path, request.lecture_id, "candidate")
    lecture = CatalogRepository(database).get_lecture(request.lecture_id)
    assert lecture is not None
    destination = build_transcript_destination(
        settings,
        LectureKey(lecture.subject, lecture.exam_number, lecture.lecture_number, lecture.topic),
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("prior canonical transcript", encoding="utf-8")
    pipeline = TranscriptPipeline(
        database, settings, _Prompt(), _Cleaner(), artifact_writes=_CheckpointWrites()
    )
    original_current_check = pipeline.repository.has_other_current_revision
    checks = 0

    def current_after_successor_row(lecture_id: int, kind: UploadKind, revision_id: int) -> bool:
        nonlocal checks
        checks += 1
        if checks == 2:
            # A successor only wins the DB current row.  It does not touch the
            # canonical file, so the candidate must restore its pre-copy bytes.
            with database.session() as session:
                session.add(UploadBatchModel(id="successor-batch", kind="transcripts", state="complete"))
                session.add(
                    UploadItemModel(
                        id="successor-item",
                        batch_id="successor-batch",
                        kind="transcripts",
                        original_filename="successor.txt",
                        staged_path="/successor.txt",
                        sha256="f" * 64,
                        size_bytes=1,
                        state="complete",
                        lecture_id=lecture_id,
                        confidence=1,
                        manual_assignment=True,
                    )
                )
                session.flush()
                session.add(
                    StudyRevisionModel(
                        upload_item_id="successor-item",
                        lecture_id=lecture_id,
                        kind="transcripts",
                        source_sha256="f" * 64,
                        immutable_source_path="/successor.txt",
                        derived_sha256="f" * 64,
                        immutable_derived_path="/successor.txt",
                        canonical_source_path=str(destination),
                        canonical_derived_path=str(destination),
                        state="current",
                        current=True,
                    )
                )
        return original_current_check(lecture_id, kind, revision_id)

    monkeypatch.setattr(
        pipeline.repository, "has_other_current_revision", current_after_successor_row
    )
    proposed = pipeline.process("candidate")
    assert proposed.current is False
    assert proposed.state == "proposed"
    assert destination.read_text(encoding="utf-8") == "prior canonical transcript"
    assert tuple(destination.parent.glob(f".{destination.name}.oms-backup-*")) == ()
    successor = next(
        revision
        for revision in repository.list_current_revisions(request.lecture_id)
        if revision.kind is UploadKind.TRANSCRIPTS
    )
    assert successor.upload_item_id == "successor-item"
    database.close()


def test_transcript_owned_db_failure_restores_canonical_and_cleans_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, settings, repository, _slides, _importer, request = _prepared_import(tmp_path)
    _add_transcript(repository, tmp_path, request.lecture_id, "candidate")
    lecture = CatalogRepository(database).get_lecture(request.lecture_id)
    assert lecture is not None
    destination = build_transcript_destination(
        settings,
        LectureKey(lecture.subject, lecture.exam_number, lecture.lecture_number, lecture.topic),
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("prior canonical transcript", encoding="utf-8")
    pipeline = TranscriptPipeline(
        database, settings, _Prompt(), _Cleaner(), artifact_writes=_CheckpointWrites()
    )
    original_finish = pipeline.repository.finish_revision
    calls = 0

    def fail_once(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("finish revision failed")
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(pipeline.repository, "finish_revision", fail_once)
    with pytest.raises(RuntimeError, match="finish revision failed"):
        pipeline.process("candidate")
    assert destination.read_text(encoding="utf-8") == "prior canonical transcript"
    backups = tuple(destination.parent.glob(f".{destination.name}.oms-backup-*"))
    assert backups == ()
    database.close()


def test_transcript_claim_loss_after_copy_preserves_successor_and_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, settings, repository, _slides, _importer, request = _prepared_import(tmp_path)
    _add_transcript(repository, tmp_path, request.lecture_id, "candidate")
    lecture = CatalogRepository(database).get_lecture(request.lecture_id)
    assert lecture is not None
    destination = build_transcript_destination(
        settings,
        LectureKey(lecture.subject, lecture.exam_number, lecture.lecture_number, lecture.topic),
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("prior canonical transcript", encoding="utf-8")

    class _LostAfterDbFailure:
        def __init__(self) -> None:
            self.calls = 0

        def assert_owned(self) -> None:
            self.calls += 1
            if self.calls >= 5:
                destination.write_text("successor canonical transcript", encoding="utf-8")
                raise ArtifactWriteClaimLost("successor owns transcript")

    class _LostWrites:
        def __init__(self) -> None:
            self.claim_value = _LostAfterDbFailure()

        @contextmanager
        def claim(self, _lecture_id: int, _purpose: str):
            yield self.claim_value

    pipeline = TranscriptPipeline(
        database, settings, _Prompt(), _Cleaner(), artifact_writes=_LostWrites()
    )
    original_finish = pipeline.repository.finish_revision
    calls = 0

    def fail_once(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("finish revision failed")
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(pipeline.repository, "finish_revision", fail_once)
    with pytest.raises(ArtifactWriteClaimLost, match="successor owns transcript"):
        pipeline.process("candidate")
    assert destination.read_text(encoding="utf-8") == "successor canonical transcript"
    backups = tuple(destination.parent.glob(f".{destination.name}.oms-backup-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "prior canonical transcript"
    database.close()
