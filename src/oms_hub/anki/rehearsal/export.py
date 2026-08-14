from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import UUID

from oms_hub.anki.domain import CurationJob, CurationStage, CurationState, StageArtifact
from oms_hub.anki.index import AnkiIndex
from oms_hub.anki.rehearsal.capsule import (
    CapsuleIdentity,
    CapsuleIntegrityError,
    _reject_sensitive_path,
    build_capsule_manifest,
    make_capsule_read_only,
    verify_capsule,
    write_capsule_manifest,
)
from oms_hub.anki.rehearsal.path_contract import enumerate_registered_paths
from oms_hub.anki.rehearsal.regressions import historical_regression_catalog
from oms_hub.anki.repository import AnkiCurationRepository
from oms_hub.anki.semantic.store import SemanticSnapshotStore
from oms_hub.db import Database
from oms_hub.models import SchemaVersionModel

_ROOT_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# The rehearsal database is deliberately a *logical* export.  These are the
# only rows that can be useful to recreate a fresh job from the recorded
# failure; provider history, accounts, NotebookLM mappings, and published
# quizzes are intentionally not part of the closure.
_SNAPSHOT_TABLES = frozenset(
    {
        "schema_version",
        "lectures",
        "upload_batches",
        "upload_items",
        "study_revisions",
        "generation_jobs",
        "outline_outputs",
        "existing_artifact_imports",
        "anki_curation_jobs",
        "anki_stage_artifacts",
    }
)
_REHEARSAL_GENERATION_COLUMNS = frozenset(
    {
        "id",
        "lecture_id",
        "kind",
        "state",
        "stage",
        "attempts",
        "next_attempt_at",
        "pdf_revision_id",
        "transcript_revision_id",
        "created_at",
        "updated_at",
    }
)


@dataclass(frozen=True, slots=True)
class _BoundJobArtifact:
    artifact: StageArtifact
    source: Path
    relative_path: PurePosixPath


