import json
import sqlite3
from pathlib import Path
from typing import Any, cast
from uuid import UUID


class OperationIdentityConflict(RuntimeError):
    """An operation UUID was reused with different immutable content."""


class AgentLedger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as connection:
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
        with self._connect() as connection:
            connection.execute("DELETE FROM snapshot_notes")
            connection.executemany(
                "INSERT INTO snapshot_notes (note_id, content_hash) VALUES (?, ?)",
                sorted(values.items()),
            )

    def note_hashes(self) -> dict[int, str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT note_id, content_hash FROM snapshot_notes ORDER BY note_id"
            ).fetchall()
        return {int(note_id): str(content_hash) for note_id, content_hash in rows}

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
        with self._connect() as connection:
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
