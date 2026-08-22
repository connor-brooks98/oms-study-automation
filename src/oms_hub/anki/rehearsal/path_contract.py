from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import PureWindowsPath

from oms_hub.anki.rehearsal.capsule import CapsuleIntegrityError
from oms_hub.migrations import LATEST_SCHEMA_VERSION

PATH_REGISTRY_VERSION = 1

_WINDOWS_ABSOLUTE_TOKEN = re.compile(
    r"(?i)(?:[a-z]:[\\/][^\"'\r\n]*|[\\/]{2}[^\\/\"'\r\n]+[\\/]"
    r"[^\\/\"'\r\n]+(?:[\\/][^\"'\r\n]*)?)"
)

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
# persisted path field, so its path registry is deliberately equal to v26.
_PATH_COLUMNS[27] = _PATH_COLUMNS[26]
# Schemas v28-v29 add policy and cost fields only; neither persists paths.
_PATH_COLUMNS[28] = _PATH_COLUMNS[27]
_PATH_COLUMNS[29] = _PATH_COLUMNS[28]


@dataclass(frozen=True, slots=True)
class RegisteredPath:
    table: str
    rowid: int
    column: str
    value: str


def registry_for_schema(schema: int) -> dict[str, tuple[str, ...]]:
    """Return the registered persisted paths for one supported schema."""

    _assert_path_registry_synchronized()
    registry = _PATH_COLUMNS.get(schema) if type(schema) is int else None
    if registry is None:
        raise CapsuleIntegrityError(f"unsupported path registry for database schema {schema}")
    return registry


def registered_path_columns(
    connection: sqlite3.Connection, schema: int
) -> frozenset[tuple[str, str]]:
    """Return registered table/column pairs present in this database.

    An absent table is acceptable for a deliberately scoped/minimal database.
    A present table missing one of its schema-versioned registered columns is a
    schema/registry drift and must not be silently ignored.
    """

    registry = registry_for_schema(schema)
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    registered: set[tuple[str, str]] = set()
    for table, columns in registry.items():
        if table not in tables:
            continue
        actual_columns = {
            str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        missing = sorted(set(columns) - actual_columns)
        if missing:
            raise CapsuleIntegrityError(
                f"path registry/schema drift: {table} is missing registered columns "
                f"{', '.join(missing)}"
            )
        registered.update((table, column) for column in columns)
    return frozenset(registered)


def enumerate_registered_paths(
    connection: sqlite3.Connection, schema: int
) -> tuple[RegisteredPath, ...]:
    """Enumerate every non-null registered path in deterministic order.

    Values are deliberately not normalized here: the immutable capsule retains
    the precise persisted value and the disposable materializer owns rewriting.
    """

    paths: list[RegisteredPath] = []
    for table, column in sorted(registered_path_columns(connection, schema)):
        for rowid, raw in connection.execute(
            f'SELECT rowid, "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
        ):
            if not isinstance(raw, str):
                raise CapsuleIntegrityError(
                    f"registered path has a non-string value: {table}.{column}"
                )
            paths.append(RegisteredPath(table, int(rowid), column, raw))
    return tuple(paths)


def is_windows_absolute_path(value: str) -> bool:
    """Recognize supported drive-qualified and UNC Windows absolute paths.

    ``PureWindowsPath`` supplies the platform-independent absolute-path
    semantics, while the prefix check rejects drive-relative values such as
    ``C:relative.txt``.
    """

    if not isinstance(value, str):
        return False
    candidate = PureWindowsPath(value)
    return candidate.is_absolute() and bool(
        re.match(r"(?i)^(?:[a-z]:[\\/]|[\\/]{2}[^\\/]+[\\/][^\\/]+)", value)
    )


def contains_windows_absolute_path(value: str) -> bool:
    """Return whether a value contains one of the supported absolute forms."""

    return any(
        is_windows_absolute_path(match.group(0))
        for match in _WINDOWS_ABSOLUTE_TOKEN.finditer(value)
    )


def _assert_path_registry_synchronized() -> None:
    """Fail closed when a supported database schema lacks path ownership."""

    expected = set(range(25, LATEST_SCHEMA_VERSION + 1))
    if set(_PATH_COLUMNS) != expected or LATEST_SCHEMA_VERSION not in _PATH_COLUMNS:
        raise CapsuleIntegrityError("path registry is not synchronized with supported schemas")
