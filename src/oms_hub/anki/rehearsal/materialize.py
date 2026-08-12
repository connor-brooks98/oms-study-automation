from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from oms_hub.anki.rehearsal.capsule import (
    CapsuleIntegrityError,
    verify_capsule,
    verify_capsule_contents,
)
from oms_hub.migrations import LATEST_SCHEMA_VERSION

PATH_REGISTRY_VERSION = 1
_WINDOWS_ABSOLUTE = re.compile(r"(?i)(?:^|[\"'])([a-z]:\\[^\"'\r\n]*)")

# This registry is intentionally explicit. New schema path fields must be added
# here and covered by a migration/materialization regression before use.
_PATH_COLUMNS: dict[int, dict[str, tuple[str, ...]]] = {
    25: {
        "upload_items": ("staged_path",),
        "study_revisions": (
            "immutable_source_path",
            "immutable_derived_path",
            "canonical_source_path",
            "canonical_derived_path",
            "icloud_path",
        ),
        "study_prompt_settings": ("path",),
        "generation_jobs": ("prompt_path",),
        "outline_outputs": ("path", "immutable_path"),
        "existing_artifact_imports": (
            "canonical_transcript_path",
            "canonical_outline_path",
            "immutable_transcript_path",
            "immutable_outline_path",
            "previous_immutable_pdf_path",
            "imported_immutable_pdf_path",
        ),
        "studio_sources": ("payload_path",),
        "studio_quiz_image_requirements": ("asset_path",),
        "published_quiz_media": ("path",),
    }
}
_PATH_COLUMNS[26] = _PATH_COLUMNS[25]
# Schema v27 adds provider-attempt subcall identity only; it introduces no
# persisted path field, so its materialization registry is deliberately equal
# to v26.  Future schemas must be added explicitly here.
_PATH_COLUMNS[27] = _PATH_COLUMNS[26]


def _assert_path_registry_synchronized() -> None:
    """Fail closed when a supported database schema lacks path ownership."""

    expected = set(range(25, LATEST_SCHEMA_VERSION + 1))
    if set(_PATH_COLUMNS) != expected or LATEST_SCHEMA_VERSION not in _PATH_COLUMNS:
        raise CapsuleIntegrityError("path registry is not synchronized with supported schemas")


@dataclass(frozen=True, slots=True)
class PathMaterializationAudit:
    table: str
    rowid: int
    column: str
    old_path_sha256: str
    logical_root: str
    new_path: str


@dataclass(frozen=True, slots=True)
class MaterializedCapsule:
    root: Path
    database_path: Path
    path_audit: tuple[PathMaterializationAudit, ...]


