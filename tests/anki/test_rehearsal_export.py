from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest

from oms_hub.anki.domain import (
    CreateCurationJob,
    CurationStage,
    CurationState,
    PipelineContractVersion,
    StageArtifact,
)
from oms_hub.anki.index import AnkiIndex
from oms_hub.anki.normalize import NormalizedNote
from oms_hub.anki.rehearsal.capsule import CapsuleIntegrityError, verify_capsule
from oms_hub.anki.rehearsal.export import (
    _close_read_only_memmap,
    _copy_bound_job_artifact,
    _export_job_scoped_database,
    _online_backup,
    _registered_root_relative,
    export_capsule,
)
from oms_hub.anki.rehearsal.regressions import historical_regression_catalog
from oms_hub.anki.repository import AnkiCurationRepository
from oms_hub.anki.semantic.domain import DocumentRecord
from oms_hub.anki.semantic.store import SemanticSnapshotStore
from oms_hub.db import Database
from oms_hub.ingestion.domain import StagedUpload, UploadKind, UploadState
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.models import LectureModel
from oms_hub.study_generation.domain import GenerationKind
from oms_hub.study_generation.repository import GenerationRepository


@pytest.fixture(autouse=True)
def _restore_tmp_path_permissions(tmp_path: Path) -> None:
    """Keep deliberately read-only export results removable on Windows."""
    yield
    try:
        for path in sorted(tmp_path.rglob("*"), reverse=True):
            mode = stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR
            if path.is_dir():
                mode |= stat.S_IRUSR | stat.S_IXUSR
            os.chmod(path, mode)
    except OSError:
        pass
    try:
        os.chmod(tmp_path, stat.S_IMODE(tmp_path.stat().st_mode) | stat.S_IWUSR | stat.S_IXUSR)
    except OSError:
        pass


class _FixedEmbedder:
    model_name = "capsule-fixture"

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.asarray([[1.0, 0.0] for _ in texts], dtype=np.float32)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _frozen_repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "frozen-repository"
    prompt = repository / "src" / "oms_hub" / "anki" / "prompt_assets" / "s2.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("frozen prompt asset\n", encoding="utf-8")
    _git(repository, "init")
    _git(repository, "config", "user.email", "capsule@example.test")
    _git(repository, "config", "user.name", "Capsule Fixture")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "frozen capsule fixture")
    return (
        repository,
        _git(repository, "rev-parse", "HEAD"),
        _git(repository, "rev-parse", "HEAD^{tree}"),
    )


