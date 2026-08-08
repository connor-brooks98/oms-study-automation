import hashlib
import os
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import numpy as np
from numpy.typing import NDArray
from pydantic import ValidationError

from oms_hub.anki.semantic.domain import (
    DocumentRecord,
    SemanticGenerationMismatchError,
    SemanticManifest,
    SemanticSnapshot,
)


class SemanticSnapshotError(ValueError):
    """A semantic snapshot failed integrity or compatibility validation."""


class SemanticSnapshotStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def generations_path(self) -> Path:
        return self.root / "generations"

    @property
    def current_path(self) -> Path:
        return self.root / "CURRENT"

    def replace(
        self,
        records: Sequence[DocumentRecord],
        vectors: NDArray[Any],
        *,
        model: str,
    ) -> SemanticManifest:
        ordered_records, ordered_vectors = self._validated_replacement(
            records,
            vectors,
            model=model,
        )
        generation = uuid4()
        self.generations_path.mkdir(parents=True, exist_ok=True)
        temporary = self.generations_path / f".building-{generation}"
        destination = self.generations_path / str(generation)
        pointer = self.root / f".CURRENT-{generation}.tmp"
        published = False
        try:
            temporary.mkdir()
            matrix_path = temporary / "vectors.npy"
            with matrix_path.open("wb") as stream:
                np.save(
                    stream,
                    ordered_vectors.astype(np.float16),
                    allow_pickle=False,
                )
                stream.flush()
                os.fsync(stream.fileno())
            manifest = SemanticManifest(
                generation=generation,
                model=model,
                dimensions=ordered_vectors.shape[1],
                created_at=datetime.now(UTC),
                note_ids=tuple(
                    record.note_id for record in ordered_records
                ),
                content_hashes=tuple(
                    record.content_hash for record in ordered_records
                ),
                matrix_sha256=_sha256_file(matrix_path),
            )
            manifest_path = temporary / "manifest.json"
            _write_fsynced(
                manifest_path,
                manifest.model_dump_json(indent=2) + "\n",
            )
            self._load_generation(
                temporary,
                expected_generation=generation,
                expected_model=model,
                expected_dimensions=ordered_vectors.shape[1],
            )
            temporary.rename(destination)
            _fsync_directory(self.generations_path)
            _write_fsynced(pointer, f"{generation}\n")
            os.replace(pointer, self.current_path)
            _fsync_directory(self.root)
            published = True
            self._prune_generations(generation)
            return manifest
        except Exception:
            if pointer.exists():
                pointer.unlink()
            if temporary.exists():
                shutil.rmtree(temporary)
            if destination.exists() and not published:
                shutil.rmtree(destination)
            raise

    def load(
        self,
        *,
        expected_model: str | None = None,
        expected_dimensions: int | None = None,
        expected_generation: str | UUID | None = None,
    ) -> SemanticSnapshot:
        if not self.current_path.is_file():
            raise SemanticSnapshotError("semantic snapshot is unavailable")
        try:
            generation = UUID(
                self.current_path.read_text(encoding="utf-8").strip()
            )
        except (OSError, ValueError) as exc:
            raise SemanticSnapshotError(
                "semantic snapshot pointer is invalid"
            ) from exc
        if expected_generation is not None and str(generation) != str(expected_generation):
            raise SemanticGenerationMismatchError(
                "pinned semantic generation "
                f"{expected_generation} is no longer active"
            )
        return self._load_generation(
            self.generations_path / str(generation),
            expected_generation=generation,
            expected_model=expected_model,
            expected_dimensions=expected_dimensions,
        )

    def _load_generation(
        self,
        path: Path,
        *,
        expected_generation: UUID,
        expected_model: str | None,
        expected_dimensions: int | None,
    ) -> SemanticSnapshot:
        manifest_path = path / "manifest.json"
        matrix_path = path / "vectors.npy"
        try:
            manifest = SemanticManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as exc:
            raise SemanticSnapshotError(
                "semantic snapshot manifest is invalid"
            ) from exc
        if manifest.generation != expected_generation:
            raise SemanticSnapshotError(
                "semantic snapshot generation is invalid"
            )
        if expected_model is not None and manifest.model != expected_model:
            raise SemanticSnapshotError(
                "semantic snapshot model is incompatible"
            )
        if (
            expected_dimensions is not None
            and manifest.dimensions != expected_dimensions
        ):
            raise SemanticSnapshotError(
                "semantic snapshot dimensions are incompatible"
            )
        if (
            len(manifest.note_ids) != len(manifest.content_hashes)
            or len(set(manifest.note_ids)) != len(manifest.note_ids)
            or tuple(sorted(manifest.note_ids)) != manifest.note_ids
            or any(note_id <= 0 for note_id in manifest.note_ids)
            or any(
                not _is_sha256(content_hash)
                for content_hash in manifest.content_hashes
            )
        ):
            raise SemanticSnapshotError(
                "semantic snapshot note metadata is invalid"
            )
        if _sha256_file(matrix_path) != manifest.matrix_sha256:
            raise SemanticSnapshotError(
                "semantic snapshot matrix checksum is invalid"
            )
        try:
            matrix = np.load(
                matrix_path,
                mmap_mode="r",
                allow_pickle=False,
            )
        except (OSError, ValueError) as exc:
            raise SemanticSnapshotError(
                "semantic snapshot matrix is invalid"
            ) from exc
        if (
            matrix.dtype != np.float16
            or matrix.shape
            != (len(manifest.note_ids), manifest.dimensions)
            or not np.isfinite(matrix).all()
        ):
            raise SemanticSnapshotError(
                "semantic snapshot matrix dimensions or values are invalid"
            )
        matrix.flags.writeable = False
        return SemanticSnapshot(manifest=manifest, matrix=matrix)

    @staticmethod
    def _validated_replacement(
        records: Sequence[DocumentRecord],
        vectors: NDArray[Any],
        *,
        model: str,
    ) -> tuple[list[DocumentRecord], NDArray[np.float32]]:
        if not model.strip():
            raise SemanticSnapshotError("semantic model is invalid")
        if len({record.note_id for record in records}) != len(records):
            raise SemanticSnapshotError("semantic note IDs must be unique")
        if any(
            record.note_id <= 0
            or not record.text.strip()
            or not _is_sha256(record.content_hash)
            for record in records
        ):
            raise SemanticSnapshotError(
                "semantic document records are invalid"
            )
        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] != len(records):
            raise SemanticSnapshotError(
                "semantic vector row count does not match records"
            )
        if matrix.shape[1] < 1 or not np.isfinite(matrix).all():
            raise SemanticSnapshotError(
                "semantic vector dimensions or values are invalid"
            )
        order = sorted(range(len(records)), key=lambda index: records[index].note_id)
        return (
            [records[index] for index in order],
            matrix[order].astype(np.float32, copy=False),
        )

    def _prune_generations(self, current: UUID) -> None:
        for path in self.generations_path.iterdir():
            if not path.is_dir() or path.name == str(current):
                continue
            try:
                shutil.rmtree(path)
            except OSError:
                continue


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise SemanticSnapshotError(
            "semantic snapshot matrix is unavailable"
        ) from exc
    return digest.hexdigest()


def _write_fsynced(path: Path, value: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
