import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from reportlab.pdfgen.canvas import Canvas
from sqlalchemy import select

from oms_hub import cli
from oms_hub.anki.models import AnkiCurationJobModel
from oms_hub.artifact_writes import (
    ArtifactWriteClaimLost,
    ArtifactWriteContended,
    ArtifactWriteCoordinator,
)
from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.existing_artifact_import import (
    A0_CLEANED_TRANSCRIPT_SHA256,
    A0_OUTLINE_SHA256,
    A0_PDF_SHA256,
    A0_PPTX_SHA256,
    ExistingArtifactImporter,
    ExistingArtifactImportError,
    ExistingArtifactImportRequest,
    ExistingArtifactRecoveryError,
    verify_a0_operator_files,
)
from oms_hub.files.handle_relative import set_hardened_write_hook
from oms_hub.ingestion.domain import UploadKind
from oms_hub.models import (
    ExistingArtifactImportModel,
    GenerationJobModel,
    OutlineOutputModel,
    StudyRevisionModel,
    StudyUsageModel,
    UploadBatchModel,
    UploadItemModel,
)
from oms_hub.repositories import CatalogRepository, LectureInput
from oms_hub.routing import build_outline_destination


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_outline(path: Path, lines: tuple[str, ...] | None = None) -> None:
    canvas = Canvas(str(path))
    for line in lines or (
        "CORE CONCEPTS",
        "One",
        "DEPTH MAP",
        "Two",
        "PROFESSOR EMPHASIS FLAGS",
        "Three",
    ):
        canvas.drawString(72, 720, line)
        canvas.translate(0, -24)
    canvas.save()


def _request_fixture(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        study_root=tmp_path / "study",
        icloud_staging_root=tmp_path / "icloud",
    )
    database = Database(settings.database_url)
    database.migrate()
    lecture_id = CatalogRepository(database).upsert_lecture(
        LectureInput("Neuro", 1, 24, "Synapse", "", None)
    )
    immutable_pptx = settings.data_dir / "artifacts" / "v2" / "slides" / "source.pptx"
    immutable_pdf = settings.data_dir / "artifacts" / "v2" / "slides" / "source.pdf"
    canonical_pptx = settings.study_root / "Neuro" / "slides.pptx"
    canonical_pdf = settings.study_root / "Neuro" / "slides.pdf"
    for path, payload in ((immutable_pptx, b"pptx"), (immutable_pdf, b"pdf")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    canonical_pptx.parent.mkdir(parents=True, exist_ok=True)
    canonical_pptx.write_bytes(immutable_pptx.read_bytes())
    canonical_pdf.write_bytes(immutable_pdf.read_bytes())
    source_sha = _sha256(immutable_pptx)
    pdf_sha = _sha256(immutable_pdf)
    with database.session() as session:
        session.add(UploadBatchModel(id="slide-batch", kind="slides", state="complete"))
        session.flush()
        session.add(
            UploadItemModel(
                id="slide-item",
                batch_id="slide-batch",
                kind="slides",
                original_filename="slides.pptx",
                staged_path="unused",
                sha256=source_sha,
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
                upload_item_id="slide-item",
                lecture_id=lecture_id,
                kind="slides",
                source_sha256=source_sha,
                immutable_source_path=str(immutable_pptx),
                derived_sha256=pdf_sha,
                immutable_derived_path=str(immutable_pdf),
                canonical_source_path=str(canonical_pptx),
                canonical_derived_path=str(canonical_pdf),
                state="current",
                current=True,
            )
        )
    transcript = tmp_path / "cleaned.txt"
    transcript.write_text("clean text", encoding="utf-8")
    outline = tmp_path / "outline.pdf"
    _write_outline(outline)
    request = ExistingArtifactImportRequest(
        lecture_id,
        1,
        source_sha,
        pdf_sha,
        transcript,
        _sha256(transcript),
        outline,
        _sha256(outline),
    )
    return database, settings, ExistingArtifactImporter(database, settings), request


def _managed_artifact_files(settings: Settings) -> tuple[tuple[str, bytes], ...]:
    files: list[tuple[str, bytes]] = []
    for root in (settings.data_dir / "artifacts", settings.study_root):
        if root.exists():
            files.extend(
                (str(path.relative_to(root)), path.read_bytes())
                for path in root.rglob("*")
                if path.is_file()
            )
    return tuple(sorted(files))


def _import_state(database: Database, settings: Settings) -> tuple[object, ...]:
    with database.session() as session:
        return (
            session.query(ExistingArtifactImportModel).count(),
            session.query(UploadBatchModel).count(),
            session.query(UploadItemModel).count(),
            tuple(
                session.scalars(
                    select(StudyRevisionModel.id).where(
                        StudyRevisionModel.current.is_(True),
                        StudyRevisionModel.kind == UploadKind.TRANSCRIPTS.value,
                    )
                )
            ),
            tuple(
                session.scalars(
                    select(OutlineOutputModel.id).where(OutlineOutputModel.current.is_(True))
                )
            ),
            _managed_artifact_files(settings),
        )


def _assert_rejected_without_writes(
    database: Database,
    settings: Settings,
    importer: ExistingArtifactImporter,
    request: ExistingArtifactImportRequest,
) -> None:
    before = _import_state(database, settings)
    with pytest.raises(ExistingArtifactImportError):
        importer.import_artifacts(request)
    assert _import_state(database, settings) == before


def _failed_audit(database: Database) -> ExistingArtifactImportModel:
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        assert audit.status == "failed"
        assert audit.error
        return audit


def _generation_counts(database: Database) -> tuple[int, int]:
    with database.session() as session:
        return (
            session.query(GenerationJobModel).count(),
            session.query(StudyUsageModel).count(),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("slides_source_sha256", "A" * 64),
        ("slides_pdf_sha256", "a"),
        ("cleaned_transcript_sha256", "0" * 64),
        ("notebooklm_outline_sha256", "0" * 64),
    ],
)
def test_import_rejects_hash_syntax_or_mismatch_without_writes(tmp_path, field, value):
    database, settings, importer, request = _request_fixture(tmp_path)
    _assert_rejected_without_writes(
        database, settings, importer, replace(request, **{field: value})
    )


@pytest.mark.parametrize(
    "case",
    [
        "relative",
        "symlink",
        "wrong-suffix",
        "directory",
        "oversize-transcript",
        "oversize-outline",
        "invalid-utf8",
        "empty-transcript",
        "data-envelope",
        "malformed-pdf",
        "missing-headings",
    ],
)
def test_import_rejects_invalid_public_files_without_writes(tmp_path, case):
    database, settings, importer, request = _request_fixture(tmp_path)
    if case == "relative":
        request = replace(request, cleaned_transcript=Path(request.cleaned_transcript.name))
    elif case == "symlink":
        linked = tmp_path / "linked.txt"
        linked.symlink_to(request.cleaned_transcript)
        request = replace(
            request, cleaned_transcript=linked, cleaned_transcript_sha256=_sha256(linked)
        )
    elif case == "wrong-suffix":
        transcript = tmp_path / "cleaned.md"
        transcript.write_bytes(request.cleaned_transcript.read_bytes())
        request = replace(
            request, cleaned_transcript=transcript, cleaned_transcript_sha256=_sha256(transcript)
        )
    elif case == "directory":
        directory = tmp_path / "not-a-file.txt"
        directory.mkdir()
        request = replace(request, cleaned_transcript=directory)
    elif case == "oversize-transcript":
        transcript = tmp_path / "too-large.txt"
        transcript.write_bytes(b"x" * (request.notebooklm_outline.stat().st_size + 1))
        importer.settings = settings.model_copy(
            update={"max_upload_file_bytes": request.notebooklm_outline.stat().st_size}
        )
        request = replace(
            request, cleaned_transcript=transcript, cleaned_transcript_sha256=_sha256(transcript)
        )
    elif case == "oversize-outline":
        importer.settings = settings.model_copy(
            update={"max_upload_file_bytes": request.cleaned_transcript.stat().st_size}
        )
    elif case == "invalid-utf8":
        transcript = tmp_path / "invalid.txt"
        transcript.write_bytes(b"\xff\xfe")
        request = replace(
            request, cleaned_transcript=transcript, cleaned_transcript_sha256=_sha256(transcript)
        )
    elif case == "empty-transcript":
        transcript = tmp_path / "empty.txt"
        transcript.write_bytes(b" \n\t")
        request = replace(
            request, cleaned_transcript=transcript, cleaned_transcript_sha256=_sha256(transcript)
        )
    elif case == "data-envelope":
        transcript = tmp_path / "envelope.txt"
        transcript.write_text('{"content":"not a transcript"}', encoding="utf-8")
        request = replace(
            request, cleaned_transcript=transcript, cleaned_transcript_sha256=_sha256(transcript)
        )
    elif case == "malformed-pdf":
        outline = tmp_path / "malformed.pdf"
        outline.write_bytes(b"not a PDF")
        request = replace(
            request, notebooklm_outline=outline, notebooklm_outline_sha256=_sha256(outline)
        )
    else:
        outline = tmp_path / "missing-headings.pdf"
        _write_outline(outline, ("CORE CONCEPTS", "One"))
        request = replace(
            request, notebooklm_outline=outline, notebooklm_outline_sha256=_sha256(outline)
        )
    _assert_rejected_without_writes(database, settings, importer, request)


@pytest.mark.parametrize("case", ["missing", "wrong-lecture", "wrong-kind", "non-current", "hash"])
def test_import_rejects_invalid_lecture_or_slide_pin_without_writes(tmp_path, case):
    database, settings, importer, request = _request_fixture(tmp_path)
    if case == "missing":
        request = replace(request, lecture_id=999)
    elif case == "wrong-lecture":
        lecture_id = CatalogRepository(database).upsert_lecture(
            LectureInput("Neuro", 1, 25, "Other", "", None)
        )
        request = replace(request, lecture_id=lecture_id)
    elif case == "hash":
        request = replace(request, slides_pdf_sha256="c" * 64)
    else:
        with database.session() as session:
            slide = session.get(StudyRevisionModel, request.slides_revision_id)
            assert slide is not None
            if case == "wrong-kind":
                slide.kind = UploadKind.TRANSCRIPTS.value
            else:
                slide.current = False
    _assert_rejected_without_writes(database, settings, importer, request)


def test_import_rejects_destination_escape_without_writes(tmp_path):
    database, settings, _, request = _request_fixture(tmp_path)
    importer = ExistingArtifactImporter(
        database,
        settings,
        destination_resolver=lambda _settings, _lecture: (
            tmp_path / "outside" / "cleaned.txt",
            build_outline_destination(settings, _lecture),
        ),
    )
    _assert_rejected_without_writes(database, settings, importer, request)


def _seed_current_artifacts(database: Database, lecture_id: int, kind: str) -> None:
    with database.session() as session:
        if kind in {"transcript", "both"}:
            session.add(
                UploadBatchModel(id="transcript-batch", kind="transcripts", state="complete")
            )
            session.flush()
            session.add(
                UploadItemModel(
                    id="transcript-item",
                    batch_id="transcript-batch",
                    kind="transcripts",
                    original_filename="current.txt",
                    staged_path="unused",
                    sha256="c" * 64,
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
                    upload_item_id="transcript-item",
                    lecture_id=lecture_id,
                    kind="transcripts",
                    source_sha256="c" * 64,
                    immutable_source_path="unused",
                    derived_sha256="c" * 64,
                    immutable_derived_path="unused",
                    canonical_derived_path="unused",
                    state="current",
                    current=True,
                )
            )
        if kind in {"outline", "both"}:
            session.add(
                OutlineOutputModel(
                    lecture_id=lecture_id,
                    job_id=None,
                    path="unused",
                    sha256="d" * 64,
                    current=True,
                )
            )


@pytest.mark.parametrize("kind", ["transcript", "outline", "both"])
def test_import_rejects_existing_current_artifacts_without_writes(tmp_path, kind):
    database, settings, importer, request = _request_fixture(tmp_path)
    _seed_current_artifacts(database, request.lecture_id, kind)
    _assert_rejected_without_writes(database, settings, importer, request)


def test_import_existing_artifacts_is_honest_and_idempotent(tmp_path):
    database, settings, importer, request = _request_fixture(tmp_path)
    first = importer.import_artifacts(request)
    second = importer.import_artifacts(request)

    assert first.status == "complete"
    assert not first.idempotent
    assert second.idempotent
    assert second.transcript_revision_id == first.transcript_revision_id
    assert first.transcript_path.read_bytes() == request.cleaned_transcript.read_bytes()
    assert first.outline_path.read_bytes() == request.notebooklm_outline.read_bytes()
    assert _import_state(database, settings)[:5] == (
        1,
        2,
        2,
        (first.transcript_revision_id,),
        (first.outline_id,),
    )
    assert _generation_counts(database) == (0, 0)