def _failed_job_fixture(tmp_path: Path, *, failed: bool = True) -> dict[str, object]:
    repository_root, commit, tree = _frozen_repository(tmp_path)
    data_root = tmp_path / "a0-data"
    anki_root = data_root / "anki"
    database_path = data_root / "hub.db"
    data_root.mkdir()
    database = Database(f"sqlite:///{database_path}")
    database.migrate()
    try:
        with database.session() as session:
            lecture = LectureModel(
                subject="Heme Lymph",
                exam_number=1,
                lecture_number=1,
                topic="Capsule export",
                lecturer="Fixture",
            )
            session.add(lecture)
            session.flush()
            lecture_id = lecture.id

        ingestion = IngestionRepository(database)
        staged_path = data_root / "staged" / "lecture.txt"
        staged_path.parent.mkdir()
        staged_path.write_text("source lecture\n", encoding="utf-8")
        batch_id = ingestion.create_batch(UploadKind.TRANSCRIPTS)
        item_id = str(uuid4())
        ingestion.add_item(
            UploadKind.TRANSCRIPTS,
            StagedUpload(
                batch_id=batch_id,
                item_id=item_id,
                path=staged_path,
                sha256=_sha256(staged_path),
                size_bytes=staged_path.stat().st_size,
                original_filename="lecture.txt",
            ),
        )
        ingestion.set_manual_assignment(item_id, lecture_id)
        revision = ingestion.begin_revision(item_id, data_root / "revisions")
        revision.immutable_source_path.parent.mkdir(parents=True)
        revision.immutable_source_path.write_bytes(staged_path.read_bytes())
        assert revision.immutable_derived_path is not None
        revision.immutable_derived_path.write_text("cleaned lecture\n", encoding="utf-8")
        canonical = data_root / "study" / "cleaned.txt"
        canonical.parent.mkdir()
        canonical.write_bytes(revision.immutable_derived_path.read_bytes())
        revision = ingestion.update_transcript_revision(
            revision.id,
            derived_sha256=_sha256(canonical),
            prompt_sha256="a" * 64,
            canonical_derived_path=canonical,
        )
        ingestion.finish_revision(
            item_id,
            revision.id,
            UploadState.COMPLETE,
            current=True,
        )

        outline_path = data_root / "study" / "outline.pdf"
        outline_path.write_bytes(b"%PDF-frozen-outline\n")
        generation = GenerationRepository(database).queue(lecture_id, GenerationKind.OUTLINE)
        outline = GenerationRepository(database).record_outline(
            lecture_id,
            generation.id,
            outline_path,
            _sha256(outline_path),
        )

        note = NormalizedNote(
            note_id=101,
            model_name="Cloze",
            text="capsule note",
            extra="fixture",
            raw_fields={"Text": "capsule note", "Extra": "fixture"},
            tags=("capsule",),
            card_ids=(201,),
            media=(),
            token_signature="capsule note",
            content_sha256="b" * 64,
        )
        companion = AnkiIndex(anki_root / "companion", embedder=_FixedEmbedder())
        companion.rebuild([note], snapshot_id="companion-generation", fingerprint="c" * 64)
        semantic = SemanticSnapshotStore(anki_root / "semantic").replace(
            [
                DocumentRecord(
                    note_id=101,
                    text="capsule note",
                    content_hash=hashlib.sha256(b"capsule note").hexdigest(),
                )
            ],
            np.asarray([[1.0, 0.0]], dtype=np.float32),
            model="capsule-fixture",
        )
        curation = AnkiCurationRepository(database)
        job = curation.create_job(
            CreateCurationJob(
                lecture_id=lecture_id,
                block_id="capsule-block",
                source_revision_ids=(revision.id,),
                source_revision_hashes={revision.id: _sha256(staged_path)},
                deck_allowlist=("Fixture",),
                tag_allowlist=("capsule",),
                instruction_text="Reproduce the recorded failure.",
                target_deck="OMS::Capsule",
                target_tag="Capsule",
                index_snapshot_id="companion-generation",
                lcl_prompt_version="lcl-fixture",
                judgment_rubric_version="judgment-fixture",
                gap_prompt_version="gap-fixture",
                provider="anthropic",
                model="fixture-model",
                pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
                companion_generation="companion-generation",
                semantic_generation=str(semantic.generation),
                summary_outline_id=outline.id,
                summary_outline_sha256=outline.sha256,
            )
        )
        source_index_payload = {"source_index": {"generation": "fixture"}}
        source_index_metadata: dict[str, object] = {}
        source_index_document = {
            "artifact_version": 3,
            "job_id": str(job.id),
            "stage": CurationStage.SOURCE_INDEX.value,
            "kind": "card_centric_source_index",
            "pipeline_contract_version": job.pipeline_contract_version.value,
            "model_config_sha256": job.model_config_sha256,
            "input_sha256": "d" * 64,
            "payload": source_index_payload,
            "metadata": source_index_metadata,
            "recovery_product": {
                "kind": "card_centric_source_index",
                "payload": source_index_payload,
                "metadata": source_index_metadata,
                "usage": None,
                "cache_hits": 0,
                "candidates": None,
                "source_evidence": None,
                "gap_cards": None,
                "job_pins": {},
                "blocking_error": None,
            },
        }
        source_index_encoded = (
            json.dumps(source_index_document, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        source_index_sha256 = hashlib.sha256(source_index_encoded).hexdigest()
        source_index_relative = (
            f"{job.id}/{CurationStage.SOURCE_INDEX.value}/{source_index_sha256}.json"
        )
        artifact_path = anki_root / "artifacts" / Path(source_index_relative)
        artifact_path.parent.mkdir(parents=True)
        artifact_path.write_bytes(source_index_encoded)
        curation.save_stage_artifact(
            job.id,
            StageArtifact(
                artifact_id=f"{CurationStage.SOURCE_INDEX.value}:{source_index_sha256}",
                stage=CurationStage.SOURCE_INDEX,
                kind="card_centric_source_index",
                relative_path=source_index_relative,
                input_sha256="d" * 64,
                content_sha256=source_index_sha256,
                pipeline_contract_version=job.pipeline_contract_version,
                model_config_sha256=job.model_config_sha256,
            ),
        )
        if failed:
            claimed = curation.claim_next_job(
                datetime(2026, 8, 12, tzinfo=UTC),
                worker_id="fixture",
                lease_seconds=60,
            )
            assert claimed is not None and claimed.id == job.id
            curation.fail_job(
                job.id,
                "fixture",
                "recorded fixture failure",
                expected_state=CurationState.PREFLIGHT,
                now=datetime(2026, 8, 12, 0, 0, 1, tzinfo=UTC),
            )
    finally:
        database.close()
    # The production exporter requires an explicitly quiescent source.  Make
    # the fixture represent the stopped Hub state rather than a live WAL owner.
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()

    (data_root / "collection.anki2").write_bytes(b"must not be exported")
    (data_root / ".env").write_text("API_KEY=must-not-escape\n", encoding="utf-8")
    return {
        "repository": repository_root,
        "commit": commit,
        "tree": tree,
        "data": data_root,
        "database": database_path,
        "anki": anki_root,
        "job": job,
        "source": revision.immutable_source_path,
        "canonical": canonical,
        "outline": outline_path,
        "artifact_path": artifact_path,
        "artifact_relative": source_index_relative,
    }


def _export(
    fixture: dict[str, object],
    destination: Path,
    *,
    expected_companion_count: int = 1,
    expected_semantic_count: int = 1,
    expected_tree: str | None = None,
) -> Path:
    return export_capsule(
        repository_root=fixture["repository"],  # type: ignore[arg-type]
        database_path=fixture["database"],  # type: ignore[arg-type]
        anki_root=fixture["anki"],  # type: ignore[arg-type]
        job_id=fixture["job"].id,  # type: ignore[union-attr]
        destination=destination,
        source_roots={
            "a0data": fixture["data"],  # type: ignore[dict-item]
            "repository": fixture["repository"],  # type: ignore[dict-item]
        },
        expected_commit=fixture["commit"],  # type: ignore[arg-type]
        expected_tree=expected_tree or fixture["tree"],  # type: ignore[arg-type]
        expected_companion_count=expected_companion_count,
        expected_semantic_count=expected_semantic_count,
    )


def test_export_is_hash_verified_complete_read_only_and_source_immutable(tmp_path: Path) -> None:
    fixture = _failed_job_fixture(tmp_path)
    database_before = _sha256(fixture["database"])  # type: ignore[arg-type]
    source_before = {
        path: _sha256(path)
        for path in (fixture["source"], fixture["canonical"], fixture["outline"])  # type: ignore[arg-type]
    }
    capsule = _export(fixture, tmp_path / "capsule")

    manifest = verify_capsule(capsule)
    assert manifest.identity.commit_sha == fixture["commit"]
    assert manifest.identity.tree_sha == fixture["tree"]
    assert manifest.identity.companion_note_count == 1
    assert manifest.identity.semantic_note_count == 1
    artifact_relative = fixture["artifact_relative"]  # type: ignore[assignment]
    copied_artifact = capsule / "anki" / "artifacts" / artifact_relative
    assert copied_artifact.is_file()
    assert _sha256(copied_artifact) == _sha256(fixture["artifact_path"])  # type: ignore[arg-type]
    assert not (fixture["anki"] / "jobs" / str(fixture["job"].id)).exists()  # type: ignore[operator,union-attr]
    prompt_asset = (
        capsule / "sources" / "repository" / "src" / "oms_hub" / "anki" / "prompt_assets" / "s2.md"
    )
    assert prompt_asset.read_text(encoding="utf-8") == "frozen prompt asset\n"
    assert json.loads((capsule / "regressions.json").read_text(encoding="utf-8")) == (
        historical_regression_catalog()
    )
    for source in source_before:
        copied = capsule / "sources" / "a0data" / source.relative_to(fixture["data"])  # type: ignore[arg-type]
        assert _sha256(copied) == source_before[source]
    assert not any(
        path.name.casefold() in {"collection.anki2", ".env"} for path in capsule.rglob("*")
    )
    assert _sha256(fixture["database"]) == database_before  # type: ignore[arg-type]
    assert {path: _sha256(path) for path in source_before} == source_before
    assert all(not stat.S_IMODE(path.stat().st_mode) & stat.S_IWUSR for path in capsule.rglob("*"))
    assert not stat.S_IMODE(capsule.stat().st_mode) & stat.S_IWUSR
    snapshot = json.loads((capsule / "source-snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["credentials_exported"] is False
    assert snapshot["database_export"] == "job-scoped-allowlist"
    assert snapshot["components"][str(fixture["database"])] == {
        "sha256": database_before,
        "bytes": fixture["database"].stat().st_size,  # type: ignore[union-attr]
    }
    assert snapshot["components"]["database_sidecars"] == {
        "-wal": {"present": False},
        "-shm": {"present": False},
    }
    assert snapshot["components"]["job_artifacts"] == {
        str(artifact_relative): {
            "artifact_id": f"source_index:{_sha256(fixture['artifact_path'])}",  # type: ignore[arg-type]
            "stage": "source_index",
            "kind": "card_centric_source_index",
            "sha256": _sha256(fixture["artifact_path"]),  # type: ignore[arg-type]
            "bytes": fixture["artifact_path"].stat().st_size,  # type: ignore[union-attr]
        }
    }
    capsule_uri = f"file:{(capsule / 'hub/hub.db').as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(capsule_uri, uri=True)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        # The schema is retained for Hub migrations, but sensitive/unrelated
        # tables carry no copied rows in the allowlisted logical export.
        assert "google_connection" in tables
        assert connection.execute("SELECT COUNT(*) FROM google_connection").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM notebook_mappings").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM published_quizzes").fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM anki_provider_attempt_events"
        ).fetchone() == (0,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_export_validation_closes_mapped_semantic_vectors_before_directory_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _failed_job_fixture(tmp_path)
    destination = tmp_path / "capsule"
    captured: dict[str, object] = {}
    original_load = SemanticSnapshotStore.load
    original_rename = Path.rename

    def tracked_load(store: SemanticSnapshotStore, **kwargs: object) -> object:
        snapshot = original_load(store, **kwargs)
        captured["snapshot"] = snapshot
        return snapshot

    def checked_rename(source: Path, target: Path) -> Path:
        if target == destination:
            snapshot = captured["snapshot"]
            mapping = snapshot.matrix._mmap  # type: ignore[attr-defined]
            assert mapping.closed
        return original_rename(source, target)

    monkeypatch.setattr(SemanticSnapshotStore, "load", tracked_load)
    monkeypatch.setattr(Path, "rename", checked_rename)
    assert _export(fixture, destination) == destination


def test_close_read_only_memmap_releases_mapping(tmp_path: Path) -> None:
    matrix_path = tmp_path / "vectors.npy"
    np.save(matrix_path, np.array([[1, 0]], dtype=np.float16), allow_pickle=False)
    matrix = np.load(matrix_path, mmap_mode="r", allow_pickle=False)
    mapping = matrix._mmap
    assert mapping is not None and not mapping.closed
    _close_read_only_memmap(matrix)
    assert mapping.closed


def test_export_closes_real_existing_artifact_fk_cycle_without_unrelated_rows(
    tmp_path: Path,
) -> None:
    fixture = _failed_job_fixture(tmp_path)
    database_path = fixture["database"]  # type: ignore[assignment]
    previous_pdf = fixture["data"] / "imports" / "previous-immutable.pdf"  # type: ignore[operator]
    previous_pdf.parent.mkdir()
    previous_pdf.write_bytes(b"%PDF-previous-immutable\n")
    connection = sqlite3.connect(database_path)
    try:
        revision_id = connection.execute("SELECT id FROM study_revisions").fetchone()[0]
        outline_id = connection.execute("SELECT id FROM outline_outputs").fetchone()[0]
        lecture_id = connection.execute("SELECT id FROM lectures").fetchone()[0]
        import_id = str(uuid4())
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN")
        connection.execute("PRAGMA defer_foreign_keys = ON")
        connection.execute(
            """INSERT INTO existing_artifact_imports
               (id, bundle_sha256, lecture_id, slide_revision_id, transcript_sha256, outline_sha256,
                subject, exam_number, lecture_number, topic, status, attempts,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                import_id,
                "d" * 64,
                lecture_id,
                revision_id,
                "e" * 64,
                "f" * 64,
                "Fixture",
                1,
                1,
                "Cycle",
                "complete",
                1,
                "2026-08-12T00:00:00+00:00",
                "2026-08-12T00:00:00+00:00",
            ),
        )
        connection.execute(
            "UPDATE study_revisions SET import_id = ? WHERE id = ?", (import_id, revision_id)
        )
        connection.execute(
            "UPDATE outline_outputs SET import_id = ? WHERE id = ?", (import_id, outline_id)
        )
        connection.execute(
            "UPDATE existing_artifact_imports SET transcript_revision_id = ?, "
            "outline_id = ?, previous_immutable_pdf_path = ? WHERE id = ?",
            (revision_id, outline_id, str(previous_pdf), import_id),
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        connection.commit()
    finally:
        connection.close()
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()

    capsule = _export(fixture, tmp_path / "capsule")
    exported = sqlite3.connect(
        f"file:{(capsule / 'hub/hub.db').as_posix()}?mode=ro&immutable=1", uri=True
    )
    try:
        assert exported.execute("PRAGMA foreign_key_check").fetchall() == []
        assert exported.execute("SELECT id FROM existing_artifact_imports").fetchall() == [
            (import_id,)
        ]
        assert exported.execute("SELECT COUNT(*) FROM anki_curation_jobs").fetchone() == (1,)
        assert exported.execute("SELECT COUNT(*) FROM study_revisions").fetchone() == (1,)
        assert exported.execute("SELECT COUNT(*) FROM outline_outputs").fetchone() == (1,)
        assert exported.execute(
            "SELECT previous_immutable_pdf_path FROM existing_artifact_imports WHERE id = ?",
            (import_id,),
        ).fetchone() == (str(previous_pdf),)
    finally:
        exported.close()
    copied_previous_pdf = (
        capsule / "sources" / "a0data" / previous_pdf.relative_to(fixture["data"])  # type: ignore[arg-type]
    )
    assert copied_previous_pdf.read_bytes() == previous_pdf.read_bytes()


def test_export_rejects_registered_file_missing_from_job_scoped_database_closure(
    tmp_path: Path,
) -> None:
    fixture = _failed_job_fixture(tmp_path)
    missing = fixture["data"] / "missing" / "registered.txt"  # type: ignore[operator]
    connection = sqlite3.connect(fixture["database"])  # type: ignore[arg-type]
    try:
        connection.execute(
            "UPDATE study_revisions SET canonical_source_path = ?", (str(missing),)
        )
        connection.commit()
    finally:
        connection.close()
    connection = sqlite3.connect(fixture["database"])  # type: ignore[arg-type]
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()

    with pytest.raises(CapsuleIntegrityError, match="source artifact is unavailable"):
        _export(fixture, tmp_path / "missing-registered-file-capsule")


def test_export_rejects_registered_file_outside_approved_roots(tmp_path: Path) -> None:
    fixture = _failed_job_fixture(tmp_path)
    outside = tmp_path / "outside-root" / "registered.txt"
    outside.parent.mkdir()
    outside.write_text("outside", encoding="utf-8")
    connection = sqlite3.connect(fixture["database"])  # type: ignore[arg-type]
    try:
        connection.execute(
            "UPDATE study_revisions SET canonical_source_path = ?", (str(outside),)
        )
        connection.commit()
    finally:
        connection.close()
    connection = sqlite3.connect(fixture["database"])  # type: ignore[arg-type]
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()

    with pytest.raises(CapsuleIntegrityError, match="outside registered roots"):
        _export(fixture, tmp_path / "outside-registered-file-capsule")


def test_export_rejects_registered_file_through_indirect_ancestor(tmp_path: Path) -> None:
    fixture = _failed_job_fixture(tmp_path)
    outside = tmp_path / "outside-root" / "escaped.txt"
    outside.parent.mkdir()
    outside.write_text("outside", encoding="utf-8")
    indirect = fixture["data"] / "indirect"  # type: ignore[operator]
    try:
        indirect.symlink_to(outside.parent, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable for the indirect-path regression: {exc}")
    indirect_path = indirect / outside.name
    connection = sqlite3.connect(fixture["database"])  # type: ignore[arg-type]
    try:
        connection.execute(
            "UPDATE study_revisions SET canonical_source_path = ?", (str(indirect_path),)
        )
        connection.commit()
    finally:
        connection.close()
    connection = sqlite3.connect(fixture["database"])  # type: ignore[arg-type]
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()

    destination = tmp_path / "indirect-registered-file-capsule"
    with pytest.raises(CapsuleIntegrityError, match="source artifact is unavailable or indirect"):
        _export(fixture, destination)
    assert not destination.exists()


def test_export_omits_sensitive_rows_and_scrubs_outline_generation_details(tmp_path: Path) -> None:
    fixture = _failed_job_fixture(tmp_path)
    connection = sqlite3.connect(fixture["database"])
    try:
        connection.execute(
            "INSERT INTO google_connection (id, state, account_email, updated_at) "
            "VALUES (1, 'connected', 'student@example.test', '2026-08-12T00:00:00+00:00')"
        )
        connection.execute(
            "UPDATE generation_jobs SET error = ?, prompt_path = ?, notebook_id = ?, "
            "notebook_answer = ?, gemini_quiz_id = ?, quiz_url = ?",
            (
                "provider error with secret context",
                "/private/source/prompt.md",
                "notebook-secret",
                "raw response",
                "quiz-secret",
                "https://quiz.example/secret",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    # The test has deliberately changed the source; re-establish the explicit
    # stopped/checkpointed fixture state before running the read-only export.
    connection = sqlite3.connect(fixture["database"])
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    capsule = _export(fixture, tmp_path / "capsule")
    connection = sqlite3.connect(
        f"file:{(capsule / 'hub/hub.db').as_posix()}?mode=ro&immutable=1", uri=True
    )
    try:
        assert connection.execute("SELECT COUNT(*) FROM google_connection").fetchone() == (0,)
        assert connection.execute(
            "SELECT error, prompt_path, notebook_id, notebook_answer, gemini_quiz_id, quiz_url "
            "FROM generation_jobs"
        ).fetchone() == (None, None, None, None, None, None)
    finally:
        connection.close()


def test_export_rejects_wal_created_during_immutable_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _failed_job_fixture(tmp_path)

    def race(source: Path, destination: Path) -> None:
        _online_backup(source, destination)
        Path(str(source) + "-wal").write_bytes(b"appeared-during-backup")

    monkeypatch.setattr("oms_hub.anki.rehearsal.export._online_backup", race)
    with pytest.raises(CapsuleIntegrityError, match="WAL is not inert"):
        _export(fixture, tmp_path / "racy-capsule")


def _write_inert_database_sidecars(database: Path) -> tuple[Path, Path]:
    wal = Path(str(database) + "-wal")
    shm = Path(str(database) + "-shm")
    wal.write_bytes(b"")
    shm.write_bytes(b"inert-shm" * 4096)
    return wal, shm


def test_export_accepts_stable_inert_zero_wal_with_shm_and_records_provenance(
    tmp_path: Path,
) -> None:
    fixture = _failed_job_fixture(tmp_path)
    wal, shm = _write_inert_database_sidecars(fixture["database"])  # type: ignore[arg-type]
    before = {path: _sha256(path) for path in (wal, shm)}
    capsule = _export(fixture, tmp_path / "inert-sidecars-capsule")
    snapshot = json.loads((capsule / "source-snapshot.json").read_text(encoding="utf-8"))
    sidecars = snapshot["components"]["database_sidecars"]
    assert snapshot["database_sidecar_state"] == "inert_zero_wal_with_shm"
    assert sidecars["-wal"] == {"present": True, "bytes": 0, "sha256": _sha256(wal)}
    assert sidecars["-shm"] == {
        "present": True,
        "bytes": shm.stat().st_size,
        "sha256": _sha256(shm),
    }
    assert {path: _sha256(path) for path in (wal, shm)} == before


def test_export_rejects_nonzero_wal_and_shm_without_zero_wal(tmp_path: Path) -> None:
    live = _failed_job_fixture(tmp_path / "live")
    wal = Path(str(live["database"]) + "-wal")  # type: ignore[arg-type]
    shm = Path(str(live["database"]) + "-shm")  # type: ignore[arg-type]
    wal.write_bytes(b"active")
    shm.write_bytes(b"shm")
    with pytest.raises(CapsuleIntegrityError, match="WAL is not inert"):
        _export(live, tmp_path / "live-capsule")

    shm_only = _failed_job_fixture(tmp_path / "shm-only")
    Path(str(shm_only["database"]) + "-shm").write_bytes(b"shm")  # type: ignore[arg-type]
    with pytest.raises(CapsuleIntegrityError, match="not the inert zero-WAL tuple"):
        _export(shm_only, tmp_path / "shm-only-capsule")


def test_export_rejects_inert_sidecar_change_during_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _failed_job_fixture(tmp_path)
    _wal, shm = _write_inert_database_sidecars(fixture["database"])  # type: ignore[arg-type]

    def race(source: Path, destination: Path) -> None:
        _online_backup(source, destination)
        shm.write_bytes(b"changed-shm" * 4096)

    monkeypatch.setattr("oms_hub.anki.rehearsal.export._online_backup", race)
    with pytest.raises(CapsuleIntegrityError, match="source database changed"):
        _export(fixture, tmp_path / "racy-inert-sidecars-capsule")


def test_export_refuses_nonfailed_job_dirty_git_and_existing_destination(tmp_path: Path) -> None:
    queued = _failed_job_fixture(tmp_path / "queued", failed=False)
    with pytest.raises(CapsuleIntegrityError, match="recorded failed job"):
        _export(queued, tmp_path / "queued-capsule")

    fixture = _failed_job_fixture(tmp_path)
    with pytest.raises(CapsuleIntegrityError, match="identity"):
        _export(fixture, tmp_path / "wrong-tree-capsule", expected_tree="0" * 40)
    capsule = _export(fixture, tmp_path / "capsule")
    with pytest.raises(CapsuleIntegrityError, match="already exists"):
        _export(fixture, capsule)

    dirty = fixture["repository"] / "unexpected.txt"  # type: ignore[operator]
    dirty.write_text("dirty\n", encoding="utf-8")
    with pytest.raises(CapsuleIntegrityError, match="clean"):
        _export(fixture, tmp_path / "dirty-capsule")


def test_export_refuses_indirect_required_tree_and_wrong_generation(tmp_path: Path) -> None:
    fixture = _failed_job_fixture(tmp_path)
    artifact_path = fixture["artifact_path"]  # type: ignore[assignment]
    replacement = artifact_path.with_name("indirect.json")
    replacement.write_bytes(artifact_path.read_bytes())
    try:
        artifact_path.unlink()
        os.symlink(replacement, artifact_path)
    except OSError as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")
    with pytest.raises(CapsuleIntegrityError, match="indirect"):
        _export(fixture, tmp_path / "symlink-capsule")
    artifact_path.unlink()
    replacement.replace(artifact_path)
    artifact_path.write_bytes(b"changed artifact")
    with pytest.raises(CapsuleIntegrityError, match="content hash"):
        _export(fixture, tmp_path / "secret-capsule")


def test_export_uses_only_persisted_real_layout_artifact_paths(tmp_path: Path) -> None:
    fixture = _failed_job_fixture(tmp_path)
    artifact_path = fixture["artifact_path"]  # type: ignore[assignment]
    artifact_relative = fixture["artifact_relative"]  # type: ignore[assignment]
    artifact_relative_path = Path(artifact_relative)
    other_job_id = uuid4()
    other_job_file = (
        fixture["anki"] / "artifacts" / str(other_job_id) / "source_index" / artifact_path.name  # type: ignore[operator]
    )
    other_job_file.parent.mkdir(parents=True)
    other_job_file.write_bytes(artifact_path.read_bytes())
    unbound = artifact_path.parent / "unbound.json"
    unbound.write_text('{"unbound":true}\n', encoding="utf-8")
    orphan = artifact_path.parent / ".orphan" / ("d" * 64 + ".json")
    orphan.parent.mkdir()
    orphan.write_text('{"orphan":true}\n', encoding="utf-8")

    capsule = _export(fixture, tmp_path / "real-layout-capsule")
    assert (capsule / "anki" / "artifacts" / artifact_relative_path).is_file()
    assert not (
        capsule / "anki" / "artifacts" / artifact_relative_path.parent / "unbound.json"
    ).exists()
    assert not (
        capsule
        / "anki"
        / "artifacts"
        / artifact_relative_path.parent
        / ".orphan"
        / orphan.name
    ).exists()
    assert not (capsule / "anki" / "artifacts" / str(other_job_id)).exists()

    artifact_path.unlink()
    with pytest.raises(CapsuleIntegrityError, match="artifact is unavailable"):
        _export(fixture, tmp_path / "cannot-substitute-capsule")


def test_export_rejects_job_artifact_changed_during_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _failed_job_fixture(tmp_path)
    artifact_path = fixture["artifact_path"]  # type: ignore[assignment]
    original_copy = _copy_bound_job_artifact

    def race(bound: object, destination: Path) -> None:
        original_copy(bound, destination)  # type: ignore[arg-type]
        artifact_path.write_bytes(artifact_path.read_bytes() + b"race")

    monkeypatch.setattr("oms_hub.anki.rehearsal.export._copy_bound_job_artifact", race)
    destination = tmp_path / "racy-artifact-capsule"
    with pytest.raises(CapsuleIntegrityError, match="source components changed"):
        _export(fixture, destination)
    assert not destination.exists()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("kind", "lecture_source_index"),
        ("pipeline_contract_version", "retrieval_v4"),
        ("model_config_sha256", "0" * 64),
    ],
)
def test_export_rejects_persisted_artifact_job_provenance_mismatch(
    tmp_path: Path, column: str, value: str
) -> None:
    fixture = _failed_job_fixture(tmp_path)
    connection = sqlite3.connect(fixture["database"])  # type: ignore[arg-type]
    try:
        connection.execute(f"UPDATE anki_stage_artifacts SET {column} = ?", (value,))
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(CapsuleIntegrityError, match="artifact.*provenance|source-index"):
        _export(fixture, tmp_path / f"bad-{column}-capsule")


def test_export_rejects_runtime_invalid_artifact_document(tmp_path: Path) -> None:
    fixture = _failed_job_fixture(tmp_path)
    artifact_path = fixture["artifact_path"]  # type: ignore[assignment]
    document = json.loads(artifact_path.read_text(encoding="utf-8"))
    document["recovery_product"]["payload"] = {"different": True}
    encoded = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    replacement = artifact_path.parent / f"{digest}.json"
    replacement.write_bytes(encoded)
    artifact_path.unlink()
    connection = sqlite3.connect(fixture["database"])  # type: ignore[arg-type]
    try:
        connection.execute(
            """UPDATE anki_stage_artifacts
               SET artifact_id = ?, relative_path = ?, content_sha256 = ?""",
            (
                f"source_index:{digest}",
                f"{fixture['job'].id}/source_index/{digest}.json",  # type: ignore[union-attr]
                digest,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(CapsuleIntegrityError, match="runtime provenance"):
        _export(fixture, tmp_path / "invalid-document-capsule")


def test_export_rejects_float_artifact_version_with_matching_hash_and_path(tmp_path: Path) -> None:
    fixture = _failed_job_fixture(tmp_path)
    artifact_path = fixture["artifact_path"]  # type: ignore[assignment]
    document = json.loads(artifact_path.read_text(encoding="utf-8"))
    document["artifact_version"] = 3.0
    encoded = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    replacement = artifact_path.parent / f"{digest}.json"
    replacement.write_bytes(encoded)
    artifact_path.unlink()
    connection = sqlite3.connect(fixture["database"])  # type: ignore[arg-type]
    try:
        connection.execute(
            """UPDATE anki_stage_artifacts
               SET artifact_id = ?, relative_path = ?, content_sha256 = ?""",
            (
                f"source_index:{digest}",
                f"{fixture['job'].id}/source_index/{digest}.json",  # type: ignore[union-attr]
                digest,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(CapsuleIntegrityError, match="runtime provenance"):
        _export(fixture, tmp_path / "float-version-capsule")


def test_export_rejects_database_changed_after_immutable_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _failed_job_fixture(tmp_path)
    original_export = _export_job_scoped_database

    def mutate_after_backup(source: Path, destination: Path, job_id: object) -> None:
        original_export(source, destination, job_id)  # type: ignore[arg-type]
        connection = sqlite3.connect(fixture["database"])  # type: ignore[arg-type]
        try:
            connection.execute("UPDATE anki_curation_jobs SET error = 'changed after backup'")
            connection.commit()
        finally:
            connection.close()

    monkeypatch.setattr(
        "oms_hub.anki.rehearsal.export._export_job_scoped_database", mutate_after_backup
    )
    destination = tmp_path / "post-backup-change-capsule"
    with pytest.raises(CapsuleIntegrityError, match="changed after immutable backup"):
        _export(fixture, destination)
    assert not destination.exists()
    assert not (destination / "source-snapshot.json").exists()


def test_export_rejects_database_changed_after_manifest_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _failed_job_fixture(tmp_path)
    original_verify = verify_capsule
    mutated = False

    def mutate_after_manifest(root: Path):
        nonlocal mutated
        result = original_verify(root)
        if not mutated:
            mutated = True
            connection = sqlite3.connect(fixture["database"])  # type: ignore[arg-type]
            try:
                connection.execute("UPDATE anki_curation_jobs SET error = 'late change'")
                connection.commit()
            finally:
                connection.close()
        return result

    monkeypatch.setattr("oms_hub.anki.rehearsal.export.verify_capsule", mutate_after_manifest)
    destination = tmp_path / "late-change-capsule"
    with pytest.raises(CapsuleIntegrityError, match="changed after immutable backup"):
        _export(fixture, destination)
    assert mutated
    assert not destination.exists()


def test_export_refuses_mismatched_snapshot_counts(tmp_path: Path) -> None:
    fixture = _failed_job_fixture(tmp_path)
    with pytest.raises(CapsuleIntegrityError, match="companion note count"):
        _export(fixture, tmp_path / "count-capsule", expected_companion_count=2)


def test_export_refuses_changed_semantic_generation(tmp_path: Path) -> None:
    fixture = _failed_job_fixture(tmp_path)
    SemanticSnapshotStore(fixture["anki"] / "semantic").replace(  # type: ignore[operator]
        [
            DocumentRecord(
                note_id=101,
                text="changed semantic snapshot",
                content_hash=hashlib.sha256(b"changed semantic snapshot").hexdigest(),
            )
        ],
        np.asarray([[1.0, 0.0]], dtype=np.float32),
        model="capsule-fixture",
    )
    with pytest.raises(CapsuleIntegrityError, match="semantic generation"):
        _export(fixture, tmp_path / "semantic-generation-capsule")


def test_export_refuses_live_wal_source_before_creating_a_capsule(tmp_path: Path) -> None:
    fixture = _failed_job_fixture(tmp_path)
    wal = Path(str(fixture["database"]) + "-wal")
    wal.write_bytes(b"active")
    with pytest.raises(CapsuleIntegrityError, match="WAL is not inert"):
        _export(fixture, tmp_path / "live-wal-capsule")


def test_registered_roots_resolve_windows_paths_by_longest_casefolded_root() -> None:
    name, relative = _registered_root_relative(
        Path(r"C:\\A0\\Study\\Lecture.txt"),
        {"a0": Path(r"c:\\a0"), "study": Path(r"C:\\A0\\Study")},
    )
    assert (name, relative.as_posix()) == ("study", "Lecture.txt")
    with pytest.raises(CapsuleIntegrityError, match="outside"):
        _registered_root_relative(Path(r"D:\\A0\\Lecture.txt"), {"a0": Path(r"C:\\A0")})


def test_powershell_wrapper_keeps_frozen_checkout_separate_and_has_refusal_contract() -> None:
    wrapper = Path(__file__).parents[2] / "scripts" / "export-a0-rehearsal-capsule.ps1"
    source = wrapper.read_text(encoding="utf-8")
    assert "Set-StrictMode -Version Latest" in source
    assert "$FrozenRepository" in source and "$ToolRepository" in source
    assert "'--repository', $FrozenRepository" in source
    assert "Push-Location $ResolvedToolRepository" in source
    assert "Refusing to overwrite prior capsule output" in source
    assert "status --porcelain" in source
    assert "Get-CimInstance Win32_Process" in source
    assert "Get-FileHash -Algorithm SHA256" in source
    assert "credentials_exported = $false" in source
    assert "collection_exported = $false" in source
    assert "Assert-ResolvedDescendant" in source
    assert (
        "TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)"
        in source
    )
    assert (
        "$EncodedPythonBootstrap = 'import base64,sys;exec(base64.b64decode(sys.argv.pop(1)))'"
        in source
    )
    assert "[Text.Encoding]::UTF8.GetBytes($Code)" in source
    assert "& $Python -I -c $EncodedPythonBootstrap $EncodedCode @Arguments" in source
    assert "Invoke-EncodedIsolatedPython $ImportProbe @($ResolvedToolSource)" in source
    assert "Invoke-EncodedIsolatedPython $RunVerifiedExporter @(" in source
    assert (
        "Invoke-EncodedIsolatedPython $ZipCode "
        "@($ResolvedToolSource, $StagingCapsule, $StagingArchive)" in source
    )
    assert "$Python -I -c $ImportProbe" not in source
    assert "$Python -I -c $RunVerifiedExporter" not in source
    assert "$Python -I -c $ZipCode" not in source
    assert "runpy.run_module" in source
    assert "verified_python_package_origin" in source
    assert "verified_python_export_origin" in source
    assert "-m oms_hub.anki.rehearsal.export" not in source
    assert "$ToolGit = Assert-CleanGitIdentity" in source
    assert source.index("$ToolGit = Assert-CleanGitIdentity") < source.index("$ImportProbe =")
    assert "$Prefix = $ResolvedRoot.TrimEnd" in source
    assert ".StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase)" in source
    assert "Get-DatabaseSidecarSnapshot" in source
    assert "Assert-InertDatabaseSidecars" in source
    assert "inert_zero_wal_with_shm" in source
    assert "database_sidecars = $InitialDatabaseSidecars" in source
    assert source.index("The bounded A0 source process set is not quiescent") < source.index(
        "$InitialDatabaseSidecars = Get-DatabaseSidecarSnapshot"
    )
    assert "checkpoint/remove" not in source


def test_base64_python_bootstrap_preserves_quotes_and_arguments() -> None:
    code = 'import json,sys; print(json.dumps({"argument":sys.argv[1],"quoted":"yes"}))'
    encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
    completed = subprocess.run(
        [
            str(Path(sys._base_executable).resolve()),
            "-I",
            "-c",
            "import base64,sys;exec(base64.b64decode(sys.argv.pop(1)))",
            encoded,
            "argument with spaces",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {"argument": "argument with spaces", "quoted": "yes"}


def test_powershell_wrapper_stages_before_final_sidecar_proof_and_cleans_its_outputs() -> None:
    """Keep the native-only publication transaction auditable without PowerShell."""
    wrapper = Path(__file__).parents[2] / "scripts" / "export-a0-rehearsal-capsule.ps1"
    source = wrapper.read_text(encoding="utf-8")
    assert "$StagingToken = [Guid]::NewGuid().ToString('N')" in source
    assert "'--destination', $StagingCapsule" in source
    assert "[IO.Directory]::Move($StagingCapsule, $Capsule)" in source
    assert "[IO.File]::Move($StagingArchive, $Archive)" in source
    assert "[IO.File]::Move($StagingSummary, $Summary)" in source
    assert source.index("'--destination', $StagingCapsule") < source.index(
        "$FinalDatabaseSidecars = Get-DatabaseSidecarSnapshot"
    ) < source.index("[IO.Directory]::Move($StagingCapsule, $Capsule)")
    assert "function Remove-StagingExportPath" in source
    assert "function Remove-ReadonlyExportPath" in source
    assert "$PrimaryError.Exception.Data['capsule_cleanup_failure']" in source
    assert "Refusing to overwrite prior capsule output" in source


def test_isolated_python_bootstrap_imports_only_the_explicit_tool_source(tmp_path: Path) -> None:
    """Model the direct base-runtime bootstrap with only verified dependency paths."""
    repository = Path(__file__).parents[2].resolve()
    tool_source = (repository / "src").resolve()
    sibling = tmp_path / f"{tool_source.name}-sibling"
    sibling_package = sibling / "oms_hub"
    sibling_package.mkdir(parents=True)
    (sibling_package / "__init__.py").write_text("ambient = True\n", encoding="utf-8")
    numpy_origin = Path(np.__file__).resolve()
    dependency_root = numpy_origin.parent.parent
    dependency_paths = [str(dependency_root)]
    assert dependency_paths
    assert dependency_root.name in {"site-packages", "dist-packages"}
    assert (dependency_root / "numpy").is_dir()
    probe = (
        "import json,pathlib,sys; source=pathlib.Path(sys.argv[1]).resolve(); "
        "dependencies=json.loads(sys.argv[2]); "
        "assert all(pathlib.Path(path).is_absolute() and pathlib.Path(path).is_dir() "
        "for path in dependencies); "
        "sys.path[:0]=[str(source),*dependencies]; import oms_hub; "
        "from oms_hub.anki.rehearsal import export; "
        "print(json.dumps({'source':str(source),'package':str(pathlib.Path(oms_hub.__file__).resolve()),"
        "'export':str(pathlib.Path(export.__file__).resolve()),'path':sys.path},sort_keys=True))"
    )
    completed = subprocess.run(
        [
            str(Path(sys._base_executable).resolve()),
            "-I",
            "-c",
            probe,
            str(tool_source),
            json.dumps(dependency_paths),
        ],
        cwd=sibling,
        check=True,
        capture_output=True,
        text=True,
        env=os.environ | {"PYTHONPATH": str(sibling)},
    )
    origins = json.loads(completed.stdout)
    source_root = Path(origins["source"])
    package_origin = Path(origins["package"])
    export_origin = Path(origins["export"])
    assert package_origin.is_relative_to(source_root)
    assert export_origin.is_relative_to(source_root)
    assert not package_origin.is_relative_to(sibling)
    assert not export_origin.is_relative_to(sibling)
    assert str(sibling) not in origins["path"]
