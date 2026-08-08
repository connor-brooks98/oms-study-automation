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
                CREATE TABLE IF NOT EXISTS operation_journal (
                    operation_id TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result_json TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
                "SELECT content_hash, result_json FROM completed_operations WHERE operation_id = ?",
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

    def begin_operation(
        self, operation_id: UUID, content_hash: str
    ) -> tuple[str, dict[str, Any] | None]:
        """Durably record intent before a local side effect is attempted."""
        identifier = str(operation_id)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT content_hash, state, result_json FROM operation_journal "
                "WHERE operation_id = ?",
                (identifier,),
            ).fetchone()
            if row is None:
                legacy = connection.execute(
                    "SELECT content_hash, result_json FROM completed_operations "
                    "WHERE operation_id = ?",
                    (identifier,),
                ).fetchone()
                if legacy is not None:
                    if legacy[0] != content_hash:
                        raise OperationIdentityConflict(
                            "operation identity was reused with different content"
                        )
                    return "completed", cast(dict[str, Any], json.loads(str(legacy[1])))
                connection.execute(
                    "INSERT INTO operation_journal (operation_id, content_hash, state) "
                    "VALUES (?, ?, 'intent')",
                    (identifier, content_hash),
                )
                return "intent", None
            stored_hash, state, result_json = row
            if stored_hash != content_hash:
                raise OperationIdentityConflict(
                    "operation identity was reused with different content"
                )
            return str(state), None if result_json is None else cast(
                dict[str, Any], json.loads(result_json)
            )

    def complete_operation(
        self, operation_id: UUID, content_hash: str, result: dict[str, Any]
    ) -> None:
        identifier = str(operation_id)
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
        with closing(self._connect()) as connection, connection:
            changed = connection.execute(
                "UPDATE operation_journal SET state='completed', result_json=?, "
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE operation_id=? AND content_hash=?",
                (encoded, identifier, content_hash),
            )
            if changed.rowcount != 1:
                raise OperationIdentityConflict(
                    "operation intent is absent or has a different digest"
                )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)