@pytest.mark.parametrize("phase", ["post-initial-validation", "post-lock-revalidation"])
def test_source_change_is_revalidated_without_unsafe_import_writes(tmp_path, phase):
    database, settings, importer, request = _request_fixture(tmp_path)

    def checkpoint(current: str) -> None:
        if current == phase:
            request.cleaned_transcript.write_text("changed after validation", encoding="utf-8")

    importer.checkpoint = checkpoint
    before = _import_state(database, settings)
    with pytest.raises(ExistingArtifactImportError, match="(?:SHA-256|checksum)"):
        importer.import_artifacts(request)
    if phase == "post-initial-validation":
        assert _import_state(database, settings) == before
    else:
        audit = _failed_audit(database)
        assert audit.attempts == 1
        assert _import_state(database, settings)[1:] == before[1:]


@pytest.mark.parametrize("change", ["catalog", "destination"])
def test_precommit_identity_drift_fails_and_cleans_owned_bytes(tmp_path, change):
    database, settings, importer, request = _request_fixture(tmp_path)
    copies = 0
    alternate = False

    def destinations(current_settings, lecture):
        if alternate:
            return (
                current_settings.study_root / "alternate" / "transcript.txt",
                current_settings.study_root / "alternate" / "outline.pdf",
            )
        return (
            build_outline_destination(current_settings, lecture).parent.parent
            / "Transcripts"
            / "placeholder.txt",
            build_outline_destination(current_settings, lecture),
        )

    if change == "destination":
        importer.destination_resolver = destinations

    def checkpoint(phase: str) -> None:
        nonlocal alternate, copies
        if phase != "after-copy":
            return
        copies += 1
        if copies != 4:
            return
        if change == "catalog":
            CatalogRepository(database).update_lecture(
                request.lecture_id,
                LectureInput("Neuro", 1, 24, "Changed", "", None),
            )
        else:
            alternate = True

    importer.checkpoint = checkpoint
    before = _import_state(database, settings)
    with pytest.raises(ExistingArtifactImportError, match="pinned catalog identity"):
        importer.import_artifacts(request)
    assert _failed_audit(database).attempts == 1
    assert _import_state(database, settings)[1:] == before[1:]


@pytest.mark.parametrize(
    "failure", [("after-copy", 1), ("after-copy", 3), ("during-current-db-commit", 1)]
)
def test_copy_and_db_failures_mark_audit_and_clean_owned_bytes(tmp_path, failure):
    database, settings, importer, request = _request_fixture(tmp_path)
    target_phase, target_count = failure
    calls = 0

    def checkpoint(phase: str) -> None:
        nonlocal calls
        if phase != target_phase:
            return
        calls += 1
        if calls == target_count:
            raise RuntimeError(f"forced {target_phase} failure")

    importer.checkpoint = checkpoint
    before = _import_state(database, settings)
    with pytest.raises(RuntimeError, match="forced"):
        importer.import_artifacts(request)
    audit = _failed_audit(database)
    assert audit.attempts == 1
    assert _import_state(database, settings)[1:] == before[1:]
    assert _generation_counts(database) == (0, 0)


def test_failed_bundle_retry_reuses_audit_and_increments_attempts(tmp_path):
    database, settings, importer, request = _request_fixture(tmp_path)
    failed = False

    def checkpoint(phase: str) -> None:
        nonlocal failed
        if phase == "after-copy" and not failed:
            failed = True
            raise RuntimeError("forced immutable failure")

    importer.checkpoint = checkpoint
    with pytest.raises(RuntimeError, match="forced immutable failure"):
        importer.import_artifacts(request)
    first_audit = _failed_audit(database)
    result = importer.import_artifacts(request)
    assert result.import_id == first_audit.id
    assert result.attempts == 2
    with database.session() as session:
        audit = session.get(ExistingArtifactImportModel, result.import_id)
        assert audit is not None
        assert audit.status == "complete"
        assert audit.attempts == 2
    assert _generation_counts(database) == (0, 0)


@pytest.mark.parametrize("status", ["failed", "preparing"])
@pytest.mark.parametrize("tamper", ["id", "immutable_path"])
def test_resume_rejects_hostile_audit_id_or_path_before_mutation(tmp_path, status, tamper):
    database, settings, importer, request = _request_fixture(tmp_path)
    importer.checkpoint = lambda phase: (
        (_ for _ in ()).throw(RuntimeError("forced")) if phase == "after-copy" else None
    )
    with pytest.raises(RuntimeError, match="forced"):
        importer.import_artifacts(request)
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        audit.status = status
        audit.owner = "original-owner"
        if tamper == "id":
            audit.id = "../outside"
        else:
            audit.immutable_transcript_path = str(tmp_path / "outside.txt")
    before = _import_state(database, settings)
    importer.checkpoint = lambda _phase: None
    with pytest.raises(ExistingArtifactImportError, match="failed import audit"):
        importer.import_artifacts(request)
    assert _import_state(database, settings) == before
    assert not (tmp_path / "outside.txt").exists()


@pytest.mark.parametrize("kind", ["transcript", "outline"])
def test_failed_resume_refuses_intervening_current_artifact_without_copying(tmp_path, kind):
    database, settings, importer, request = _request_fixture(tmp_path)
    failed = False

    def checkpoint(phase: str) -> None:
        nonlocal failed
        if phase == "after-copy" and not failed:
            failed = True
            raise RuntimeError("forced failed audit")

    importer.checkpoint = checkpoint
    with pytest.raises(RuntimeError, match="forced failed audit"):
        importer.import_artifacts(request)
    _seed_current_artifacts(database, request.lecture_id, kind)
    before = _import_state(database, settings)
    importer.checkpoint = lambda _phase: None
    with pytest.raises(ExistingArtifactImportError, match="replacement is unsupported"):
        importer.import_artifacts(request)
    assert _import_state(database, settings) == before


@pytest.mark.parametrize("after_failure", [True, False])
def test_renamed_same_byte_sources_fail_closed_without_reusing_provenance(tmp_path, after_failure):
    database, settings, importer, request = _request_fixture(tmp_path)
    if after_failure:
        importer.checkpoint = lambda phase: (
            (_ for _ in ()).throw(RuntimeError("forced")) if phase == "after-copy" else None
        )
        with pytest.raises(RuntimeError, match="forced"):
            importer.import_artifacts(request)
        importer.checkpoint = lambda _phase: None
    else:
        importer.import_artifacts(request)
    renamed = tmp_path / "renamed-cleaned.txt"
    renamed.write_bytes(request.cleaned_transcript.read_bytes())
    before = _import_state(database, settings)
    with pytest.raises(ExistingArtifactImportError, match="different source filenames"):
        importer.import_artifacts(replace(request, cleaned_transcript=renamed))
    assert _import_state(database, settings) == before


class _FencedClaim:
    def __init__(self) -> None:
        self.owner = "fault-owner"
        self.lost = False

    def assert_owned(self) -> None:
        if self.lost:
            raise ArtifactWriteClaimLost("forced claim loss")


class _FencedWrites:
    def __init__(self) -> None:
        self.current = _FencedClaim()

    @contextmanager
    def claim(self, _lecture_id: int, _purpose: str) -> Iterator[_FencedClaim]:
        yield self.current


@pytest.mark.parametrize(
    "phase,index",
    [
        *(("before-copy", index) for index in range(1, 5)),
        *(("after-copy", index) for index in range(1, 5)),
        ("pre-current-db-commit", 1),
    ],
)
def test_claim_loss_preserves_successor_bytes_and_current_rows(tmp_path, phase, index):
    database, settings, importer, request = _request_fixture(tmp_path)
    writes = _FencedWrites()
    importer.writes = writes
    calls = 0
    successor: Path | None = None
    successor_revision_id: int | None = None

    def checkpoint(current: str) -> None:
        nonlocal calls, successor, successor_revision_id
        if current != phase:
            return
        calls += 1
        if calls != index:
            return
        with database.session() as session:
            audit = session.scalar(select(ExistingArtifactImportModel))
            assert audit is not None
            paths = (
                audit.immutable_transcript_path,
                audit.immutable_outline_path,
                audit.canonical_transcript_path,
                audit.canonical_outline_path,
            )
        successor = Path(paths[min(index, 4) - 1] or "")
        successor.parent.mkdir(parents=True, exist_ok=True)
        successor.write_bytes(b"successor bytes")
        with database.session() as session:
            session.add(
                UploadBatchModel(id="successor-batch", kind="transcripts", state="complete")
            )
            session.flush()
            session.add(
                UploadItemModel(
                    id="successor-item",
                    batch_id="successor-batch",
                    kind="transcripts",
                    original_filename="successor.txt",
                    staged_path=str(successor),
                    sha256="e" * 64,
                    size_bytes=1,
                    state="complete",
                    lecture_id=request.lecture_id,
                    confidence=1,
                    manual_assignment=True,
                )
            )
            session.flush()
            revision = StudyRevisionModel(
                upload_item_id="successor-item",
                lecture_id=request.lecture_id,
                kind="transcripts",
                source_sha256="e" * 64,
                immutable_source_path=str(successor),
                derived_sha256="e" * 64,
                immutable_derived_path=str(successor),
                canonical_derived_path=str(successor),
                state="current",
                current=True,
            )
            session.add(revision)
            session.flush()
            successor_revision_id = revision.id
        writes.current.lost = True

    importer.checkpoint = checkpoint
    before = _import_state(database, settings)
    with pytest.raises(ArtifactWriteClaimLost, match="forced claim loss"):
        importer.import_artifacts(request)
    assert successor is not None
    assert successor.read_bytes() == b"successor bytes"
    assert successor_revision_id is not None
    assert _failed_audit(database).attempts == 1
    state = _import_state(database, settings)
    assert state[1:3] == (before[1] + 1, before[2] + 1)
    assert state[3] == (successor_revision_id,)
    assert state[4] == before[4]


@pytest.mark.parametrize(
    "tamper",
    [
        "audit",
        "catalog",
        "revision-provenance",
        "revision-path",
        "outline-provenance",
        "outline-link",
        "outline-slide",
        "immutable-transcript",
        "canonical-transcript",
        "immutable-outline",
        "canonical-outline",
    ],
)
def test_completed_retry_fails_closed_on_every_persisted_identity_class(tmp_path, tamper):
    database, settings, importer, request = _request_fixture(tmp_path)
    result = importer.import_artifacts(request)
    before = _import_state(database, settings)
    if tamper == "catalog":
        CatalogRepository(database).update_lecture(
            request.lecture_id,
            LectureInput("Neuro", 1, 24, "Changed", "", None),
        )
    elif tamper.endswith("transcript") or tamper.endswith("outline"):
        path = {
            "immutable-transcript": result.immutable_transcript_path,
            "canonical-transcript": result.transcript_path,
            "immutable-outline": result.immutable_outline_path,
            "canonical-outline": result.outline_path,
        }[tamper]
        path.write_bytes(b"tampered bytes")
    else:
        with database.session() as session:
            audit = session.get(ExistingArtifactImportModel, result.import_id)
            revision = session.get(StudyRevisionModel, result.transcript_revision_id)
            outline = session.get(OutlineOutputModel, result.outline_id)
            assert audit is not None and revision is not None and outline is not None
            if tamper == "audit":
                audit.subject = "Tampered"
            elif tamper == "revision-provenance":
                revision.provenance_kind = "llm_cleaned"
            elif tamper == "revision-path":
                revision.canonical_derived_path = "wrong-path"
            elif tamper == "outline-provenance":
                outline.provenance_kind = "notebooklm_generated"
            elif tamper == "outline-link":
                outline.transcript_sha256 = "0" * 64
            else:
                outline.slide_source_sha256 = "0" * 64
    with pytest.raises(ExistingArtifactImportError, match="completed import no longer"):
        importer.import_artifacts(request)
    assert _import_state(database, settings)[:3] == before[:3]
    assert _generation_counts(database) == (0, 0)


def _cli_args(request: ExistingArtifactImportRequest):
    return cli.build_parser().parse_args(
        [
            "import-existing-lecture-artifacts",
            "--lecture-id",
            str(request.lecture_id),
            "--slides-revision-id",
            str(request.slides_revision_id),
            "--slides-source-sha256",
            request.slides_source_sha256,
            "--slides-pdf-sha256",
            request.slides_pdf_sha256,
            "--cleaned-transcript",
            str(request.cleaned_transcript),
            "--cleaned-transcript-sha256",
            request.cleaned_transcript_sha256,
            "--notebooklm-outline",
            str(request.notebooklm_outline),
            "--notebooklm-outline-sha256",
            request.notebooklm_outline_sha256,
        ]
    )


