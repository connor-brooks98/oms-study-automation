"""Create an integrity-checked SQLite online backup for Windows rollout safety."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import struct
import uuid
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import Any


def backup_database(source: Path, destination: Path) -> str:
    """Copy committed SQLite state into one standalone, verified database file."""
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.partial-{uuid.uuid4().hex}")
    try:
        source_uri = f"{source.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True)) as source_connection:
            with closing(sqlite3.connect(temporary)) as destination_connection:
                source_connection.backup(destination_connection)
                _assert_integrity(destination_connection, temporary)
                journal_mode = destination_connection.execute(
                    "PRAGMA journal_mode=DELETE"
                ).fetchone()
                if journal_mode != ("delete",):
                    raise sqlite3.DatabaseError(
                        "SQLite backup could not be normalized to DELETE journal mode "
                        f"for {temporary}: {journal_mode}"
                    )
                _assert_integrity(destination_connection, temporary)
        _assert_no_sidecars(temporary)
        os.replace(temporary, destination)
        with closing(
            sqlite3.connect(
                f"{destination.resolve().as_uri()}?mode=ro",
                uri=True,
            )
        ) as check_connection:
            _assert_integrity(check_connection, destination)
            journal_mode = check_connection.execute("PRAGMA journal_mode").fetchone()
            if journal_mode != ("delete",):
                raise sqlite3.DatabaseError(
                    "Published SQLite backup is not in DELETE journal mode "
                    f"for {destination}: {journal_mode}"
                )
        _assert_no_sidecars(destination)
        return _sha256(destination)
    finally:
        for path in (temporary, *_sidecar_paths(temporary)):
            path.unlink(missing_ok=True)


def _assert_integrity(connection: sqlite3.Connection, path: Path) -> None:
    result = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    if result != ["ok"]:
        raise sqlite3.DatabaseError(
            f"SQLite backup integrity check failed for {path}: {result}"
        )


def _sidecar_paths(path: Path) -> tuple[Path, Path]:
    return (
        path.with_name(f"{path.name}-wal"),
        path.with_name(f"{path.name}-shm"),
    )


def _assert_no_sidecars(path: Path) -> None:
    present = [sidecar for sidecar in _sidecar_paths(path) if sidecar.exists()]
    if present:
        raise sqlite3.DatabaseError(
            f"SQLite backup retained transient sidecars for {path}: {present}"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def logical_sha256(path: Path) -> str:
    """Hash logical SQLite state without incorporating page-layout churn."""
    if not path.is_file():
        raise FileNotFoundError(path)
    with closing(
        sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
        )
    ) as connection:
        _assert_integrity(connection, path)
        digest = hashlib.sha256()
        digest.update(b"oms-sqlite-logical-v1\x00")
        _update_header_metadata(digest, connection)
        schema_rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        _update_sequence(digest, schema_rows)
        _update_sqlite_sequence(digest, connection)
        table_names = sorted(
            row[1] for row in schema_rows if row[0] == "table"
        )
        table_names.extend(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type = 'table' "
                "AND name LIKE 'sqlite_%' "
                "AND name <> 'sqlite_sequence' "
                "ORDER BY name"
            )
        )
        table_names.sort()
        _update_length(digest, len(table_names))
        for table_name in table_names:
            _update_value(digest, table_name)
            columns = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM pragma_table_xinfo(?) ORDER BY cid",
                    (table_name,),
                )
            ]
            if not columns:
                raise sqlite3.DatabaseError(
                    f"SQLite table has no inspectable columns: {table_name}"
                )
            _update_sequence(digest, [(column,) for column in columns])
            table = _quote_identifier(table_name)
            rowid_projection = _selectable_rowid(connection, table, columns)
            _update_value(digest, rowid_projection)
            projections = [_quote_identifier(column) for column in columns]
            if rowid_projection is not None:
                projections.insert(0, rowid_projection)
            projection = ", ".join(projections)
            row_hashes = []
            for row in connection.execute(f"SELECT {projection} FROM {table}"):
                row_digest = hashlib.sha256()
                row_digest.update(b"row-v1\x00")
                _update_sequence(row_digest, [row])
                row_hashes.append(row_digest.digest())
            row_hashes.sort()
            _update_length(digest, len(row_hashes))
            for row_hash in row_hashes:
                _update_bytes(digest, row_hash)
        return digest.hexdigest()


def _update_header_metadata(digest: Any, connection: sqlite3.Connection) -> None:
    """Include persistent header settings whose values are not schema rows."""
    metadata = []
    for pragma in ("application_id", "auto_vacuum", "encoding", "user_version"):
        rows = connection.execute(f"PRAGMA {pragma}").fetchall()
        if len(rows) != 1 or len(rows[0]) != 1:
            raise sqlite3.DatabaseError(
                f"SQLite pragma {pragma} did not return one scalar value: {rows!r}"
            )
        metadata.append((pragma, rows[0][0]))
    _update_sequence(digest, metadata)


def _update_sqlite_sequence(digest: Any, connection: sqlite3.Connection) -> None:
    """Include AUTOINCREMENT state, which controls the next generated identity."""
    sequence_schema = connection.execute(
        "SELECT 1 FROM sqlite_schema "
        "WHERE type = 'table' AND name = 'sqlite_sequence'"
    ).fetchall()
    if len(sequence_schema) > 1:
        raise sqlite3.DatabaseError("SQLite schema contains duplicate sqlite_sequence tables")
    _update_value(digest, "sqlite_sequence")
    _update_value(digest, 1 if sequence_schema else 0)
    if sequence_schema:
        sequence_rows = connection.execute(
            "SELECT name, seq FROM sqlite_sequence"
        ).fetchall()
        _update_unordered_rows(digest, sequence_rows)


def _quote_identifier(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _selectable_rowid(
    connection: sqlite3.Connection,
    table: str,
    columns: list[str],
) -> str | None:
    declared = {column.casefold() for column in columns}
    for alias in ("rowid", "_rowid_", "oid"):
        if alias in declared:
            continue
        projection = _quote_identifier(alias)
        try:
            connection.execute(f"SELECT {projection} FROM {table} LIMIT 0")
        except sqlite3.OperationalError:
            continue
        return projection
    return None


def _update_sequence(
    digest: Any,
    rows: Sequence[tuple[object, ...]],
) -> None:
    _update_length(digest, len(rows))
    for row in rows:
        _update_length(digest, len(row))
        for value in row:
            _update_value(digest, value)


def _update_unordered_rows(
    digest: Any,
    rows: Sequence[tuple[object, ...]],
) -> None:
    row_hashes = []
    for row in rows:
        row_digest = hashlib.sha256()
        row_digest.update(b"unordered-row-v1\x00")
        _update_sequence(row_digest, [row])
        row_hashes.append(row_digest.digest())
    row_hashes.sort()
    _update_length(digest, len(row_hashes))
    for row_hash in row_hashes:
        _update_bytes(digest, row_hash)


def _update_value(
    digest: Any,
    value: object,
) -> None:
    if value is None:
        digest.update(b"n")
        return
    if type(value) is int:
        digest.update(b"i")
        _update_bytes(digest, str(value).encode("ascii"))
        return
    if type(value) is float:
        digest.update(b"f")
        _update_bytes(digest, struct.pack(">d", value))
        return
    if type(value) is str:
        digest.update(b"t")
        _update_bytes(digest, value.encode("utf-8", errors="strict"))
        return
    if type(value) is bytes:
        digest.update(b"b")
        _update_bytes(digest, value)
        return
    raise TypeError(f"unsupported SQLite value type: {type(value).__name__}")


def _update_bytes(digest: Any, value: bytes) -> None:
    _update_length(digest, len(value))
    digest.update(value)


def _update_length(digest: Any, value: int) -> None:
    if type(value) is not int or not 0 <= value < 2**64:
        raise ValueError(f"invalid canonical frame length: {value!r}")
    digest.update(value.to_bytes(8, "big", signed=False))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    arguments = _parse_args()
    digest = backup_database(arguments.source, arguments.destination)
    logical_digest = logical_sha256(arguments.destination)
    print(
        json.dumps(
            {
                "destination": str(arguments.destination.resolve()),
                "logical_sha256": logical_digest,
                "sha256": digest,
                "status": "ok",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
