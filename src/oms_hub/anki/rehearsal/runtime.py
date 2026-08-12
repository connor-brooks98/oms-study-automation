from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from oms_hub.anki.ankiconnect import AnkiConnectActionError
from oms_hub.anki.index import AnkiIndex


class RehearsalMutationDenied(AnkiConnectActionError):
    """A mutation was attempted against the read-only rehearsal snapshot."""


class ReadOnlyAnkiGateway:
    """Snapshot-backed Anki gateway which leaves a minimal mutation audit trail.

    Evidence is deliberately optional at this class boundary so unit-only uses remain
    lightweight.  App composition requires it for every rehearsal process.
    """

    _schema_version = 1

    def __init__(
        self,
        companion: AnkiIndex,
        *,
        profile: str = "A0 Rehearsal",
        evidence_directory: Path | None = None,
        run_nonce: str | None = None,
    ) -> None:
        if (evidence_directory is None) != (run_nonce is None):
            raise ValueError("read-only evidence requires both directory and run nonce")
        self.companion = companion
        self.profile = profile
        self.mutation_attempts: list[str] = []
        self._evidence_path: Path | None = None
        self._run_nonce = run_nonce
        self._records: list[dict[str, object]] = []
        if evidence_directory is not None:
            evidence_directory.mkdir(parents=True, exist_ok=True)
            self._evidence_path = evidence_directory / "read-only-anki-mutation-ledger.json"
            self._records = self._load_records()

    async def version(self) -> int:
        return 6

    async def get_active_profile(self) -> str:
        return self.profile

    async def find_notes(self, query: str) -> list[int]:
        normalized = query.strip()
        if not normalized:
            return [note.note_id for note in self.companion.list_notes()]
        if normalized.startswith("nid:"):
            values = normalized.removeprefix("nid:").split(",")
            requested = {int(value) for value in values if value.strip().isdigit()}
            return [
                note.note_id for note in self.companion.list_notes() if note.note_id in requested
            ]
        raise AnkiConnectActionError("rehearsal adapter supports only empty or nid searches")

    async def notes_info(self, note_ids: Sequence[int]) -> list[dict[str, Any]]:
        notes = []
        for note_id in note_ids:
            note = self.companion.get_note(note_id)
            if note is None:
                continue
            notes.append(
                {
                    "noteId": note.note_id,
                    "modelName": note.model_name,
                    "tags": list(note.tags),
                    "fields": {
                        name: {"value": value, "order": order}
                        for order, (name, value) in enumerate(note.raw_fields.items())
                    },
                    "cards": list(note.card_ids),
                }
            )
        return notes

    async def add_tags(self, note_ids: Sequence[int], tags: Sequence[str]) -> None:
        del note_ids, tags
        self._deny("addTags")

    async def remove_tags(self, note_ids: Sequence[int], tags: Sequence[str]) -> None:
        del note_ids, tags
        self._deny("removeTags")

    async def add_notes(self, notes: Sequence[dict[str, Any]]) -> list[int | None]:
        del notes
        self._deny("addNotes")

    async def sync(self) -> None:
        self._deny("sync")

    async def aclose(self) -> None:
        self._persist()
        return None

    def _deny(self, action: str) -> NoReturn:
        self.mutation_attempts.append(action)
        self._records.append(
            {
                "action": action,
                "ordinal": len(self._records) + 1,
                "timestamp": _timestamp(),
                "outcome": "denied",
            }
        )
        self._persist()
        raise RehearsalMutationDenied(f"rehearsal adapter denied {action}")

    def _load_records(self) -> list[dict[str, object]]:
        assert self._evidence_path is not None
        if not self._evidence_path.exists():
            return []
        try:
            payload = json.loads(self._evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("read-only mutation evidence is malformed") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != self._schema_version
            or payload.get("run_nonce") != self._run_nonce
            or not isinstance(payload.get("records"), list)
        ):
            raise RuntimeError("read-only mutation evidence is stale or malformed")
        records = payload["records"]
        if not all(_valid_record(record, ordinal) for ordinal, record in enumerate(records, 1)):
            raise RuntimeError("read-only mutation evidence has an invalid sequence")
        return list(records)

    def _persist(self) -> None:
        if self._evidence_path is None:
            return
        _atomic_json(
            self._evidence_path,
            {
                "schema_version": self._schema_version,
                "run_nonce": self._run_nonce,
                "records": self._records,
            },
        )


def _valid_record(value: object, ordinal: int) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"action", "ordinal", "timestamp", "outcome"}
        and isinstance(value.get("action"), str)
        and value.get("ordinal") == ordinal
        and isinstance(value.get("timestamp"), str)
        and value.get("outcome") == "denied"
    )


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    """Durably replace one evidence file without ever writing outside its directory."""
    parent = path.parent.resolve()
    target = path.resolve(strict=False)
    try:
        target.relative_to(parent)
    except ValueError as exc:
        raise ValueError("runtime evidence path escapes its overlay directory") from exc
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _fsync_directory(path: Path) -> None:
    """Durably sync a POSIX directory; Windows has no compatible directory handle."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class NoopLauncher:
    async def is_running(self) -> bool:
        return True

    async def launch(self) -> None:
        return None