def test_windows_cli_contract_requires_authoritative_pptx_and_explicit_pdf_hash(tmp_path):
    _, _, _, request = _request_fixture(tmp_path)
    authoritative_pptx_sha256 = "b1c7abc3fb5d86476a3477d397e679ec42e61cff982fcec9dcb55a9d0a9c5469"
    args = cli.build_parser().parse_args(
        [
            "import-existing-lecture-artifacts",
            "--lecture-id",
            str(request.lecture_id),
            "--slides-revision-id",
            str(request.slides_revision_id),
            "--slides-source-sha256",
            authoritative_pptx_sha256,
            "--slides-pdf-sha256",
            request.slides_pdf_sha256,
            "--cleaned-transcript",
            str(request.cleaned_transcript),
            "--cleaned-transcript-sha256",
            request.cleaned_transcript_sha256,
            "--notebooklm-outline",
            str(request.notebooklm_outline),
            "--notebooklm-outline-sha256",
            request.notebooklm_outline_sha256,
        ]
    )
    assert args.slides_source_sha256 == authoritative_pptx_sha256
    assert args.slides_pdf_sha256 == request.slides_pdf_sha256


def test_a0_operator_file_gate_hash_contract_is_exact_and_separate(tmp_path, monkeypatch):
    """Contract test only; A0 verifies the real shared files separately."""
    files = tuple(
        tmp_path / name for name in ("slides.pptx", "slides.pdf", "cleaned.txt", "outline.pdf")
    )
    for path in files:
        path.touch()
    expected = dict(
        zip(
            files,
            (
                A0_PPTX_SHA256,
                A0_PDF_SHA256,
                A0_CLEANED_TRANSCRIPT_SHA256,
                A0_OUTLINE_SHA256,
            ),
            strict=True,
        )
    )
    checked: list[Path] = []

    def contract_hasher(path: Path) -> str:
        checked.append(path)
        return expected[path]

    import oms_hub.existing_artifact_import as import_module

    monkeypatch.setattr(import_module, "sha256_file", contract_hasher)
    assert (
        len(
            {
                A0_PPTX_SHA256,
                A0_PDF_SHA256,
                A0_CLEANED_TRANSCRIPT_SHA256,
                A0_OUTLINE_SHA256,
            }
        )
        == 4
    )
    verify_a0_operator_files(*files)
    assert checked == list(files)


@pytest.mark.parametrize("wrong_index", range(4))
def test_a0_operator_file_gate_rejects_each_wrong_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrong_index: int
) -> None:
    files = tuple(tmp_path / f"a0-{index}" for index in range(4))
    for path in files:
        path.touch()
    expected = (
        A0_PPTX_SHA256,
        A0_PDF_SHA256,
        A0_CLEANED_TRANSCRIPT_SHA256,
        A0_OUTLINE_SHA256,
    )
    actual = dict(zip(files, expected, strict=True))
    actual[files[wrong_index]] = "0" * 64
    import oms_hub.existing_artifact_import as import_module

    monkeypatch.setattr(import_module, "sha256_file", actual.__getitem__)
    with pytest.raises(ExistingArtifactImportError, match="A0 .* SHA-256 does not match"):
        verify_a0_operator_files(*files)


def test_a0_cli_flag_enforces_the_four_file_gate(tmp_path, monkeypatch, capsys) -> None:
    _, _, _, request = _request_fixture(tmp_path)
    pptx, pdf = tmp_path / "a0.pptx", tmp_path / "a0.pdf"
    pptx.touch()
    pdf.touch()
    checked: list[Path] = []

    def reject_gate(*paths: Path) -> None:
        checked.extend(paths)
        raise ExistingArtifactImportError("A0 deliberate rejection")

    monkeypatch.setattr(cli, "verify_a0_operator_files", reject_gate)
    args = cli.build_parser().parse_args(
        [
            "import-existing-lecture-artifacts",
            "--lecture-id",
            str(request.lecture_id),
            "--slides-revision-id",
            str(request.slides_revision_id),
            "--slides-source-sha256",
            request.slides_source_sha256,
            "--slides-pdf-sha256",
            request.slides_pdf_sha256,
            "--cleaned-transcript",
            str(request.cleaned_transcript),
            "--cleaned-transcript-sha256",
            request.cleaned_transcript_sha256,
            "--notebooklm-outline",
            str(request.notebooklm_outline),
            "--notebooklm-outline-sha256",
            request.notebooklm_outline_sha256,
            "--a0-operator-files",
            "--a0-authoritative-pptx",
            str(pptx),
            "--a0-derived-pdf",
            str(pdf),
        ]
    )
    assert args.handler(args) == 2
    assert checked == [pptx, pdf, request.cleaned_transcript, request.notebooklm_outline]
    assert json.loads(capsys.readouterr().out) == {
        "error": "A0 deliberate rejection",
        "status": "error",
    }


@pytest.mark.parametrize("mismatched", ["--slides-source-sha256", "--slides-pdf-sha256"])
def test_a0_cli_rejects_request_hashes_that_do_not_bind_verified_files(
    tmp_path, monkeypatch, capsys, mismatched
) -> None:
    _, _, _, request = _request_fixture(tmp_path)
    pptx, pdf = tmp_path / "a0.pptx", tmp_path / "a0.pdf"
    pptx.touch()
    pdf.touch()
    monkeypatch.setattr(cli, "verify_a0_operator_files", lambda *_paths: None)
    argument_values = {
        "--slides-source-sha256": A0_PPTX_SHA256,
        "--slides-pdf-sha256": A0_PDF_SHA256,
    }
    argument_values[mismatched] = "0" * 64
    args = cli.build_parser().parse_args(
        [
            "import-existing-lecture-artifacts",
            "--lecture-id",
            str(request.lecture_id),
            "--slides-revision-id",
            str(request.slides_revision_id),
            "--slides-source-sha256",
            argument_values["--slides-source-sha256"],
            "--slides-pdf-sha256",
            argument_values["--slides-pdf-sha256"],
            "--cleaned-transcript",
            str(request.cleaned_transcript),
            "--cleaned-transcript-sha256",
            A0_CLEANED_TRANSCRIPT_SHA256,
            "--notebooklm-outline",
            str(request.notebooklm_outline),
            "--notebooklm-outline-sha256",
            A0_OUTLINE_SHA256,
            "--a0-operator-files",
            "--a0-authoritative-pptx",
            str(pptx),
            "--a0-derived-pdf",
            str(pdf),
        ]
    )
    assert args.handler(args) == 2
    body = json.loads(capsys.readouterr().out)
    assert body["status"] == "error"
    assert "A0 request" in body["error"]


def test_import_cli_parser_validation_error_is_stable_json(tmp_path, monkeypatch, capsys):
    database, settings, _, request = _request_fixture(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "Database", lambda _url: database)
    args = _cli_args(replace(request, cleaned_transcript=Path("relative.txt")))
    assert args.handler is cli.import_existing_lecture_artifacts
    assert args.handler(args) == 2
    assert json.loads(capsys.readouterr().out) == {
        "error": "cleaned transcript must be an absolute regular non-symlink .txt file",
        "status": "error",
    }


@pytest.mark.parametrize("operation", ["ReadFile", "WriteFile", "FlushFileBuffers", "final hash"])
def test_importer_normalizes_hardened_native_copy_failures(tmp_path, monkeypatch, operation):
    database, _settings, importer, request = _request_fixture(tmp_path)
    import oms_hub.existing_artifact_import as import_module
    from oms_hub.files.handle_relative import HardenedWriteError

    def fail_copy(*_args: object, **_kwargs: object) -> str:
        raise HardenedWriteError(f"pinned Windows {operation} failed") from OSError(operation)

    monkeypatch.setattr(import_module, "hardened_verified_copy", fail_copy)
    with pytest.raises(ExistingArtifactImportError, match="pinned atomic copy could not complete"):
        importer.import_artifacts(request)
    assert database is importer.database


def test_import_cli_hardened_import_failure_is_stable_json(tmp_path, monkeypatch, capsys):
    database, settings, _importer, request = _request_fixture(tmp_path)

    class FailingImporter:
        def __init__(self, _database: Database, _settings: Settings) -> None:
            return None

        def import_artifacts(self, _request: ExistingArtifactImportRequest) -> object:
            raise ExistingArtifactImportError("pinned atomic copy could not complete")

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "Database", lambda _url: database)
    monkeypatch.setattr(cli, "ExistingArtifactImporter", FailingImporter)
    args = _cli_args(request)
    assert args.handler(args) == 2
    assert json.loads(capsys.readouterr().out) == {
        "error": "pinned atomic copy could not complete",
        "status": "error",
    }


def test_import_cli_normalizes_hardened_posix_copy_failure_to_stable_json(
    tmp_path, monkeypatch, capsys
):
    database, settings, _importer, request = _request_fixture(tmp_path)
    import oms_hub.existing_artifact_import as import_module
    from oms_hub.files.handle_relative import HardenedWriteError

    def fail_copy(*_args: object, **_kwargs: object) -> str:
        raise HardenedWriteError("pinned POSIX copy failed") from PermissionError("forced")

    monkeypatch.setattr(import_module, "hardened_verified_copy", fail_copy)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "Database", lambda _url: database)
    args = _cli_args(request)

    assert args.handler(args) == 2
    assert json.loads(capsys.readouterr().out) == {
        "error": "pinned atomic copy could not complete",
        "status": "error",
    }


@pytest.mark.parametrize("error", [ArtifactWriteContended("held"), ArtifactWriteClaimLost("lost")])
def test_import_cli_fencing_errors_are_retryable_json(tmp_path, monkeypatch, capsys, error):
    database, settings, _, request = _request_fixture(tmp_path)

    class FailingImporter:
        def __init__(self, _database, _settings) -> None:
            pass

        def import_artifacts(self, _request):
            raise error

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "Database", lambda _url: database)
    monkeypatch.setattr(cli, "ExistingArtifactImporter", FailingImporter)
    args = _cli_args(request)
    assert args.handler(args) == 75
    assert json.loads(capsys.readouterr().out) == {"error": str(error), "status": "retryable"}