def export_capsule(
    *,
    repository_root: Path,
    database_path: Path,
    anki_root: Path,
    job_id: UUID,
    destination: Path,
    source_roots: dict[str, Path],
    expected_commit: str,
    expected_tree: str,
    expected_companion_count: int,
    expected_semantic_count: int,
) -> Path:
    """Create one immutable capsule without modifying any source location."""
    _verify_git_identity(repository_root, expected_commit, expected_tree)
    _validate_export_inputs(database_path, anki_root, destination, source_roots)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        capsule_database = temporary / "hub" / "hub.db"
        capsule_database.parent.mkdir(parents=True)
        logical_snapshot = temporary / ".source-logical-snapshot.db"
        backup_before = _database_snapshot(database_path)
        _require_quiescent_database_snapshot(backup_before)
        _online_backup(database_path, logical_snapshot)
        backup_after = _database_snapshot(database_path)
        _require_quiescent_database_snapshot(backup_after)
        if backup_before != backup_after:
            raise CapsuleIntegrityError(
                "source database changed during immutable backup; "
                "a stopped/quiescent source is required"
            )
        _require_database_snapshot_matches(database_path, backup_after)
        _export_job_scoped_database(logical_snapshot, capsule_database, job_id)
        logical_snapshot.unlink()

        with Database(f"sqlite:///{capsule_database}") as database:
            repository = AnkiCurationRepository(database)
            job = repository.require_job(job_id)
            if job.state is not CurationState.FAILED:
                raise CapsuleIntegrityError("capsule export requires the recorded failed job")
            with database.session() as session:
                schema_row = session.get(SchemaVersionModel, 1)
                if schema_row is None:
                    raise CapsuleIntegrityError("source database has no schema identity")
                database_schema = schema_row.version
            job_artifacts = _bound_job_artifacts(
                anki_root,
                job,
                repository.list_stage_artifacts(job_id),
            )

        referenced_files = _registered_database_source_files(capsule_database, database_schema)

        _require_database_snapshot_matches(database_path, backup_after)
        before = _source_component_snapshot(database_path, anki_root, job_artifacts)
        _require_source_snapshot_database_matches(before, database_path, backup_after)

        for source in referenced_files:
            _copy_logical_file(source, temporary, source_roots)

        for relative in (Path("companion"), Path("semantic")):
            source = anki_root / relative
            if not source.is_dir():
                raise CapsuleIntegrityError(f"required Anki capsule root is missing: {relative}")
            _copy_verified_tree(source, temporary / "anki" / relative)

        for bound in job_artifacts:
            _copy_bound_job_artifact(bound, temporary / "anki" / "artifacts")

        after = _source_component_snapshot(database_path, anki_root, job_artifacts)
        _require_database_snapshot_matches(database_path, backup_after)
        _require_source_snapshot_database_matches(after, database_path, backup_after)
        if before != after:
            raise CapsuleIntegrityError(
                "source components changed during export; a stopped/quiescent source is required"
            )
        (temporary / "source-snapshot.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "consistency": "component-stable; no cross-resource atomicity claimed",
                    "database_export": "job-scoped-allowlist",
                    "database_sidecar_state": _classify_database_sidecars(
                        before["database_sidecars"]
                    ),
                    "credentials_exported": False,
                    "components": before,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

        prompt_assets = repository_root / "src" / "oms_hub" / "anki" / "prompt_assets"
        if not prompt_assets.is_dir():
            raise CapsuleIntegrityError("frozen prompt assets are unavailable")
        repository_logical = _logical_root_name(repository_root, source_roots)
        prompt_relative = prompt_assets.relative_to(repository_root)
        _copy_verified_tree(
            prompt_assets,
            temporary / "sources" / repository_logical / prompt_relative,
        )

        companion = AnkiIndex(temporary / "anki" / "companion")
        companion_generation = companion.snapshot_id()
        companion_count = len(companion.list_notes())
        semantic = SemanticSnapshotStore(temporary / "anki" / "semantic").load()
        semantic_generation = str(semantic.manifest.generation)
        semantic_count = len(semantic.manifest.note_ids)
        # ``load`` validates vectors through a read-only NumPy memmap.  Closing
        # it before renaming the containing directory is required on Windows,
        # where an open mapped file prevents the capsule directory move.
        _close_read_only_memmap(semantic.matrix)
        if companion_generation is None:
            raise CapsuleIntegrityError("companion snapshot identity is unavailable")
        if companion_generation != job.companion_generation:
            raise CapsuleIntegrityError("companion generation does not match the frozen job")
        if semantic_generation != job.semantic_generation:
            raise CapsuleIntegrityError("semantic generation does not match the frozen job")
        if companion_count != expected_companion_count:
            raise CapsuleIntegrityError("companion note count does not match the export gate")
        if semantic_count != expected_semantic_count:
            raise CapsuleIntegrityError("semantic note count does not match the export gate")

        replay = temporary / "replay"
        replay.mkdir()
        (replay / "structured.json").write_text("{}\n", encoding="utf-8")
        (temporary / "regressions.json").write_text(
            json.dumps(historical_regression_catalog(), sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        logical_roots = {name: f"sources/{name}" for name in sorted(source_roots)}
        manifest = build_capsule_manifest(
            temporary,
            identity=CapsuleIdentity(
                commit_sha=expected_commit,
                tree_sha=expected_tree,
                database_schema=database_schema,
                companion_generation=companion_generation,
                semantic_generation=semantic_generation,
                companion_note_count=companion_count,
                semantic_note_count=semantic_count,
            ),
            logical_roots=logical_roots,
            source_roots={name: str(path) for name, path in sorted(source_roots.items())},
        )
        write_capsule_manifest(temporary, manifest)
        verify_capsule(temporary)
        _require_database_snapshot_matches(database_path, backup_after)
        _require_source_snapshot_database_matches(before, database_path, backup_after)
        temporary.rename(destination)
        make_capsule_read_only(destination)
        verify_capsule(destination)
        return destination
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _registered_database_source_files(database: Path, schema: int) -> tuple[Path, ...]:
    """Return the exact de-duplicated registered file closure of the export."""

    with closing(sqlite3.connect(database)) as connection:
        paths = {
            Path(registered_path.value)
            for registered_path in enumerate_registered_paths(connection, schema)
        }
    if not paths:
        raise CapsuleIntegrityError("job-scoped database has no registered source artifacts")
    return tuple(sorted(paths, key=str))


def _copy_logical_file(source: Path, capsule: Path, roots: dict[str, Path]) -> None:
    if not source.is_file() or source.is_symlink():
        raise CapsuleIntegrityError(f"source artifact is unavailable or indirect: {source}")
    name, relative = _registered_root_relative(source, roots)
    _require_direct_logical_source_file(source, roots[name])
    _copy_verified_file(source, capsule / "sources" / name / relative)


def _require_direct_logical_source_file(source: Path, root: Path) -> None:
    """Prove a registered lexical path is directly contained by its real root."""

    try:
        relative = source.relative_to(root)
    except ValueError as exc:
        raise CapsuleIntegrityError("source artifact is outside registered roots") from exc
    current = root
    for part in (None, *relative.parts):
        if part is not None:
            current = current / part
        if _is_indirect_path_component(current):
            raise CapsuleIntegrityError(f"source artifact is unavailable or indirect: {source}")
    try:
        resolved_root = root.resolve(strict=True)
        resolved_source = source.resolve(strict=True)
        resolved_source.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CapsuleIntegrityError("source artifact escapes its registered logical root") from exc


def _is_indirect_path_component(path: Path) -> bool:
    """Reject links and Windows junctions/reparse points when exposed by Python."""

    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction()) if callable(is_junction) else False
    except OSError:
        return True


def _bound_job_artifacts(
    anki_root: Path,
    job: CurationJob,
    artifacts: Sequence[StageArtifact],
) -> tuple[_BoundJobArtifact, ...]:
    """Bind the copied artifact set to persisted job metadata, not a layout guess."""
    artifact_root = anki_root / "artifacts"
    if not artifact_root.is_dir() or artifact_root.is_symlink():
        raise CapsuleIntegrityError("persisted job artifact root is unavailable or indirect")
    if not artifacts:
        raise CapsuleIntegrityError("frozen job has no persisted stage artifacts")

    bound: list[_BoundJobArtifact] = []
    seen_paths: set[PurePosixPath] = set()
    for artifact in artifacts:
        relative = PurePosixPath(artifact.relative_path)
        expected_relative = PurePosixPath(
            str(job.id), artifact.stage.value, f"{artifact.content_sha256}.json"
        )
        if (
            artifact.artifact_id != f"{artifact.stage.value}:{artifact.content_sha256}"
            or relative != expected_relative
            or relative.is_absolute()
            or ".." in relative.parts
            or not re.fullmatch(r"[0-9a-f]{64}", artifact.content_sha256)
            or relative in seen_paths
            or artifact.pipeline_contract_version is not job.pipeline_contract_version
            or artifact.model_config_sha256 != job.model_config_sha256
        ):
            raise CapsuleIntegrityError("persisted job artifact provenance is invalid")
        source = artifact_root.joinpath(*relative.parts)
        _require_direct_descendant_file(source, artifact_root)
        if _sha256_file(source) != artifact.content_sha256:
            raise CapsuleIntegrityError("persisted job artifact content hash does not match")
        if not _runtime_artifact_document_matches(source, artifact, job):
            raise CapsuleIntegrityError("persisted job artifact runtime provenance is invalid")
        seen_paths.add(relative)
        bound.append(_BoundJobArtifact(artifact, source, relative))

    if not any(
        item.artifact.stage is CurationStage.SOURCE_INDEX
        and item.artifact.kind == "card_centric_source_index"
        for item in bound
    ):
        raise CapsuleIntegrityError("frozen job has no persisted source-index artifact")
    return tuple(sorted(bound, key=lambda item: item.relative_path.as_posix()))


def _require_direct_descendant_file(path: Path, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise CapsuleIntegrityError("persisted job artifact escapes its root") from exc
    if not relative.parts or not path.is_file():
        raise CapsuleIntegrityError("persisted job artifact is unavailable")
    current = path
    while current != root:
        if current.is_symlink():
            raise CapsuleIntegrityError("persisted job artifact is indirect")
        current = current.parent


def _copy_bound_job_artifact(bound: _BoundJobArtifact, destination: Path) -> None:
    if _sha256_file(bound.source) != bound.artifact.content_sha256:
        raise CapsuleIntegrityError("persisted job artifact content hash does not match")
    target = destination.joinpath(*bound.relative_path.parts)
    _copy_verified_file(bound.source, target)
    if _sha256_file(target) != bound.artifact.content_sha256:
        raise CapsuleIntegrityError("copied job artifact content hash does not match")


def _runtime_artifact_document_matches(
    source: Path, artifact: StageArtifact, job: CurationJob
) -> bool:
    """Apply the v3 runtime provenance contract before exposing an artifact."""
    try:
        encoded = source.read_bytes()
        document = json.loads(encoded)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict) or _canonical_artifact_bytes(document) != encoded:
        return False
    payload = document.get("payload")
    metadata = document.get("metadata")
    recovery_product = document.get("recovery_product")
    return (
        type(document.get("artifact_version")) is int
        and document.get("artifact_version") == 3
        and document.get("job_id") == str(job.id)
        and document.get("stage") == artifact.stage.value
        and document.get("kind") == artifact.kind
        and document.get("input_sha256") == artifact.input_sha256
        and document.get("pipeline_contract_version") == job.pipeline_contract_version.value
        and document.get("model_config_sha256") == job.model_config_sha256
        and payload is not None
        and isinstance(payload, dict)
        and isinstance(metadata, dict)
        and metadata == artifact.metadata
        and isinstance(recovery_product, dict)
        and recovery_product.get("kind") == artifact.kind
        and recovery_product.get("payload") == payload
        and recovery_product.get("metadata") == metadata
    )


def _canonical_artifact_bytes(document: dict[str, object]) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8") + b"\n"


def _registered_root_relative(source: Path, roots: dict[str, Path]) -> tuple[str, Path]:
    """Resolve a persisted source path to its longest registered logical root.

    ``PureWindowsPath`` is intentional: the frozen NUC database records Windows
    paths, while review tests run on macOS and Linux.
    """
    matches: list[tuple[int, str, Path]] = []
    candidate = PureWindowsPath(str(source))
    folded = str(candidate).casefold()
    for name, raw_root in roots.items():
        root = PureWindowsPath(str(raw_root))
        root_text = str(root).rstrip("\\")
        prefix = root_text.casefold()
        if folded.startswith(prefix + "\\"):
            relative = Path(*PureWindowsPath(str(candidate)[len(root_text) + 1 :]).parts)
            matches.append((len(prefix), name, relative))
        elif folded == prefix:
            raise CapsuleIntegrityError("logical source root cannot itself be a file")
    if not matches:
        raise CapsuleIntegrityError(f"source artifact is outside registered roots: {source}")
    _, name, relative = max(matches)
    if ".." in relative.parts:
        raise CapsuleIntegrityError("source artifact escapes its logical root")
    if not relative.parts:
        raise CapsuleIntegrityError("logical source root cannot itself be a file")
    return name, relative


def _logical_root_name(source: Path, roots: dict[str, Path]) -> str:
    matches = [
        (len(str(root)), name)
        for name, root in roots.items()
        if str(PureWindowsPath(source)).casefold()
        == str(PureWindowsPath(root)).rstrip("\\").casefold()
    ]
    if not matches:
        raise CapsuleIntegrityError(f"source root is not registered: {source}")
    return max(matches)[1]


def _copy_verified_tree(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise CapsuleIntegrityError(f"capsule source contains an indirect path: {source}")
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise CapsuleIntegrityError(f"capsule source contains an indirect path: {path}")
        if path.is_file():
            relative = path.relative_to(source)
            _reject_sensitive_path(relative.as_posix())
            _copy_verified_file(path, destination / relative)


def _copy_verified_file(source: Path, destination: Path) -> None:
    before = _sha256_file(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if _sha256_file(destination) != before or _sha256_file(source) != before:
        raise CapsuleIntegrityError(f"source changed while copying: {source}")


def _online_backup(source: Path, destination: Path) -> None:
    """Make a WAL-aware logical SQLite snapshot without modifying the source."""
    # The native wrapper proves its bounded A0 process set is stopped; this
    # exporter independently proves the database/sidecar tuple stays still.
    # ``immutable=1`` ignores the only accepted inert tuple (zero-byte WAL
    # plus stable SHM) and prevents SQLite from creating or modifying sidecars.
    source_uri = f"file:{source.resolve().as_posix()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(source_uri, uri=True)) as reader:
        with closing(sqlite3.connect(destination)) as writer:
            reader.backup(writer)


def _close_read_only_memmap(matrix: object) -> None:
    """Release a NumPy memmap file handle after export-only validation."""
    mapping = getattr(matrix, "_mmap", None)
    if mapping is not None:
        mapping.close()


def _export_job_scoped_database(source: Path, destination: Path, job_id: UUID) -> None:
    """Build a schema-aware, allowlisted relational closure for one failed job.

    The destination begins with this application's schema but receives data only
    through the predicates below.  This prevents an accidental whole-Hub clone
    from carrying account state, provider payloads, Notebook identifiers, or
    unrelated public quiz records into a capsule.
    """
    target = Database(f"sqlite:///{destination}")
    target.migrate()
    target.close()
    with (
        closing(sqlite3.connect(source)) as reader,
        closing(sqlite3.connect(destination)) as writer,
    ):
        reader.row_factory = sqlite3.Row
        writer.execute("PRAGMA foreign_keys = ON")
        # The production schema deliberately records an existing-artifact
        # import in both directions: its revisions/outlines point at the
        # import and the import identifies those same rows.  SQLite checks
        # that valid cycle immediately unless it is explicitly deferred for
        # this one closure transaction.
        writer.execute("BEGIN")
        writer.execute("PRAGMA defer_foreign_keys = ON")
        job = reader.execute(
            "SELECT * FROM anki_curation_jobs WHERE id = ?", (str(job_id),)
        ).fetchone()
        if job is None:
            raise CapsuleIntegrityError("failed job is unavailable in the source database")
        revision_ids = set(
            _json_integer_ids(job["source_revision_ids_json"], "source revision IDs")
        )
        outline_ids = (
            {int(job["summary_outline_id"])} if job["summary_outline_id"] is not None else set()
        )
        import_ids: set[str] = set()
        # Follow every FK edge in the import/revision/outline knot before any
        # insert.  This is a logical provenance closure, not a fabricated
        # substitute for unavailable artifact records.
        while True:
            before = (frozenset(revision_ids), frozenset(outline_ids), frozenset(import_ids))
            if revision_ids:
                for row in _rows(
                    reader,
                    "study_revisions",
                    _in_predicate("id", sorted(revision_ids)),
                    tuple(sorted(revision_ids)),
                ):
                    if row["import_id"] is not None:
                        import_ids.add(str(row["import_id"]))
            if outline_ids:
                for row in _rows(
                    reader,
                    "outline_outputs",
                    _in_predicate("id", sorted(outline_ids)),
                    tuple(sorted(outline_ids)),
                ):
                    if row["slide_revision_id"] is not None:
                        revision_ids.add(int(row["slide_revision_id"]))
                    if row["transcript_revision_id"] is not None:
                        revision_ids.add(int(row["transcript_revision_id"]))
                    if row["import_id"] is not None:
                        import_ids.add(str(row["import_id"]))
            if import_ids:
                for row in _rows(
                    reader,
                    "existing_artifact_imports",
                    _in_predicate("id", sorted(import_ids)),
                    tuple(sorted(import_ids)),
                ):
                    revision_ids.add(int(row["slide_revision_id"]))
                    if row["transcript_revision_id"] is not None:
                        revision_ids.add(int(row["transcript_revision_id"]))
                    if row["outline_id"] is not None:
                        outline_ids.add(int(row["outline_id"]))
            if before == (frozenset(revision_ids), frozenset(outline_ids), frozenset(import_ids)):
                break
        _copy_rows(writer, reader, "schema_version", "id = 1")
        revision_rows = _rows(
            reader,
            "study_revisions",
            _in_predicate("id", sorted(revision_ids)),
            tuple(sorted(revision_ids)),
        )
        upload_ids = [row["upload_item_id"] for row in revision_rows]
        lecture_ids = {int(job["lecture_id"])} | {int(row["lecture_id"]) for row in revision_rows}
        item_rows = _rows(
            reader, "upload_items", _in_predicate("id", upload_ids), tuple(upload_ids)
        )
        batch_ids = [row["batch_id"] for row in item_rows]
        lecture_ids.update(
            int(row["lecture_id"]) for row in item_rows if row["lecture_id"] is not None
        )
        outline_rows = _rows(
            reader,
            "outline_outputs",
            _in_predicate("id", sorted(outline_ids)),
            tuple(sorted(outline_ids)),
        )
        lecture_ids.update(int(row["lecture_id"]) for row in outline_rows)
        _copy_rows(
            writer,
            reader,
            "lectures",
            _in_predicate("id", sorted(lecture_ids)),
            tuple(sorted(lecture_ids)),
        )
        _copy_rows(
            writer, reader, "upload_batches", _in_predicate("id", batch_ids), tuple(batch_ids)
        )
        _copy_rows(
            writer, reader, "upload_items", _in_predicate("id", upload_ids), tuple(upload_ids)
        )
        _copy_rows(
            writer,
            reader,
            "study_revisions",
            _in_predicate("id", sorted(revision_ids)),
            tuple(sorted(revision_ids)),
        )
        if import_ids:
            _copy_rows(
                writer,
                reader,
                "existing_artifact_imports",
                _in_predicate("id", sorted(import_ids)),
                tuple(sorted(import_ids)),
            )
        for outline in outline_rows:
            if outline["job_id"] is not None:
                _copy_rows(
                    writer, reader, "generation_jobs", "id = ?", (outline["job_id"],), scrub=True
                )
        if outline_ids:
            _copy_rows(
                writer,
                reader,
                "outline_outputs",
                _in_predicate("id", sorted(outline_ids)),
                tuple(sorted(outline_ids)),
            )
        _copy_rows(writer, reader, "anki_curation_jobs", "id = ?", (str(job_id),))
        _copy_rows(writer, reader, "anki_stage_artifacts", "job_id = ?", (str(job_id),))
        failures = writer.execute("PRAGMA foreign_key_check").fetchall()
        if failures:
            raise CapsuleIntegrityError("job-scoped database closure has foreign-key failures")
        writer.commit()


def _rows(
    connection: sqlite3.Connection, table: str, predicate: str, parameters: tuple[object, ...]
) -> list[sqlite3.Row]:
    _require_allowlisted_table(table)
    return list(connection.execute(f"SELECT * FROM {table} WHERE {predicate}", parameters))


def _copy_rows(
    writer: sqlite3.Connection,
    reader: sqlite3.Connection,
    table: str,
    predicate: str,
    parameters: tuple[object, ...] = (),
    *,
    scrub: bool = False,
) -> None:
    rows = _rows(reader, table, predicate, parameters)
    if not rows:
        return
    if table == "schema_version":
        writer.execute("DELETE FROM schema_version")
    columns = tuple(rows[0].keys())
    if scrub:
        if table != "generation_jobs":
            raise CapsuleIntegrityError("only generation rows may be scrubbed")
        columns = tuple(column for column in columns if column in _REHEARSAL_GENERATION_COLUMNS)
        if not columns:
            raise CapsuleIntegrityError("generation export has no rehearsal-safe columns")
    values = [tuple(row[column] for column in columns) for row in rows]
    quoted_columns = ", ".join(columns)
    writer.executemany(
        f"INSERT INTO {table} ({quoted_columns}) VALUES ({', '.join('?' for _ in columns)})",
        values,
    )


def _require_allowlisted_table(table: str) -> None:
    if table not in _SNAPSHOT_TABLES:
        raise CapsuleIntegrityError(f"table is outside the job-scoped export allowlist: {table}")


def _in_predicate(column: str, values: Sequence[object]) -> str:
    if not values:
        return "0"
    return f"{column} IN ({', '.join('?' for _ in values)})"


def _json_integer_ids(raw: object, label: str) -> list[int]:
    try:
        values = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise CapsuleIntegrityError(f"frozen job has invalid {label}") from exc
    if (
        not isinstance(values, list)
        or not values
        or any(type(value) is not int for value in values)
    ):
        raise CapsuleIntegrityError(f"frozen job has invalid {label}")
    return sorted(set(values))


def _source_component_snapshot(
    database: Path,
    anki_root: Path,
    job_artifacts: Sequence[_BoundJobArtifact],
) -> dict[str, object]:
    """Identity evidence for independently copied source components.

    It proves each component stayed still during the export.  It intentionally
    does not claim an impossible all-resource transaction.
    """
    paths = [database]
    for relative in (Path("companion"), Path("semantic")):
        paths.extend(sorted(path for path in (anki_root / relative).rglob("*") if path.is_file()))
    snapshot: dict[str, object] = {
        str(path): {"sha256": _sha256_file(path), "bytes": path.stat().st_size}
        for path in paths
        if path.exists()
    }
    snapshot["job_artifacts"] = {
        item.relative_path.as_posix(): {
            "artifact_id": item.artifact.artifact_id,
            "stage": item.artifact.stage.value,
            "kind": item.artifact.kind,
            "sha256": _sha256_file(item.source),
            "bytes": item.source.stat().st_size,
        }
        for item in job_artifacts
    }
    snapshot["database_sidecars"] = _database_sidecars(database)
    return snapshot


def _database_snapshot(database: Path) -> dict[str, object]:
    if not database.is_file():
        raise CapsuleIntegrityError("source database is unavailable")
    return {
        "database": {"sha256": _sha256_file(database), "bytes": database.stat().st_size},
        "sidecars": _database_sidecars(database),
    }


def _require_database_snapshot_matches(database: Path, expected: dict[str, object]) -> None:
    if _database_snapshot(database) != expected:
        raise CapsuleIntegrityError(
            "source database changed after immutable backup; "
            "a stopped/quiescent source is required"
        )


def _require_source_snapshot_database_matches(
    snapshot: dict[str, object], database: Path, expected: dict[str, object]
) -> None:
    if (
        snapshot.get(str(database)) != expected.get("database")
        or snapshot.get("database_sidecars") != expected.get("sidecars")
    ):
        raise CapsuleIntegrityError("source snapshot database attribution is inconsistent")


def _database_sidecars(database: Path) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for suffix in ("-wal", "-shm"):
        sidecar = database.with_name(database.name + suffix)
        result[suffix] = (
            {"present": True, "sha256": _sha256_file(sidecar), "bytes": sidecar.stat().st_size}
            if sidecar.exists()
            else {"present": False}
        )
    return result


def _require_quiescent_database_snapshot(snapshot: dict[str, object]) -> None:
    # This exporter deliberately does not checkpoint or otherwise write the
    # production source. Process quiescence is a native-wrapper trust boundary;
    # here we only admit an absent tuple or a stable inert zero-WAL tuple.
    sidecars = snapshot.get("sidecars")
    if not isinstance(sidecars, dict):
        raise CapsuleIntegrityError("source database snapshot is malformed")
    _classify_database_sidecars(sidecars)


def _classify_database_sidecars(sidecars: object) -> str:
    """Accept only no sidecars or the observed inert zero-WAL/SHM tuple."""
    if not isinstance(sidecars, dict):
        raise CapsuleIntegrityError("source database snapshot is malformed")
    wal = sidecars.get("-wal")
    shm = sidecars.get("-shm")
    if not isinstance(wal, dict) or not isinstance(shm, dict):
        raise CapsuleIntegrityError("source database snapshot is malformed")
    wal_present = wal.get("present") is True
    shm_present = shm.get("present") is True
    if not wal_present and not shm_present:
        return "absent"
    if wal_present and (
        wal.get("bytes") != 0 or wal.get("sha256") != hashlib.sha256(b"").hexdigest()
    ):
        raise CapsuleIntegrityError("source database WAL is not inert")
    if not wal_present or not shm_present:
        raise CapsuleIntegrityError("source database sidecars are not the inert zero-WAL tuple")
    if not isinstance(shm.get("bytes"), int) or shm["bytes"] < 0 or not isinstance(
        shm.get("sha256"), str
    ):
        raise CapsuleIntegrityError("source database SHM sidecar is malformed")
    return "inert_zero_wal_with_shm"


def _verify_git_identity(repository: Path, commit: str, tree: str) -> None:
    observed_commit = _git(repository, "rev-parse", "HEAD")
    observed_tree = _git(repository, "rev-parse", "HEAD^{tree}")
    if observed_commit != commit or observed_tree != tree:
        raise CapsuleIntegrityError("repository identity does not match the export gate")
    if _git(repository, "status", "--porcelain"):
        raise CapsuleIntegrityError("capsule export requires a clean source checkout")


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()


def _validate_export_inputs(
    database: Path,
    anki_root: Path,
    destination: Path,
    source_roots: dict[str, Path],
) -> None:
    if destination.exists():
        raise CapsuleIntegrityError("capsule destination already exists")
    if (
        not database.is_file()
        or database.is_symlink()
        or not anki_root.is_dir()
        or anki_root.is_symlink()
    ):
        raise CapsuleIntegrityError("database or Anki root is unavailable")
    if not source_roots or any(not _ROOT_NAME.fullmatch(name) for name in source_roots):
        raise CapsuleIntegrityError("source-root names are invalid")
    if any(not path.is_dir() or path.is_symlink() for path in source_roots.values()):
        raise CapsuleIntegrityError("a registered source root is unavailable")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _source_root_pairs(values: Iterable[str]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        if not separator or name in roots:
            raise argparse.ArgumentTypeError("source roots must be unique NAME=PATH values")
        roots[name] = Path(raw_path)
    return roots


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a read-only A0 rehearsal capsule")
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--anki-root", type=Path, required=True)
    parser.add_argument("--job-id", type=UUID, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--source-root", action="append", default=[])
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--expected-companion-count", type=int, required=True)
    parser.add_argument("--expected-semantic-count", type=int, required=True)
    arguments = parser.parse_args(argv)
    exported = export_capsule(
        repository_root=arguments.repository,
        database_path=arguments.database,
        anki_root=arguments.anki_root,
        job_id=arguments.job_id,
        destination=arguments.destination,
        source_roots=_source_root_pairs(arguments.source_root),
        expected_commit=arguments.commit,
        expected_tree=arguments.tree,
        expected_companion_count=arguments.expected_companion_count,
        expected_semantic_count=arguments.expected_semantic_count,
    )
    print(exported)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
