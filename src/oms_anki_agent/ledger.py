import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID


class OperationIdentityConflict(RuntimeError):
    """An operation UUID was reused with different immutable content."""


class SnapshotVersionInfo(Protocol):
    export_version: str
    normalizer_version: str
    embedding_model: str


class AgentLedger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS snapshot_notes (
                    note_id INTEGER PRIMARY KEY,
                    content_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS completed_operations (
                    operation_id TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def replace_note_hashes(self, values: dict[int, str]) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM snapshot_notes")
            connection.executemany(
                "INSERT INTO snapshot_notes (note_id, content_hash) VALUES (?, ?)",
                sorted(values.items()),
            )

    def note_hashes(self) -> dict[int, str]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT note_id, content_hash FROM snapshot_notes ORDER BY note_id"
            ).fetchall()
        return {int(note_id): str(content_hash) for note_id, content_hash in rows}

    def set_snapshot_state(
        self,
        *,
        exported_at: datetime,
        note_count: int,
        versions: SnapshotVersionInfo,
    ) -> None:
        value = json.dumps(
            {
                "exported_at": exported_at.isoformat(),
                "note_count": note_count,
                "export_version": versions.export_version,
                "normalizer_version": versions.normalizer_version,
                "embedding_model": versions.embedding_model,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO metadata (key, value) VALUES ('snapshot_state', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (value,),
            )

    def snapshot_state(self) -> dict[str, Any] | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'snapshot_state'"
            ).fetchone()
        if row is None:
            return None
        value = json.loads(str(row[0]))
        return cast(dict[str, Any], value)

    def record_operation(
        self,
        operation_id: UUID,
        content_hash: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        identifier = str(operation_id)
        result_json = json.dumps(
            result,
            sort_keys=True,
            separators=(",", ":"),
        )
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                "SELECT content_hash, result_json FROM completed_operations "
                "WHERE operation_id = ?",
                (identifier,),
            ).fetchone()
            if existing is not None:
                stored_hash, stored_result = existing
                if stored_hash != content_hash:
                    raise OperationIdentityConflict(
                        f"operation {identifier} was reused with different content"
                    )
                return cast(dict[str, Any], json.loads(stored_result))
            connection.execute(
                "INSERT INTO completed_operations "
                "(operation_id, content_hash, result_json) VALUES (?, ?, ?)",
                (identifier, content_hash, result_json),
            )
        return result

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)