def test_import_cli_success_identity_and_idempotent_retry(tmp_path, monkeypatch, capsys):
    database, settings, _, request = _request_fixture(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "Database", lambda _url: database)
    args = _cli_args(request)
    assert args.handler(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert args.handler(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert first["status"] == second["status"] == "complete"
    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert first["import_id"] == second["import_id"]
    assert first["lecture"] == second["lecture"]
    assert first["transcript"] == second["transcript"]
    assert first["outline"] == second["outline"]
    assert set(first) == {
        "attempts",
        "bundle_sha256",
        "idempotent",
        "import_id",
        "lecture",
        "outline",
        "status",
        "transcript",
    }
    assert first["transcript"]["provenance_kind"] == "imported_cleaned"
    assert first["outline"]["provenance_kind"] == "imported_notebooklm"
    assert _generation_counts(database) == (0, 0)


def test_explicit_derived_pdf_adoption_archives_target_and_preserves_office_pdf(tmp_path):
    database, settings, importer, request = _request_fixture(tmp_path)
    old_immutable = settings.data_dir / "artifacts" / "v2" / "slides" / "source.pdf"
    old_bytes = old_immutable.read_bytes()
    icloud = settings.icloud_staging_root / "Neuro" / "iCloud-slides.pdf"
    icloud.parent.mkdir(parents=True, exist_ok=True)
    icloud.write_bytes(old_bytes)
    with database.session() as session:
        slide = session.get(StudyRevisionModel, request.slides_revision_id)
        assert slide is not None
        slide.icloud_path = str(icloud)
    target = tmp_path / "authoritative.pdf"
    _write_outline(target)
    adopted = replace(
        request,
        slides_pdf_sha256=_sha256(target),
        authoritative_derived_pdf=target,
        expected_current_pdf_sha256=_sha256(old_immutable),
        adoption_operator="operator",
        adoption_reason="A0 verified source",
        confirm_derived_adoption=True,
    )
    result = importer.import_artifacts(adopted)
    assert result.adoption is not None
    assert result.adoption["phase"] == "committed"
    assert old_immutable.read_bytes() == old_bytes
    with database.session() as session:
        slide = session.get(StudyRevisionModel, request.slides_revision_id)
        audit = session.get(ExistingArtifactImportModel, result.import_id)
        assert slide is not None and audit is not None
        assert slide.immutable_derived_path == audit.imported_immutable_pdf_path
        assert Path(audit.imported_immutable_pdf_path or "").read_bytes() == target.read_bytes()
        assert slide.provenance_kind == "imported_derived"
        assert slide.import_id == audit.id
    assert icloud.read_bytes() == target.read_bytes()
    assert importer.import_artifacts(adopted).idempotent is True


def _adoption_fixture(tmp_path):
    database, settings, importer, request = _request_fixture(tmp_path)
    old = settings.data_dir / "artifacts" / "v2" / "slides" / "source.pdf"
    icloud = settings.icloud_staging_root / "Neuro" / "iCloud-slides.pdf"
    icloud.parent.mkdir(parents=True, exist_ok=True)
    icloud.write_bytes(old.read_bytes())
    with database.session() as session:
        slide = session.get(StudyRevisionModel, request.slides_revision_id)
        assert slide is not None
        slide.icloud_path = str(icloud)
    target = tmp_path / "authoritative.pdf"
    _write_outline(target)
    return (
        database,
        settings,
        importer,
        request,
        old,
        icloud,
        target,
        replace(
            request,
            slides_pdf_sha256=_sha256(target),
            authoritative_derived_pdf=target,
            expected_current_pdf_sha256=_sha256(old),
            adoption_operator="operator",
            adoption_reason="verified adoption",
            confirm_derived_adoption=True,
        ),
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("authoritative_derived_pdf", None),
        ("expected_current_pdf_sha256", None),
        ("adoption_operator", ""),
        ("adoption_reason", ""),
        ("confirm_derived_adoption", False),
        ("expected_current_pdf_sha256", "A" * 64),
        ("slides_pdf_sha256", "bad"),
    ],
)
def test_adoption_incomplete_or_malformed_intent_has_no_mutation(tmp_path, field, value):
    (database, settings, importer, request, _old, _icloud, _target, adopted) = _adoption_fixture(
        tmp_path
    )
    before = _import_state(database, settings)
    with pytest.raises(ExistingArtifactImportError):
        importer.import_artifacts(replace(adopted, **{field: value}))
    assert _import_state(database, settings) == before


@pytest.mark.parametrize("path_name", ["immutable", "canonical", "icloud"])
def test_adoption_rejects_current_old_path_mismatch_without_mutation(tmp_path, path_name):
    (database, settings, importer, request, old, icloud, _target, adopted) = _adoption_fixture(
        tmp_path
    )
    paths = {
        "immutable": old,
        "canonical": settings.study_root / "Neuro" / "slides.pdf",
        "icloud": icloud,
    }
    paths[path_name].write_bytes(b"different")
    before = _import_state(database, settings)
    with pytest.raises(ExistingArtifactImportError, match="path/hash is not exact"):
        importer.import_artifacts(adopted)
    assert _import_state(database, settings) == before


@pytest.mark.parametrize(
    "field,path_name",
    [
        ("immutable_source_path", "immutable-pptx"),
        ("immutable_derived_path", "immutable-old-pdf"),
        ("canonical_source_path", "canonical-pptx"),
        ("canonical_derived_path", "canonical-pdf"),
        ("icloud_path", "icloud-pdf"),
    ],
)
@pytest.mark.parametrize("path_form", ["symlink", "relative"])
def test_adoption_rejects_untrusted_persisted_slide_paths_before_any_mutation(
    tmp_path, field, path_name, path_form
):
    (database, settings, importer, _request, _old, icloud, _target, adopted) = _adoption_fixture(
        tmp_path
    )
    paths = {
        "immutable-pptx": settings.data_dir / "artifacts" / "v2" / "slides" / "source.pptx",
        "immutable-old-pdf": settings.data_dir / "artifacts" / "v2" / "slides" / "source.pdf",
        "canonical-pptx": settings.study_root / "Neuro" / "slides.pptx",
        "canonical-pdf": settings.study_root / "Neuro" / "slides.pdf",
        "icloud-pdf": icloud,
    }
    path = paths[path_name]
    before_bytes = {name: candidate.read_bytes() for name, candidate in paths.items()}
    if path_form == "symlink":
        moved = tmp_path / f"{path_name}-real{path.suffix}"
        try:
            path.rename(moved)
            path.symlink_to(moved)
        except OSError as error:
            pytest.skip(f"symlink support unavailable: {error}")
    else:
        with database.session() as session:
            slide = session.get(StudyRevisionModel, adopted.slides_revision_id)
            assert slide is not None
            setattr(slide, field, f"relative/{path.name}")
    before = _import_state(database, settings)

    with pytest.raises(ExistingArtifactImportError, match="absolute regular non-symlink"):
        importer.import_artifacts(adopted)

    assert _import_state(database, settings) == before
    assert {name: candidate.read_bytes() for name, candidate in paths.items()} == before_bytes


@pytest.mark.parametrize(
    "path_name",
    [
        "immutable-pptx",
        "immutable-old-pdf",
        "canonical-pptx",
        "canonical-pdf",
        "icloud-pdf",
    ],
)
def test_adoption_rejects_mocked_windows_junction_graph_paths_before_mutation(
    tmp_path, monkeypatch, path_name
):
    """This is intentionally mocked so Windows coverage never depends on symlink support."""
    database, settings, importer, _request, _old, icloud, _target, adopted = _adoption_fixture(
        tmp_path
    )
    paths = {
        "immutable-pptx": settings.data_dir / "artifacts" / "v2" / "slides" / "source.pptx",
        "immutable-old-pdf": settings.data_dir / "artifacts" / "v2" / "slides" / "source.pdf",
        "canonical-pptx": settings.study_root / "Neuro" / "slides.pptx",
        "canonical-pdf": settings.study_root / "Neuro" / "slides.pdf",
        "icloud-pdf": icloud,
    }
    target = paths[path_name]
    original_is_junction = getattr(Path, "is_junction", None)

    def is_junction(path: Path) -> bool:
        return path == target or (
            callable(original_is_junction) and bool(original_is_junction(path))
        )

    monkeypatch.setattr(Path, "is_junction", is_junction, raising=False)
    before = _import_state(database, settings)

    with pytest.raises(ExistingArtifactImportError, match="absolute regular non-symlink"):
        importer.import_artifacts(adopted)

    assert _import_state(database, settings) == before


@pytest.mark.parametrize("root_name", ["v2", "study", "icloud"])
@pytest.mark.parametrize("indirection", ["junction", "symlink"])
def test_adoption_rejects_mocked_parent_indirection_above_each_configured_root(
    tmp_path, monkeypatch, root_name, indirection
):
    database, settings, importer, _request, _old, _icloud, _target, adopted = _adoption_fixture(
        tmp_path
    )
    parents = {
        "v2": settings.data_dir / "artifacts",
        "study": settings.study_root.parent,
        "icloud": settings.icloud_staging_root.parent,
    }
    target = parents[root_name]
    if indirection == "junction":
        original_is_junction = getattr(Path, "is_junction", None)

        def is_junction(path: Path) -> bool:
            return path == target or (
                callable(original_is_junction) and bool(original_is_junction(path))
            )

        monkeypatch.setattr(Path, "is_junction", is_junction, raising=False)
    else:
        original_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda path: path == target or original_is_symlink(path),
        )
    before = _import_state(database, settings)
    with pytest.raises(ExistingArtifactImportError):
        importer.import_artifacts(adopted)
    assert _import_state(database, settings) == before


@pytest.mark.parametrize("adoption", [False, True])
@pytest.mark.parametrize("indirection", ["symlink", "junction"])
def test_first_import_rejects_untrusted_existing_imports_root_before_audit_or_copy(
    tmp_path, monkeypatch, adoption, indirection
):
    if adoption:
        database, settings, importer, _request, _old, icloud, _target, request = _adoption_fixture(
            tmp_path
        )
        watched = [
            settings.study_root / "Neuro" / "slides.pdf",
            icloud,
        ]
    else:
        database, settings, importer, request = _request_fixture(tmp_path)
        watched = [settings.study_root / "Neuro" / "slides.pdf"]
    imports_root = settings.data_dir / "artifacts" / "existing-imports"
    link_target = tmp_path / "linked-existing-imports"
    link_target.mkdir()
    if indirection == "symlink":
        try:
            imports_root.symlink_to(link_target, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"symlink support unavailable: {error}")
    else:
        imports_root.mkdir()
        original_is_junction = getattr(Path, "is_junction", None)

        def is_junction(path: Path) -> bool:
            return path == imports_root or (
                callable(original_is_junction) and bool(original_is_junction(path))
            )

        monkeypatch.setattr(Path, "is_junction", is_junction, raising=False)
    before = {path: path.read_bytes() for path in watched}

    with pytest.raises(ExistingArtifactImportError, match="existing-import managed root"):
        importer.import_artifacts(request)

    with database.session() as session:
        assert session.query(ExistingArtifactImportModel).count() == 0
    assert list(link_target.iterdir()) == []
    assert {path: path.read_bytes() for path in watched} == before


