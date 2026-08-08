import gzip
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

from oms_hub.anki.contracts import (
    SnapshotManifest,
    SnapshotNote,
    canonical_payload_sha256,
)


class SnapshotValidationError(ValueError):
    """A snapshot failed integrity or resource-bound validation."""


@dataclass(frozen=True, slots=True)
class StagedSnapshot:
    root: Path
    manifest_path: Path
    notes_path: Path
    note_count: int


def hash_id_set(note_ids: Iterable[int]) -> str:
    digest = hashlib.sha256()
    for note_id in note_ids:
        digest.update(str(note_id).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def hash_content_sequence(content_hashes: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for content_hash in content_hashes:
        digest.update(content_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def snapshot_note_hash(payload: dict[str, object]) -> str:
    canonical = dict(payload)
    canonical.pop("content_sha256", None)
    return canonical_payload_sha256(canonical)


def stage_full_snapshot(
    manifest: SnapshotManifest,
    source: Path,
    destination: Path,
    *,
    max_decompressed_bytes: int,
    max_row_bytes: int,
) -> StagedSnapshot:
    if max_decompressed_bytes < 1 or max_row_bytes < 1:
        raise ValueError("snapshot limits must be positive")
    if canonical_payload_sha256(manifest) != manifest.payload_sha256:
        raise SnapshotValidationError("snapshot manifest hash mismatch")
    destination.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".staging-", dir=destination))
    notes_path = temporary / "notes.jsonl"
    manifest_path = temporary / "manifest.json"
    note_ids: list[int] = []
    content_hashes: list[str] = []
    total_bytes = 0
    try:
        with _open_snapshot(source) as input_stream, notes_path.open("wb") as output:
            while True:
                row = input_stream.readline(max_row_bytes + 2)
                if not row:
                    break
                total_bytes += len(row)
                if len(row) > max_row_bytes or total_bytes > max_decompressed_bytes:
                    raise SnapshotValidationError("snapshot exceeds configured size limits")
                try:
                    raw = json.loads(row)
                    note = SnapshotNote.model_validate(raw)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise SnapshotValidationError("snapshot contains an invalid row") from exc
                if snapshot_note_hash(raw) != note.content_sha256:
                    raise SnapshotValidationError("snapshot note hash mismatch")
                if note_ids and note.note_id <= note_ids[-1]:
                    raise SnapshotValidationError(
                        "snapshot note IDs must be unique and sorted"
                    )
                note_ids.append(note.note_id)
                content_hashes.append(note.content_sha256)
                output.write(
                    (
                        json.dumps(
                            note.model_dump(mode="json"),
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        )
                        + "\n"
                    ).encode("utf-8")
                )
            output.flush()
            os.fsync(output.fileno())
        if len(note_ids) != manifest.note_count:
            raise SnapshotValidationError("snapshot note count mismatch")
        if hash_id_set(note_ids) != manifest.id_set_sha256:
            raise SnapshotValidationError("snapshot ID-set hash mismatch")
        if hash_content_sequence(content_hashes) != manifest.content_sha256:
            raise SnapshotValidationError("snapshot content fingerprint mismatch")
        manifest_path.write_text(
            manifest.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        _fsync_file(manifest_path)
        current = destination / "current"
        _replace_directory(temporary, current)
        return StagedSnapshot(
            root=current,
            manifest_path=current / "manifest.json",
            notes_path=current / "notes.jsonl",
            note_count=len(note_ids),
        )
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def _open_snapshot(path: Path) -> AbstractContextManager[BinaryIO]:
    if path.suffix.casefold() == ".gz":
        return cast(AbstractContextManager[BinaryIO], gzip.open(path, "rb"))
    return path.open("rb")


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _replace_directory(source: Path, target: Path) -> None:
    backup = target.with_name(f".{target.name}.previous")
    if backup.exists():
        shutil.rmtree(backup)
    if target.exists():
        target.rename(backup)
    try:
        source.rename(target)
    except Exception:
        if backup.exists() and not target.exists():
            backup.rename(target)
        raise
    if backup.exists():
        shutil.rmtree(backup)
