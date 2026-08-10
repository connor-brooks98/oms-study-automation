import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from reportlab.pdfgen.canvas import Canvas
from sqlalchemy import select

from oms_hub import cli
from oms_hub.artifact_writes import ArtifactWriteClaimLost, ArtifactWriteContended
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
    verify_a0_operator_files,
)
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
def test_renamed_same_byte_sources_fail_closed_without_reusing_provenance(
    tmp_path, after_failure
):
    database, settings, importer, request = _request_fixture(tmp_path)
    if after_failure:
        importer.checkpoint = lambda phase: (
            (_ for _ in ()).throw(RuntimeError("forced"))
            if phase == "after-copy"
            else None
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
    authoritative_pptx_sha256 = (
        "b1c7abc3fb5d86476a3477d397e679ec42e61cff982fcec9dcb55a9d0a9c5469"
    )
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
        tmp_path / name
        for name in ("slides.pptx", "slides.pdf", "cleaned.txt", "outline.pdf")
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
    assert len(
        {
            A0_PPTX_SHA256,
            A0_PDF_SHA256,
            A0_CLEANED_TRANSCRIPT_SHA256,
            A0_OUTLINE_SHA256,
        }
    ) == 4
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