def test_first_import_rejects_indirect_artifacts_parent_before_audit_or_copy(tmp_path):
    database, settings, importer, request = _request_fixture(tmp_path)
    artifacts = settings.data_dir / "artifacts"
    moved = tmp_path / "artifacts-real"
    try:
        artifacts.rename(moved)
        artifacts.symlink_to(moved, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink support unavailable: {error}")
    canonical = settings.study_root / "Neuro" / "slides.pdf"
    before = canonical.read_bytes()

    with pytest.raises(ExistingArtifactImportError):
        importer.import_artifacts(request)

    with database.session() as session:
        assert session.query(ExistingArtifactImportModel).count() == 0
    assert canonical.read_bytes() == before
    assert not (moved / "existing-imports").exists()


def test_first_import_creates_missing_honest_existing_imports_root_and_succeeds(tmp_path):
    database, settings, importer, request = _request_fixture(tmp_path)
    imports_root = settings.data_dir / "artifacts" / "existing-imports"
    assert not imports_root.exists()

    result = importer.import_artifacts(request)

    assert result.status == "complete"
    assert imports_root.is_dir() and not imports_root.is_symlink()


def test_adoption_archive_copy_parent_swap_never_writes_outside_pinned_audit_dir(tmp_path):
    database, settings, importer, _request, _old, icloud, _target, adopted = _adoption_fixture(
        tmp_path
    )
    imports_root = settings.data_dir / "artifacts" / "existing-imports"
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = tmp_path / "moved-audit"
    canonical = settings.study_root / "Neuro" / "slides.pdf"
    before = (canonical.read_bytes(), icloud.read_bytes())
    parent_pins = 0

    def hook(name: str) -> None:
        nonlocal parent_pins
        if name != "destination-parent-pinned":
            return
        parent_pins += 1
        # transcript, outline, canonical transcript, canonical outline, archive
        if parent_pins == 5:
            audit_root = next(path for path in imports_root.iterdir() if path.is_dir())
            audit_root.rename(moved)
            audit_root.symlink_to(outside, target_is_directory=True)

    set_hardened_write_hook(hook)
    try:
        with pytest.raises(ExistingArtifactImportError):
            importer.import_artifacts(adopted)
    finally:
        set_hardened_write_hook(None)

    assert list(outside.iterdir()) == []
    assert (canonical.read_bytes(), icloud.read_bytes()) == before
    assert (moved / "derived-slide.pdf").is_file()
    with database.session() as session:
        assert session.query(ExistingArtifactImportModel).count() == 1


@pytest.mark.parametrize("component", ["v2-root", "slide-ancestor", "study-root", "icloud-root"])
def test_adoption_rejects_indirect_managed_root_or_ancestor_before_mutation(
    tmp_path, component
):
    database, settings, importer, _request, _old, _icloud, _target, adopted = _adoption_fixture(
        tmp_path
    )
    paths = {
        "v2-root": settings.data_dir / "artifacts" / "v2",
        "slide-ancestor": settings.data_dir / "artifacts" / "v2" / "slides",
        "study-root": settings.study_root,
        "icloud-root": settings.icloud_staging_root,
    }
    path = paths[component]
    moved = tmp_path / f"{component}-real"
    try:
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink support unavailable: {error}")
    before = _import_state(database, settings)

    with pytest.raises(ExistingArtifactImportError):
        importer.import_artifacts(adopted)

    assert _import_state(database, settings) == before


def test_adoption_rejects_lexically_outside_path_resolving_back_inside_before_mutation(tmp_path):
    database, settings, importer, _request, _old, _icloud, _target, adopted = _adoption_fixture(
        tmp_path
    )
    with database.session() as session:
        slide = session.get(StudyRevisionModel, adopted.slides_revision_id)
        assert slide is not None
        slide.canonical_source_path = str(
            settings.study_root / "Neuro" / ".." / "Neuro" / "slides.pptx"
        )
    before = _import_state(database, settings)

    with pytest.raises(ExistingArtifactImportError, match="absolute regular non-symlink"):
        importer.import_artifacts(adopted)

    assert _import_state(database, settings) == before


@pytest.mark.parametrize(
    "field,path_name",
    [
        ("immutable_source_path", "immutable-pptx"),
        ("immutable_derived_path", "immutable-pdf"),
        ("canonical_source_path", "canonical-pptx"),
        ("canonical_derived_path", "canonical-pdf"),
    ],
)
def test_ordinary_import_rejects_symlinked_slide_paths_before_any_mutation(
    tmp_path, field, path_name
):
    database, settings, importer, request = _request_fixture(tmp_path)
    paths = {
        "immutable-pptx": settings.data_dir / "artifacts" / "v2" / "slides" / "source.pptx",
        "immutable-pdf": settings.data_dir / "artifacts" / "v2" / "slides" / "source.pdf",
        "canonical-pptx": settings.study_root / "Neuro" / "slides.pptx",
        "canonical-pdf": settings.study_root / "Neuro" / "slides.pdf",
    }
    path = paths[path_name]
    before_bytes = {name: candidate.read_bytes() for name, candidate in paths.items()}
    moved = tmp_path / f"ordinary-{path_name}-real{path.suffix}"
    try:
        path.rename(moved)
        path.symlink_to(moved)
    except OSError as error:
        pytest.skip(f"symlink support unavailable: {error}")
    before = _import_state(database, settings)

    with pytest.raises(ExistingArtifactImportError, match="absolute regular non-symlink"):
        importer.import_artifacts(request)

    assert _import_state(database, settings) == before
    assert {name: candidate.read_bytes() for name, candidate in paths.items()} == before_bytes


def test_adoption_exact_target_file_with_literal_incident_old_hash_fails_before_mutation(tmp_path):
    (database, settings, importer, request, _old, _icloud, _target, adopted) = _adoption_fixture(
        tmp_path
    )
    copied = tmp_path / "Lecture 02 - Hemoglobin Synthesis and Function.pdf"
    copied.write_bytes(_target.read_bytes())
    literal = replace(
        adopted,
        authoritative_derived_pdf=copied,
        slides_pdf_sha256=A0_PDF_SHA256,
        expected_current_pdf_sha256="0bf098df3518a9cc7f3c0657f109eb9a802e5e1681123e1d194aec5b31ea5de8",
    )
    before = _import_state(database, settings)
    with pytest.raises(ExistingArtifactImportError, match="authoritative derived PDF SHA-256"):
        importer.import_artifacts(literal)
    assert _import_state(database, settings) == before


def test_adoption_changed_operator_reason_or_filename_is_not_idempotent(tmp_path):
    (database, settings, importer, request, _old, _icloud, target, adopted) = _adoption_fixture(
        tmp_path
    )
    importer.import_artifacts(adopted)
    renamed = tmp_path / "renamed.pdf"
    renamed.write_bytes(target.read_bytes())
    for changed in (
        replace(adopted, adoption_operator="other"),
        replace(adopted, adoption_reason="other"),
        replace(adopted, authoritative_derived_pdf=renamed),
    ):
        before = _import_state(database, settings)
        with pytest.raises(ExistingArtifactImportError):
            importer.import_artifacts(changed)
        assert _import_state(database, settings) == before


@pytest.mark.parametrize(
    "boundary",
    [
        "after-adoption-archive",
        "after-adoption-canonical_promoted",
        "after-adoption-icloud_promoted",
        "after-adoption-precommit",
    ],
)
def test_adoption_owned_failure_at_each_boundary_rolls_back_and_retries(tmp_path, boundary):
    (database, settings, importer, request, old, icloud, _target, adopted) = _adoption_fixture(
        tmp_path
    )
    old_bytes = old.read_bytes()
    failed = False

    def checkpoint(phase: str) -> None:
        nonlocal failed
        if phase == boundary and not failed:
            failed = True
            raise RuntimeError(f"forced {boundary}")

    importer.checkpoint = checkpoint
    with pytest.raises(RuntimeError, match="forced"):
        importer.import_artifacts(adopted)
    assert old.read_bytes() == old_bytes
    assert (settings.study_root / "Neuro" / "slides.pdf").read_bytes() == old_bytes
    assert icloud.read_bytes() == old_bytes
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        assert audit.status == "preparing"
        assert audit.recovery_phase == "archived"
        assert Path(audit.imported_immutable_pdf_path or "").is_file()
    result = importer.import_artifacts(adopted)
    assert result.status == "complete"


@pytest.mark.parametrize(
    ("boundary", "copy_index"),
    [
        *( ("after-copy", index) for index in range(1, 5) ),
        ("after-adoption-archive_copying", 1),
    ],
)
def test_adoption_owned_pre_archive_failure_returns_to_preparing_and_retries(
    tmp_path, boundary, copy_index
):
    database, settings, importer, _request, old, icloud, target, adopted = _adoption_fixture(
        tmp_path
    )
    calls = 0

    def checkpoint(phase: str) -> None:
        nonlocal calls
        if phase == boundary:
            calls += 1
            if calls == copy_index:
                raise RuntimeError("owned pre-archive failure")

    importer.checkpoint = checkpoint
    with pytest.raises(RuntimeError, match="owned pre-archive failure"):
        importer.import_artifacts(adopted)
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        precursors = (
            Path(audit.immutable_transcript_path or ""),
            Path(audit.immutable_outline_path or ""),
            Path(audit.canonical_transcript_path or ""),
            Path(audit.canonical_outline_path or ""),
        )
        assert (audit.status, audit.recovery_phase) == ("preparing", "preparing")
        assert not Path(audit.imported_immutable_pdf_path or "").exists()
        assert not any(path.exists() for path in precursors)
    assert (settings.study_root / "Neuro" / "slides.pdf").read_bytes() == old.read_bytes()
    assert icloud.read_bytes() == old.read_bytes()
    fresh_database, _fresh_settings, fresh_importer = _fresh_adoption_importer(settings)
    assert fresh_importer.import_artifacts(adopted).status == "complete"
    assert (settings.study_root / "Neuro" / "slides.pdf").read_bytes() == target.read_bytes()
    fresh_database.close()


def test_adoption_owned_post_archive_failure_retains_archived_evidence_and_retries(tmp_path):
    database, settings, importer, _request, old, icloud, target, adopted = _adoption_fixture(
        tmp_path
    )
    importer.checkpoint = lambda phase: (
        (_ for _ in ()).throw(RuntimeError("owned post-archive failure"))
        if phase == "after-adoption-archive-copy"
        else None
    )
    with pytest.raises(RuntimeError, match="owned post-archive failure"):
        importer.import_artifacts(adopted)
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        archive = Path(audit.imported_immutable_pdf_path or "")
        assert (audit.status, audit.recovery_phase) == ("preparing", "archived")
        assert archive.is_file() and _sha256(archive) == adopted.slides_pdf_sha256
    assert (settings.study_root / "Neuro" / "slides.pdf").read_bytes() == old.read_bytes()
    assert icloud.read_bytes() == old.read_bytes()
    fresh_database, _fresh_settings, fresh_importer = _fresh_adoption_importer(settings)
    assert fresh_importer.import_artifacts(adopted).status == "complete"
    assert (settings.study_root / "Neuro" / "slides.pdf").read_bytes() == target.read_bytes()
    fresh_database.close()


@pytest.mark.parametrize("corruption", ["wrong-bytes", "directory", "symlink"])
def test_adoption_owned_rollback_refuses_nonexact_archive_evidence(tmp_path, corruption):
    database, _settings, importer, _request, _old, _icloud, _target, adopted = _adoption_fixture(
        tmp_path
    )

    def checkpoint(phase: str) -> None:
        if phase != "after-adoption-archive-copy":
            return
        with database.session() as session:
            audit = session.scalar(select(ExistingArtifactImportModel))
            assert audit is not None
            archive = Path(audit.imported_immutable_pdf_path or "")
        if corruption == "wrong-bytes":
            archive.write_bytes(b"wrong archive")
        elif corruption == "directory":
            archive.unlink()
            archive.mkdir()
        else:
            outside = tmp_path / "outside-archive.pdf"
            outside.write_bytes(archive.read_bytes())
            archive.unlink()
            try:
                archive.symlink_to(outside)
            except OSError as error:
                pytest.skip(f"symlink support unavailable: {error}")
        raise RuntimeError("owned post-archive failure")

    importer.checkpoint = checkpoint
    with pytest.raises(ExistingArtifactRecoveryError, match="rollback could not restore"):
        importer.import_artifacts(adopted)
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        assert (audit.status, audit.recovery_phase) == ("preparing", "recovery_required")


@pytest.mark.parametrize(
    ("failure_boundary", "expected_phase"),
    [("after-copy", "preparing"), ("after-adoption-archive-copy", "recovery_required")],
)
def test_adoption_process_death_after_failure_state_migrates_then_retries(
    tmp_path, failure_boundary, expected_phase
):
    database, settings, importer, _request, _old, _icloud, target, adopted = _adoption_fixture(
        tmp_path
    )

    class SimulatedProcessDeath(BaseException):
        pass

    failed = False

    def checkpoint(phase: str) -> None:
        nonlocal failed
        if phase == failure_boundary and not failed:
            failed = True
            raise RuntimeError("ordinary failure")
        if phase == "after-adoption-failure-state":
            raise SimulatedProcessDeath("process terminated")

    importer.checkpoint = checkpoint
    with pytest.raises(SimulatedProcessDeath, match="process terminated"):
        importer.import_artifacts(adopted)
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        assert audit.recovery_phase == expected_phase
    fresh_database, _fresh_settings, fresh_importer = _fresh_adoption_importer(settings)
    assert fresh_importer.import_artifacts(adopted).status == "complete"
    assert (settings.study_root / "Neuro" / "slides.pdf").read_bytes() == target.read_bytes()
    fresh_database.close()


def test_adoption_first_write_process_death_migrates_then_retries(tmp_path):
    database, settings, importer, _request, old, icloud, target, adopted = _adoption_fixture(
        tmp_path
    )

    class SimulatedProcessDeath(BaseException):
        pass

    def checkpoint(phase: str) -> None:
        if phase == "after-audit-commit-before-first-copy":
            raise SimulatedProcessDeath

    importer.checkpoint = checkpoint
    with pytest.raises(SimulatedProcessDeath):
        importer.import_artifacts(adopted)
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        audit_root = Path(audit.immutable_transcript_path or "").parent
        assert (audit.status, audit.recovery_phase) == ("preparing", "preparing")
        assert not audit_root.exists()
        assert not Path(audit.imported_immutable_pdf_path or "").exists()
    # This is a fresh startup validation, before a new importer resumes writes.
    Database(settings.database_url).migrate()

    result = ExistingArtifactImporter(database, settings).import_artifacts(adopted)
    assert result.status == "complete"
    assert (settings.study_root / "Neuro" / "slides.pdf").read_bytes() == target.read_bytes()
    assert icloud.read_bytes() == target.read_bytes()
    assert old.is_file()


@pytest.mark.parametrize("copy_index", range(1, 5))
def test_adoption_precursor_copy_process_death_migrates_then_retries(tmp_path, copy_index):
    database, settings, importer, _request, old, icloud, target, adopted = _adoption_fixture(
        tmp_path
    )

    class SimulatedProcessDeath(BaseException):
        pass

    copies = 0

    def checkpoint(phase: str) -> None:
        nonlocal copies
        if phase == "after-copy":
            copies += 1
            if copies == copy_index:
                raise SimulatedProcessDeath("process terminated")

    importer.checkpoint = checkpoint
    with pytest.raises(SimulatedProcessDeath, match="process terminated"):
        importer.import_artifacts(adopted)

    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        precursor_paths = (
            Path(audit.immutable_transcript_path or ""),
            Path(audit.immutable_outline_path or ""),
            Path(audit.canonical_transcript_path or ""),
            Path(audit.canonical_outline_path or ""),
        )
        assert (audit.status, audit.recovery_phase) == ("preparing", "preparing")
        assert tuple(path.exists() for path in precursor_paths) == tuple(
            index < copy_index for index in range(4)
        )
        expected_bytes = (
            adopted.cleaned_transcript.read_bytes(),
            adopted.notebooklm_outline.read_bytes(),
            adopted.cleaned_transcript.read_bytes(),
            adopted.notebooklm_outline.read_bytes(),
        )
        assert tuple(path.read_bytes() for path in precursor_paths[:copy_index]) == expected_bytes[
            :copy_index
        ]
        assert not Path(audit.imported_immutable_pdf_path or "").exists()
    assert (settings.study_root / "Neuro" / "slides.pdf").read_bytes() == old.read_bytes()
    assert icloud.read_bytes() == old.read_bytes()

    fresh_database, _fresh_settings, fresh_importer = _fresh_adoption_importer(settings)
    assert fresh_importer.import_artifacts(adopted).status == "complete"
    assert (settings.study_root / "Neuro" / "slides.pdf").read_bytes() == target.read_bytes()
    assert icloud.read_bytes() == target.read_bytes()
    fresh_database.close()


@pytest.mark.parametrize("copy_index", range(1, 5))
def test_adoption_precursor_copy_claim_loss_migrates_then_retries(tmp_path, copy_index):
    database, settings, importer, _request, old, icloud, target, adopted = _adoption_fixture(
        tmp_path
    )
    writes = _FencedWrites()
    importer.writes = writes
    copies = 0

    def checkpoint(phase: str) -> None:
        nonlocal copies
        if phase == "after-copy":
            copies += 1
            if copies == copy_index:
                writes.current.lost = True

    importer.checkpoint = checkpoint
    with pytest.raises(ArtifactWriteClaimLost, match="forced claim loss"):
        importer.import_artifacts(adopted)

    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        precursor_paths = (
            Path(audit.immutable_transcript_path or ""),
            Path(audit.immutable_outline_path or ""),
            Path(audit.canonical_transcript_path or ""),
            Path(audit.canonical_outline_path or ""),
        )
        assert (audit.owner, audit.status, audit.recovery_phase, audit.error) == (
            "fault-owner",
            "failed",
            "preparing",
            "forced claim loss",
        )
        assert tuple(path.exists() for path in precursor_paths) == tuple(
            index < copy_index for index in range(4)
        )
        expected_bytes = (
            adopted.cleaned_transcript.read_bytes(),
            adopted.notebooklm_outline.read_bytes(),
            adopted.cleaned_transcript.read_bytes(),
            adopted.notebooklm_outline.read_bytes(),
        )
        assert tuple(path.read_bytes() for path in precursor_paths[:copy_index]) == expected_bytes[
            :copy_index
        ]
        assert not Path(audit.imported_immutable_pdf_path or "").exists()
    assert (settings.study_root / "Neuro" / "slides.pdf").read_bytes() == old.read_bytes()
    assert icloud.read_bytes() == old.read_bytes()

    fresh_database, _fresh_settings, fresh_importer = _fresh_adoption_importer(settings)
    assert fresh_importer.import_artifacts(adopted).status == "complete"
    assert (settings.study_root / "Neuro" / "slides.pdf").read_bytes() == target.read_bytes()
    assert icloud.read_bytes() == target.read_bytes()
    fresh_database.close()


@pytest.mark.parametrize(
    ("boundary", "archive_present"),
    [
        ("after-adoption-archive_copying", False),
        ("after-adoption-archive-copy", True),
    ],
)
def test_adoption_archive_copying_process_death_migrates_then_retries(
    tmp_path, boundary, archive_present
):
    database, settings, importer, _request, old, icloud, target, adopted = _adoption_fixture(
        tmp_path
    )

    class SimulatedProcessDeath(BaseException):
        pass

    importer.checkpoint = lambda phase: (
        (_ for _ in ()).throw(SimulatedProcessDeath("process terminated"))
        if phase == boundary
        else None
    )
    with pytest.raises(SimulatedProcessDeath, match="process terminated"):
        importer.import_artifacts(adopted)
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        precursors = (
            Path(audit.immutable_transcript_path or ""),
            Path(audit.immutable_outline_path or ""),
            Path(audit.canonical_transcript_path or ""),
            Path(audit.canonical_outline_path or ""),
        )
        assert (audit.status, audit.recovery_phase) == ("preparing", "archive_copying")
        assert all(path.is_file() for path in precursors)
        assert Path(audit.imported_immutable_pdf_path or "").is_file() is archive_present
    assert (settings.study_root / "Neuro" / "slides.pdf").read_bytes() == old.read_bytes()
    assert icloud.read_bytes() == old.read_bytes()
    fresh_database, _fresh_settings, fresh_importer = _fresh_adoption_importer(settings)
    assert fresh_importer.import_artifacts(adopted).status == "complete"
    assert (settings.study_root / "Neuro" / "slides.pdf").read_bytes() == target.read_bytes()
    fresh_database.close()


@pytest.mark.parametrize(
    ("boundary", "archive_present"),
    [
        ("after-adoption-archive_copying", False),
        ("after-adoption-archive-copy", True),
    ],
)
def test_adoption_archive_copying_claim_loss_migrates_then_retries(
    tmp_path, boundary, archive_present
):
    database, settings, importer, _request, old, icloud, target, adopted = _adoption_fixture(
        tmp_path
    )
    writes = _FencedWrites()
    importer.writes = writes

    def checkpoint(phase: str) -> None:
        if phase == boundary:
            writes.current.lost = True

    importer.checkpoint = checkpoint
    with pytest.raises(ArtifactWriteClaimLost, match="forced claim loss"):
        importer.import_artifacts(adopted)
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        assert (audit.status, audit.recovery_phase, audit.error) == (
            "failed",
            "archive_copying",
            "forced claim loss",
        )
        assert Path(audit.imported_immutable_pdf_path or "").is_file() is archive_present
    assert (settings.study_root / "Neuro" / "slides.pdf").read_bytes() == old.read_bytes()
    assert icloud.read_bytes() == old.read_bytes()
    fresh_database, _fresh_settings, fresh_importer = _fresh_adoption_importer(settings)
    assert fresh_importer.import_artifacts(adopted).status == "complete"
    assert (settings.study_root / "Neuro" / "slides.pdf").read_bytes() == target.read_bytes()
    fresh_database.close()


@pytest.mark.parametrize(
    "corruption",
    [
        "preparing-archive",
        "missing-precursor",
        "wrong-archive",
        "wrong-mutable",
    ],
)
def test_adoption_archive_copying_runtime_corruption_rejects_before_retry(tmp_path, corruption):
    database, settings, importer, _request, _old, _icloud, _target, adopted = _adoption_fixture(
        tmp_path
    )

    class SimulatedProcessDeath(BaseException):
        pass

    boundary = (
        "after-adoption-archive-copy"
        if corruption == "preparing-archive"
        else "after-adoption-archive_copying"
    )
    importer.checkpoint = lambda phase: (
        (_ for _ in ()).throw(SimulatedProcessDeath("process terminated"))
        if phase == boundary
        else None
    )
    with pytest.raises(SimulatedProcessDeath, match="process terminated"):
        importer.import_artifacts(adopted)
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        if corruption == "preparing-archive":
            audit.recovery_phase = "preparing"
        elif corruption == "missing-precursor":
            Path(audit.canonical_outline_path or "").unlink()
        elif corruption == "wrong-archive":
            Path(audit.imported_immutable_pdf_path or "").write_bytes(b"wrong archive")
        else:
            (settings.study_root / "Neuro" / "slides.pdf").write_bytes(b"third state")
    before = _import_state(database, settings)
    importer.checkpoint = lambda _phase: None
    with pytest.raises(ExistingArtifactImportError):
        importer.import_artifacts(adopted)
    assert _import_state(database, settings) == before


@pytest.mark.parametrize(
    "boundary",
    [
        "after-adoption-archive",
        "after-adoption-canonical_promoted",
        "after-adoption-icloud_promoted",
        "after-adoption-precommit",
    ],
)
def test_adoption_claim_loss_manual_old_state_respects_durable_phase(tmp_path, boundary):
    (database, settings, importer, request, old, icloud, _target, adopted) = _adoption_fixture(
        tmp_path
    )
    writes = _FencedWrites()
    importer.writes = writes
    canonical = settings.study_root / "Neuro" / "slides.pdf"

    def checkpoint(phase: str) -> None:
        if phase != boundary:
            return
        canonical.write_bytes(b"successor canonical")
        icloud.write_bytes(b"successor icloud")
        writes.current.lost = True

    importer.checkpoint = checkpoint
    with pytest.raises(ArtifactWriteClaimLost, match="forced claim loss"):
        importer.import_artifacts(adopted)
    assert canonical.read_bytes() == b"successor canonical"
    assert icloud.read_bytes() == b"successor icloud"
    assert old.is_file()
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        assert audit.status == "failed"
        assert audit.error == "forced claim loss"
        assert Path(audit.imported_immutable_pdf_path or "").is_file()
    # Only the archived phase admits a return to old/old.  Later durable
    # phases reject a manually forced third state rather than normalizing it.
    canonical.write_bytes(old.read_bytes())
    icloud.write_bytes(old.read_bytes())
    importer.writes = ArtifactWriteCoordinator(database, settings)
    importer.checkpoint = lambda _phase: None
    if boundary == "after-adoption-archive":
        assert importer.import_artifacts(adopted).status == "complete"
    else:
        before = _import_state(database, settings)
        with pytest.raises(ExistingArtifactImportError, match="mutable PDF state is not resumable"):
            importer.import_artifacts(adopted)
        assert _import_state(database, settings) == before


@pytest.mark.parametrize(
    "boundary",
    [
        "after-adoption-archive",
        "after-adoption-canonical_promoted",
        "after-adoption-icloud_promoted",
        "after-adoption-precommit",
    ],
)
def test_adoption_claim_loss_boundary_retries_without_manual_file_repair(tmp_path, boundary):
    (database, settings, importer, _request, old, icloud, target, adopted) = _adoption_fixture(
        tmp_path
    )
    writes = _FencedWrites()
    importer.writes = writes

    def checkpoint(phase: str) -> None:
        if phase == boundary:
            writes.current.lost = True

    importer.checkpoint = checkpoint
    with pytest.raises(ArtifactWriteClaimLost, match="forced claim loss"):
        importer.import_artifacts(adopted)
    importer.writes = ArtifactWriteCoordinator(database, settings)
    importer.checkpoint = lambda _phase: None
    result = importer.import_artifacts(adopted)
    assert result.status == "complete"
    canonical = settings.study_root / "Neuro" / "slides.pdf"
    assert canonical.read_bytes() == target.read_bytes()
    assert icloud.read_bytes() == target.read_bytes()
    assert old.read_bytes() != target.read_bytes()


@pytest.mark.parametrize(
    "boundary,expected_state",
    [
        ("after-adoption-canonical_promoted-copy", ("target", "old")),
        ("after-adoption-icloud_promoted-copy", ("target", "target")),
    ],
)
def test_adoption_claim_loss_immediately_after_mutable_copy_retries_from_adjacent_state(
    tmp_path, boundary, expected_state
):
    """A lost writer may leave only the copy's adjacent durable phase behind."""
    (database, settings, importer, _request, old, icloud, target, adopted) = _adoption_fixture(
        tmp_path
    )
    writes = _FencedWrites()
    importer.writes = writes

    def checkpoint(phase: str) -> None:
        if phase == boundary:
            writes.current.lost = True

    importer.checkpoint = checkpoint
    with pytest.raises(ArtifactWriteClaimLost, match="forced claim loss"):
        importer.import_artifacts(adopted)

    canonical = settings.study_root / "Neuro" / "slides.pdf"
    expected_bytes = {"old": old.read_bytes(), "target": target.read_bytes()}
    assert (canonical.read_bytes(), icloud.read_bytes()) == tuple(
        expected_bytes[state] for state in expected_state
    )
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        assert audit.status == "failed"
        assert audit.recovery_phase == (
            "archived" if boundary.startswith("after-adoption-canonical") else "canonical_promoted"
        )

    # A new owner resumes the exact adjacent state without any operator repair.
    importer.writes = ArtifactWriteCoordinator(database, settings)
    importer.checkpoint = lambda _phase: None
    assert importer.import_artifacts(adopted).status == "complete"


def _fresh_adoption_importer(
    settings: Settings,
) -> tuple[Database, Settings, ExistingArtifactImporter]:
    """Open the same persistent store exactly as a replacement process would."""
    fresh_settings = Settings(
        _env_file=None,
        data_dir=settings.data_dir,
        database_url=settings.database_url,
        study_root=settings.study_root,
        icloud_staging_root=settings.icloud_staging_root,
    )
    fresh_database = Database(fresh_settings.database_url)
    fresh_database.migrate()
    return (
        fresh_database,
        fresh_settings,
        ExistingArtifactImporter(fresh_database, fresh_settings),
    )


def _resume_phase_fixture(tmp_path, phase: str, pair: tuple[str, str], archive: bool):
    """Create a real incomplete adoption, then arrange one accepted durable state."""
    database, settings, importer, _request, old, icloud, target, adopted = _adoption_fixture(
        tmp_path
    )

    class ProcessDeath(BaseException):
        pass

    importer.checkpoint = lambda name: (
        (_ for _ in ()).throw(ProcessDeath())
        if name == "after-adoption-archive-copy"
        else None
    )
    with pytest.raises(ProcessDeath):
        importer.import_artifacts(adopted)
    canonical = settings.study_root / "Neuro" / "slides.pdf"
    payloads = {"old": old.read_bytes(), "target": target.read_bytes()}
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        audit.status = "preparing"
        audit.recovery_phase = phase
        Path(audit.imported_immutable_pdf_path or "").unlink(missing_ok=not archive)
        if archive:
            Path(audit.imported_immutable_pdf_path or "").write_bytes(target.read_bytes())
    canonical.write_bytes(payloads[pair[0]])
    icloud.write_bytes(payloads[pair[1]])
    return database, settings, importer, canonical, icloud, adopted


_RESUME_PHASES = [
    ("archive_copying", ("old", "old"), False, "after-adoption-archive-copy"),
    ("archive_copying", ("old", "old"), True, "after-adoption-archive-copy"),
    ("archived", ("old", "old"), True, "after-adoption-canonical_promoted-copy"),
    ("archived", ("target", "old"), True, "after-adoption-canonical_promoted-copy"),
    ("canonical_promoted", ("target", "old"), True, "after-adoption-icloud_promoted-copy"),
    ("canonical_promoted", ("target", "target"), True, "after-adoption-icloud_promoted-copy"),
    ("icloud_promoted", ("target", "target"), True, "after-adoption-precommit"),
    ("precommit", ("target", "target"), True, "after-adoption-precommit"),
    ("recovery_required", ("old", "old"), True, "after-adoption-canonical_promoted-copy"),
    ("recovery_required", ("target", "old"), True, "after-adoption-icloud_promoted-copy"),
    ("recovery_required", ("old", "target"), True, "after-adoption-precommit"),
    ("recovery_required", ("target", "target"), True, "after-adoption-precommit"),
]


@pytest.mark.parametrize(("phase", "pair", "archive", "boundary"), _RESUME_PHASES)
def test_adoption_resume_phase_second_process_death_migrates_then_completes(
    tmp_path, phase, pair, archive, boundary
):
    database, settings, importer, _canonical, _icloud, adopted = _resume_phase_fixture(
        tmp_path, phase, pair, archive
    )

    class ProcessDeath(BaseException):
        pass

    importer.checkpoint = lambda name: (
        (_ for _ in ()).throw(ProcessDeath()) if name == boundary else None
    )
    with pytest.raises(ProcessDeath):
        importer.import_artifacts(adopted)
    fresh_database, _fresh_settings, fresh_importer = _fresh_adoption_importer(settings)
    assert fresh_importer.import_artifacts(adopted).status == "complete"
    fresh_database.close()


@pytest.mark.parametrize(("phase", "pair", "archive", "boundary"), _RESUME_PHASES)
def test_adoption_resume_phase_claim_loss_migrates_then_completes(
    tmp_path, phase, pair, archive, boundary
):
    database, settings, importer, _canonical, _icloud, adopted = _resume_phase_fixture(
        tmp_path, phase, pair, archive
    )
    writes = _FencedWrites()
    importer.writes = writes

    def checkpoint(name: str) -> None:
        if name == boundary:
            writes.current.lost = True

    importer.checkpoint = checkpoint
    with pytest.raises(ArtifactWriteClaimLost):
        importer.import_artifacts(adopted)
    fresh_database, _fresh_settings, fresh_importer = _fresh_adoption_importer(settings)
    assert fresh_importer.import_artifacts(adopted).status == "complete"
    fresh_database.close()


@pytest.mark.parametrize(
    "boundary,expected_phase",
    [
        ("after-adoption-canonical_promoted-copy", "archived"),
        ("after-adoption-icloud_promoted-copy", "canonical_promoted"),
    ],
)
def test_adoption_process_death_after_mutable_copy_migrates_and_retries(
    tmp_path, boundary, expected_phase
):
    (database, settings, importer, _request, old, icloud, target, adopted) = _adoption_fixture(
        tmp_path
    )

    class SimulatedProcessDeath(BaseException):
        pass

    def checkpoint(phase: str) -> None:
        if phase == boundary:
            raise SimulatedProcessDeath("process terminated")

    importer.checkpoint = checkpoint
    with pytest.raises(SimulatedProcessDeath, match="process terminated"):
        importer.import_artifacts(adopted)

    canonical = settings.study_root / "Neuro" / "slides.pdf"
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        assert (audit.status, audit.recovery_phase) == ("preparing", expected_phase)
    expected = (
        (target.read_bytes(), old.read_bytes())
        if expected_phase == "archived"
        else (target.read_bytes(), target.read_bytes())
    )
    assert (canonical.read_bytes(), icloud.read_bytes()) == expected

    fresh_database, _fresh_settings, fresh_importer = _fresh_adoption_importer(settings)
    assert fresh_importer.import_artifacts(adopted).status == "complete"
    fresh_database.close()


@pytest.mark.parametrize(
    "boundary",
    [
        "after-adoption-canonical_promoted-copy",
        "after-adoption-icloud_promoted-copy",
    ],
)
def test_adoption_claim_loss_after_mutable_copy_migrates_and_retries(tmp_path, boundary):
    (_database, settings, importer, _request, _old, _icloud, _target, adopted) = _adoption_fixture(
        tmp_path
    )
    writes = _FencedWrites()
    importer.writes = writes

    def checkpoint(phase: str) -> None:
        if phase == boundary:
            writes.current.lost = True

    importer.checkpoint = checkpoint
    with pytest.raises(ArtifactWriteClaimLost, match="forced claim loss"):
        importer.import_artifacts(adopted)

    fresh_database, _fresh_settings, fresh_importer = _fresh_adoption_importer(settings)
    assert fresh_importer.import_artifacts(adopted).status == "complete"
    fresh_database.close()


@pytest.mark.parametrize(
    "boundary,successor_path",
    [
        ("after-adoption-canonical_promoted-copy", "canonical"),
        ("after-adoption-icloud_promoted-copy", "icloud"),
    ],
)
def test_lost_adoption_owner_never_cleans_successor_bytes_after_mutable_copy(
    tmp_path, boundary, successor_path
):
    (database, settings, importer, _request, old, icloud, _target, adopted) = _adoption_fixture(
        tmp_path
    )
    writes = _FencedWrites()
    importer.writes = writes
    canonical = settings.study_root / "Neuro" / "slides.pdf"
    successor = canonical if successor_path == "canonical" else icloud

    def checkpoint(phase: str) -> None:
        if phase == boundary:
            successor.write_bytes(b"successor bytes")
            writes.current.lost = True

    importer.checkpoint = checkpoint
    with pytest.raises(ArtifactWriteClaimLost, match="forced claim loss"):
        importer.import_artifacts(adopted)
    assert successor.read_bytes() == b"successor bytes"
    # Third bytes are never treated as an operator-repairable retry state.
    importer.writes = ArtifactWriteCoordinator(database, settings)
    importer.checkpoint = lambda _phase: None
    with pytest.raises(ExistingArtifactImportError, match="not resumable"):
        importer.import_artifacts(adopted)
    assert successor.read_bytes() == b"successor bytes"
    assert old.is_file()


@pytest.mark.parametrize(
    "canonical_target,icloud_target",
    [(False, False), (True, False), (False, True), (True, True)],
)
def test_adoption_recovery_required_accepts_exact_old_target_pairs(
    tmp_path, canonical_target, icloud_target
):
    (database, settings, importer, _request, old, icloud, target, adopted) = _adoption_fixture(
        tmp_path
    )
    importer.checkpoint = lambda phase: (
        (_ for _ in ()).throw(RuntimeError("force recovery"))
        if phase == "after-adoption-archive"
        else None
    )
    with pytest.raises(RuntimeError, match="force recovery"):
        importer.import_artifacts(adopted)
    canonical = settings.study_root / "Neuro" / "slides.pdf"
    canonical.write_bytes(target.read_bytes() if canonical_target else old.read_bytes())
    icloud.write_bytes(target.read_bytes() if icloud_target else old.read_bytes())
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        audit.status = "preparing"
        audit.recovery_phase = "recovery_required"
    importer.checkpoint = lambda _phase: None
    assert importer.import_artifacts(adopted).status == "complete"


def test_adoption_recovery_required_rejects_third_state_before_owner_mutation(tmp_path):
    (database, settings, importer, _request, old, icloud, _target, adopted) = _adoption_fixture(
        tmp_path
    )
    importer.checkpoint = lambda phase: (
        (_ for _ in ()).throw(RuntimeError("force recovery"))
        if phase == "after-adoption-archive"
        else None
    )
    with pytest.raises(RuntimeError, match="force recovery"):
        importer.import_artifacts(adopted)
    canonical = settings.study_root / "Neuro" / "slides.pdf"
    canonical.write_bytes(b"third canonical state")
    icloud.write_bytes(old.read_bytes())
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        audit.status = "preparing"
        audit.recovery_phase = "recovery_required"
        before = (audit.owner, audit.status, audit.recovery_phase, audit.error)
    with pytest.raises(ExistingArtifactImportError, match="not resumable"):
        importer.import_artifacts(adopted)
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        assert (audit.owner, audit.status, audit.recovery_phase, audit.error) == before
    assert canonical.read_bytes() == b"third canonical state"


def test_adoption_rollback_failure_retains_recovery_evidence(tmp_path, monkeypatch):
    (database, settings, importer, request, old, _icloud, _target, adopted) = _adoption_fixture(
        tmp_path
    )
    canonical = settings.study_root / "Neuro" / "slides.pdf"
    original = importer._replace_exact

    def replacement(claim, source, destination, digest):
        if source == old and destination == canonical:
            raise OSError("rollback destination unavailable")
        return original(claim, source, destination, digest)

    monkeypatch.setattr(importer, "_replace_exact", replacement)
    importer.checkpoint = lambda phase: (
        (_ for _ in ()).throw(RuntimeError("force rollback"))
        if phase == "after-adoption-canonical_promoted"
        else None
    )
    with pytest.raises(ExistingArtifactRecoveryError, match="rollback could not restore"):
        importer.import_artifacts(adopted)
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        assert audit.status == "preparing"
        assert audit.recovery_phase == "recovery_required"
        assert Path(audit.previous_immutable_pdf_path or "").is_file()
        assert Path(audit.imported_immutable_pdf_path or "").is_file()


@pytest.mark.parametrize(
    "consumer",
    [
        "completed-import",
        "generated-outline",
        "imported-outline",
        "generation-job",
        "anki-pin",
        "anki-malformed",
    ],
)
def test_adoption_consumers_reject_before_audit_or_file_mutation(tmp_path, consumer):
    (database, settings, importer, request, old, _icloud, _target, adopted) = _adoption_fixture(
        tmp_path
    )
    with database.session() as session:
        if consumer == "completed-import":
            session.add(
                ExistingArtifactImportModel(
                    id=str(uuid4()),
                    bundle_sha256="f" * 64,
                    lecture_id=request.lecture_id,
                    slide_revision_id=request.slides_revision_id,
                    transcript_sha256="a" * 64,
                    outline_sha256="b" * 64,
                    status="complete",
                )
            )
        elif consumer in {"generated-outline", "imported-outline"}:
            session.add(
                OutlineOutputModel(
                    lecture_id=request.lecture_id,
                    path=str(tmp_path / f"{consumer}.pdf"),
                    sha256="c" * 64,
                    current=False,
                    provenance_kind=(
                        "imported_notebooklm"
                        if consumer == "imported-outline"
                        else "notebooklm_generated"
                    ),
                    slide_revision_id=request.slides_revision_id,
                    slide_sha256=_sha256(old),
                    slide_source_sha256=request.slides_source_sha256,
                )
            )
        elif consumer == "generation-job":
            session.add(
                GenerationJobModel(
                    id=str(uuid4()),
                    lecture_id=request.lecture_id,
                    kind="outline",
                    pdf_revision_id=request.slides_revision_id,
                )
            )
        else:
            session.add(
                AnkiCurationJobModel(
                    id=str(uuid4()),
                    lecture_id=request.lecture_id,
                    target_deck="deck",
                    target_tag="tag",
                    index_snapshot_id="snapshot",
                    source_revision_ids_json=(
                        "not-json"
                        if consumer == "anki-malformed"
                        else json.dumps([request.slides_revision_id])
                    ),
                    instruction_sha256="d" * 64,
                    lcl_prompt_version="lcl",
                    judgment_rubric_version="judgment",
                    gap_prompt_version="gap",
                )
            )
    before = _import_state(database, settings)
    with pytest.raises(ExistingArtifactImportError):
        importer.import_artifacts(adopted)
    assert _import_state(database, settings) == before


@pytest.mark.parametrize(
    "source_pins",
    [
        '{"revision": 1}',
        '"not-a-list"',
        "1",
        "true",
        '["1"]',
        "[true]",
        "[0]",
        "[-1]",
        "[7, 7]",
    ],
)
def test_adoption_rejects_structurally_malformed_anki_source_pins_before_mutation(
    tmp_path, source_pins
):
    (database, settings, importer, request, _old, _icloud, _target, adopted) = _adoption_fixture(
        tmp_path
    )
    with database.session() as session:
        session.add(
            AnkiCurationJobModel(
                id=str(uuid4()),
                lecture_id=request.lecture_id,
                target_deck="deck",
                target_tag="tag",
                index_snapshot_id="snapshot",
                source_revision_ids_json=source_pins,
                instruction_sha256="d" * 64,
                lcl_prompt_version="lcl",
                judgment_rubric_version="judgment",
                gap_prompt_version="gap",
            )
        )
    before = _import_state(database, settings)
    with pytest.raises(ExistingArtifactImportError, match="Anki source pin is malformed"):
        importer.import_artifacts(adopted)
    assert _import_state(database, settings) == before


def test_adoption_rejects_anki_pin_of_the_current_slide_before_mutation(tmp_path):
    (database, settings, importer, request, _old, _icloud, _target, adopted) = _adoption_fixture(
        tmp_path
    )
    with database.session() as session:
        session.add(
            AnkiCurationJobModel(
                id=str(uuid4()),
                lecture_id=request.lecture_id,
                target_deck="deck",
                target_tag="tag",
                index_snapshot_id="snapshot",
                source_revision_ids_json=json.dumps([request.slides_revision_id]),
                instruction_sha256="d" * 64,
                lcl_prompt_version="lcl",
                judgment_rubric_version="judgment",
                gap_prompt_version="gap",
            )
        )
    before = _import_state(database, settings)
    with pytest.raises(ExistingArtifactImportError, match="already consumed"):
        importer.import_artifacts(adopted)
    assert _import_state(database, settings) == before


def test_adoption_exact_completed_bundle_is_idempotent_after_downstream_consumer(tmp_path):
    (database, settings, importer, request, _old, _icloud, _target, adopted) = _adoption_fixture(
        tmp_path
    )
    importer.import_artifacts(adopted)
    with database.session() as session:
        session.add(
            GenerationJobModel(
                id=str(uuid4()),
                lecture_id=request.lecture_id,
                kind="outline",
                pdf_revision_id=request.slides_revision_id,
            )
        )
    before = _import_state(database, settings)
    retried = importer.import_artifacts(adopted)
    assert retried.idempotent is True
    assert _import_state(database, settings) == before


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("immutable_transcript_path", "same-audit-transcript.txt"),
        ("immutable_outline_path", "nested/outline.pdf"),
    ],
)
def test_completed_adoption_idempotency_rejects_same_byte_repointed_audit_evidence(
    tmp_path, field, replacement
):
    (database, _settings, importer, _request, _old, _icloud, _target, adopted) = _adoption_fixture(
        tmp_path
    )
    result = importer.import_artifacts(adopted)
    with database.session() as session:
        audit = session.get(ExistingArtifactImportModel, result.import_id)
        assert audit is not None
        original = Path(getattr(audit, field) or "")
        repointed = original.parent / replacement
        repointed.parent.mkdir(parents=True, exist_ok=True)
        repointed.write_bytes(original.read_bytes())
        setattr(audit, field, str(repointed))
    before = _import_state(database, importer.settings)

    with pytest.raises(ExistingArtifactImportError, match="exact current artifact identity"):
        importer.import_artifacts(adopted)

    assert _import_state(database, importer.settings) == before


