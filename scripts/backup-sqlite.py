"""Create an integrity-checked SQLite online backup for Windows rollout safety."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path


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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    arguments = _parse_args()
    digest = backup_database(arguments.source, arguments.destination)
    print(
        json.dumps(
            {
                "destination": str(arguments.destination.resolve()),
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