def materialize_capsule(capsule_root: Path, overlay_root: Path) -> MaterializedCapsule:
    manifest = verify_capsule(capsule_root)
    if overlay_root.exists() or overlay_root.is_symlink():
        raise CapsuleIntegrityError("overlay already exists")
    overlay_created = False
    try:
        overlay_root.mkdir(parents=True)
        overlay_created = True
        shutil.copytree(
            capsule_root,
            overlay_root,
            copy_function=shutil.copy2,
            dirs_exist_ok=True,
        )
        verify_capsule_contents(overlay_root, manifest)
        _make_overlay_writable(overlay_root)

        database = overlay_root / "hub" / "hub.db"
        if not database.is_file():
            raise CapsuleIntegrityError("capsule database is unavailable")
        audit = _materialize_paths(
            database,
            schema=manifest.identity.database_schema,
            source_roots=manifest.source_roots,
            logical_roots={
                name: overlay_root / PureWindowsPath(value).as_posix()
                for name, value in manifest.logical_roots.items()
            },
        )
        audit_path = overlay_root / "rehearsal" / "path-materialization.json"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps(
                [
                    {
                        "table": row.table,
                        "rowid": row.rowid,
                        "column": row.column,
                        "old_path_sha256": row.old_path_sha256,
                        "logical_root": row.logical_root,
                        "new_path": row.new_path,
                    }
                    for row in audit
                ],
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        return MaterializedCapsule(overlay_root, database, audit)
    except BaseException:
        if overlay_created:
            _remove_created_overlay(overlay_root)
        raise


def _make_overlay_writable(root: Path) -> None:
    """Restore write access only after the copied capsule is verified."""

    for path in [root, *sorted(root.rglob("*"))]:
        if path.is_symlink():
            raise CapsuleIntegrityError("copied capsule contains a symbolic link")
        mode = stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR
        if path.is_dir():
            mode |= stat.S_IRUSR | stat.S_IXUSR
        os.chmod(path, mode)


def _remove_created_overlay(root: Path) -> None:
    if root.is_symlink():
        root.unlink()
    elif root.exists():
        shutil.rmtree(root)


def _materialize_paths(
    database: Path,
    *,
    schema: int,
    source_roots: dict[str, str],
    logical_roots: dict[str, Path],
) -> tuple[PathMaterializationAudit, ...]:
    _assert_path_registry_synchronized()
    registry = _PATH_COLUMNS.get(schema)
    if registry is None:
        raise CapsuleIntegrityError(f"unsupported path registry for database schema {schema}")
    audits: list[PathMaterializationAudit] = []
    with closing(sqlite3.connect(database)) as connection, connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        registered = {
            (table, column)
            for table, columns in registry.items()
            if table in tables
            for column in columns
        }
        for table, column in sorted(registered):
            actual_columns = {
                str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
            }
            if column not in actual_columns:
                continue
            rows = list(
                connection.execute(
                    f'SELECT rowid, "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
                )
            )
            for rowid, raw in rows:
                if not isinstance(raw, str) or not _is_windows_absolute(raw):
                    continue
                logical_name, relative = _resolve_logical_path(raw, source_roots)
                target = logical_roots[logical_name].joinpath(*relative.parts)
                if not target.is_file() and not target.is_dir():
                    raise CapsuleIntegrityError(
                        f"materialized path target is unavailable: {table}.{column}"
                    )
                connection.execute(
                    f'UPDATE "{table}" SET "{column}" = ? WHERE rowid = ?',
                    (str(target), int(rowid)),
                )
                audits.append(
                    PathMaterializationAudit(
                        table=table,
                        rowid=int(rowid),
                        column=column,
                        old_path_sha256=hashlib.sha256(raw.encode()).hexdigest(),
                        logical_root=logical_name,
                        new_path=str(target),
                    )
                )
        _reject_unregistered_windows_paths(connection, registered, logical_roots)
        foreign_key_rows = list(connection.execute("PRAGMA foreign_key_check"))
        if foreign_key_rows:
            raise CapsuleIntegrityError("materialized database foreign key check failed")
        integrity_rows = list(connection.execute("PRAGMA integrity_check"))
        if integrity_rows != [("ok",)]:
            raise CapsuleIntegrityError("materialized database integrity check failed")
    return tuple(audits)


def _resolve_logical_path(
    value: str,
    source_roots: dict[str, str],
) -> tuple[str, PureWindowsPath]:
    candidate = PureWindowsPath(value)
    matches: list[tuple[int, str, PureWindowsPath]] = []
    folded = str(candidate).casefold()
    for name, raw_root in source_roots.items():
        root = PureWindowsPath(raw_root)
        root_text = str(root).rstrip("\\")
        prefix = root_text.casefold()
        if folded == prefix:
            matches.append((len(prefix), name, PureWindowsPath()))
        elif folded.startswith(prefix + "\\"):
            relative = PureWindowsPath(str(candidate)[len(root_text) + 1 :])
            matches.append((len(prefix), name, relative))
    if not matches:
        raise CapsuleIntegrityError("persisted path is outside registered source roots")
    _, name, relative = max(matches)
    if ".." in relative.parts:
        raise CapsuleIntegrityError("persisted path escapes its logical root")
    return name, relative


def _reject_unregistered_windows_paths(
    connection: sqlite3.Connection,
    registered: set[tuple[str, str]],
    logical_roots: dict[str, Path],
) -> None:
    tables = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for table in tables:
        columns = [
            str(row[1])
            for row in connection.execute(f'PRAGMA table_info("{table}")')
            if ("CHAR" in str(row[2]).upper() or "TEXT" in str(row[2]).upper())
            and _path_like_column(str(row[1]))
        ]
        for column in columns:
            for (value,) in connection.execute(
                f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
            ):
                if isinstance(value, str) and _WINDOWS_ABSOLUTE.search(value):
                    if (table, column) in registered:
                        if _is_materialized_logical_path(value, logical_roots):
                            continue
                        raise CapsuleIntegrityError(
                            f"registered Windows path was not materialized: {table}.{column}"
                        )
                    raise CapsuleIntegrityError(
                        f"unregistered Windows path remains in {table}.{column}"
                    )


def _is_materialized_logical_path(value: str, logical_roots: dict[str, Path]) -> bool:
    """Accept only a rewritten path beneath this overlay's known logical roots.

    A source database records Windows paths, but on Windows the materialized
    overlay is also a Windows path.  Treating every remaining drive-qualified
    value as source residue incorrectly rejects the safe rewritten target.
    ``PureWindowsPath`` makes this containment check deterministic in the
    portable test suite too.
    """
    candidate = PureWindowsPath(value)
    return any(
        candidate == (root_path := PureWindowsPath(str(root))) or root_path in candidate.parents
        for root in logical_roots.values()
    )


def _path_like_column(column: str) -> bool:
    normalized = column.casefold()
    return normalized == "path" or normalized.endswith(
        ("_path", "_root", "_directory", "_dir")
    )


def _is_windows_absolute(value: str) -> bool:
    return bool(re.match(r"(?i)^[a-z]:\\", value))