@pytest.mark.parametrize(
    "artifact",
    [
        "old-office-pdf",
        "imported-pdf",
        "pptx-immutable",
        "pptx-canonical",
        "pdf-canonical",
        "pdf-icloud",
        "transcript-immutable",
        "transcript-canonical",
        "outline-immutable",
        "outline-canonical",
    ],
)
@pytest.mark.parametrize("operation", ["delete", "tamper", "same-byte-symlink", "ancestor-symlink"])
def test_completed_adoption_idempotency_rejects_each_required_file(
    tmp_path, artifact, operation
):
    (database, settings, importer, request, _old, icloud, _target, adopted) = _adoption_fixture(
        tmp_path
    )
    result = importer.import_artifacts(adopted)
    with database.session() as session:
        audit = session.get(ExistingArtifactImportModel, result.import_id)
        slide = session.get(StudyRevisionModel, request.slides_revision_id)
        assert audit is not None and slide is not None
        paths = {
            "old-office-pdf": Path(audit.previous_immutable_pdf_path or ""),
            "imported-pdf": Path(audit.imported_immutable_pdf_path or ""),
            "pptx-immutable": Path(slide.immutable_source_path),
            "pptx-canonical": Path(slide.canonical_source_path or ""),
            "pdf-canonical": Path(slide.canonical_derived_path or ""),
            "pdf-icloud": icloud,
            "transcript-immutable": Path(audit.immutable_transcript_path or ""),
            "transcript-canonical": Path(audit.canonical_transcript_path or ""),
            "outline-immutable": Path(audit.immutable_outline_path or ""),
            "outline-canonical": Path(audit.canonical_outline_path or ""),
        }
    path = paths[artifact]
    if operation == "delete":
        path.unlink()
    elif operation == "tamper":
        path.write_bytes(b"tampered")
    else:
        try:
            if operation == "same-byte-symlink":
                moved = tmp_path / f"{artifact}-same-bytes{path.suffix}"
                path.rename(moved)
                path.symlink_to(moved)
            else:
                moved = tmp_path / f"{artifact}-parent"
                path.parent.rename(moved)
                path.parent.symlink_to(moved, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"symlink support unavailable: {error}")
    before = _import_state(database, settings)
    with pytest.raises(ExistingArtifactImportError):
        importer.import_artifacts(adopted)
    assert _import_state(database, settings) == before


def _failed_adoption_for_retry(tmp_path):
    (database, settings, importer, request, old, icloud, target, adopted) = _adoption_fixture(
        tmp_path
    )
    importer.checkpoint = lambda phase: (
        (_ for _ in ()).throw(RuntimeError("interrupted"))
        if phase == "after-adoption-archive"
        else None
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        importer.import_artifacts(adopted)
    importer.checkpoint = lambda _phase: None
    return database, settings, importer, request, old, icloud, target, adopted


@pytest.mark.parametrize(
    "tamper",
    [
        "imported-path",
        "previous-path",
        "operator",
        "reason",
        "confirmation",
        "phase",
        "status",
    ],
)
def test_hostile_adoption_retry_metadata_rejects_before_mutation(tmp_path, tamper):
    (database, settings, importer, request, old, _icloud, _target, adopted) = (
        _failed_adoption_for_retry(tmp_path)
    )
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        if tamper == "imported-path":
            audit.imported_immutable_pdf_path = str(tmp_path / "escape.pdf")
        elif tamper == "previous-path":
            audit.previous_immutable_pdf_path = str(tmp_path / "other.pdf")
        elif tamper == "operator":
            audit.adoption_operator = "other"
        elif tamper == "reason":
            audit.adoption_reason = "other"
        elif tamper == "confirmation":
            audit.adoption_confirmed_at = ""
        elif tamper == "phase":
            audit.recovery_phase = "committed"
        else:
            audit.status = "complete"
    before = _import_state(database, settings)
    with pytest.raises(ExistingArtifactImportError):
        importer.import_artifacts(adopted)
    assert _import_state(database, settings) == before


@pytest.mark.parametrize("destination", ["canonical", "icloud"])
def test_hostile_adoption_retry_third_state_bytes_reject_before_mutation(tmp_path, destination):
    (database, settings, importer, request, old, icloud, _target, adopted) = (
        _failed_adoption_for_retry(tmp_path)
    )
    path = settings.study_root / "Neuro" / "slides.pdf" if destination == "canonical" else icloud
    path.write_bytes(b"third state")
    before = _import_state(database, settings)
    with pytest.raises(ExistingArtifactImportError):
        importer.import_artifacts(adopted)
    assert _import_state(database, settings) == before


def test_adoption_real_same_lecture_claim_contention_has_no_mutation(tmp_path):
    (database, settings, importer, request, _old, _icloud, _target, adopted) = _adoption_fixture(
        tmp_path
    )
    before = _import_state(database, settings)
    with ArtifactWriteCoordinator(database, settings).claim(request.lecture_id, "holder"):
        with pytest.raises(ArtifactWriteContended):
            importer.import_artifacts(adopted)
    assert _import_state(database, settings) == before


@pytest.mark.parametrize("status", ["failed", "preparing"])
@pytest.mark.parametrize(
    "change",
    [
        "audit-id",
        "slide-source",
        "transcript-filename",
        "transcript-hash",
        "outline-filename",
        "outline-hash",
    ],
)
def test_hostile_adoption_retry_identity_changes_reject_before_mutation(tmp_path, status, change):
    (database, settings, importer, request, old, _icloud, _target, adopted) = (
        _failed_adoption_for_retry(tmp_path)
    )
    changed = adopted
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        audit.status = status
        if change == "audit-id":
            audit.id = "not-a-canonical-uuid"
        elif change == "slide-source":
            changed = replace(adopted, slides_source_sha256="e" * 64)
        elif change == "transcript-filename":
            renamed = tmp_path / "renamed.txt"
            renamed.write_bytes(adopted.cleaned_transcript.read_bytes())
            changed = replace(adopted, cleaned_transcript=renamed)
        elif change == "transcript-hash":
            changed = replace(adopted, cleaned_transcript_sha256="e" * 64)
        elif change == "outline-filename":
            renamed = tmp_path / "renamed.pdf"
            renamed.write_bytes(adopted.notebooklm_outline.read_bytes())
            changed = replace(adopted, notebooklm_outline=renamed)
        else:
            changed = replace(adopted, notebooklm_outline_sha256="e" * 64)
    before = _import_state(database, settings)
    with pytest.raises(ExistingArtifactImportError):
        importer.import_artifacts(changed)
    assert _import_state(database, settings) == before


@pytest.mark.parametrize("symlink_kind", ["root", "audit", "destination"])
def test_hostile_adoption_retry_symlink_paths_reject_before_mutation(tmp_path, symlink_kind):
    (database, settings, importer, request, _old, _icloud, _target, adopted) = (
        _failed_adoption_for_retry(tmp_path)
    )
    root = settings.data_dir / "artifacts" / "existing-imports"
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        audit_dir = root / audit.id
    try:
        if symlink_kind == "root":
            moved = tmp_path / "imports-real"
            root.rename(moved)
            root.symlink_to(moved, target_is_directory=True)
        elif symlink_kind == "audit":
            moved = tmp_path / "audit-real"
            audit_dir.rename(moved)
            audit_dir.symlink_to(moved, target_is_directory=True)
        else:
            destination = audit_dir / "derived-slide.pdf"
            moved = tmp_path / "derived-real.pdf"
            destination.rename(moved)
            destination.symlink_to(moved)
    except OSError as error:
        pytest.skip(f"symlink support unavailable: {error}")
    before = _import_state(database, settings)
    with pytest.raises(ExistingArtifactImportError):
        importer.import_artifacts(adopted)
    assert _import_state(database, settings) == before


def test_real_artifact_claim_is_independent_for_distinct_lectures(tmp_path):
    (database, settings, importer, request, _old, _icloud, _target, _adopted) = _adoption_fixture(
        tmp_path
    )
    second = CatalogRepository(database).upsert_lecture(
        LectureInput("Neuro", 1, 25, "Other", "", None)
    )
    with importer.writes.claim(request.lecture_id, "first"):
        with importer.writes.claim(second, "second") as second_claim:
            second_claim.assert_owned()
