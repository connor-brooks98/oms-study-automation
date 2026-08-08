import json
import os
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self, cast

import numpy as np
from numpy.typing import NDArray


class Embedder(Protocol):
    @property
    def model_name(self) -> str: ...

    def embed(self, texts: Sequence[str]) -> NDArray[np.float32]: ...


class FileLock:
    """Small cross-platform exclusive lock based on atomic file creation."""

    def __init__(self, path: Path, *, timeout_seconds: float = 10.0) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._descriptor: int | None = None

    def __enter__(self) -> Self:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self._descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for lock {self.path.name}") from None
                time.sleep(0.01)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None
        self.path.unlink(missing_ok=True)


def normalize_vectors(values: NDArray[np.float32]) -> NDArray[np.float32]:
    vectors = np.asarray(values, dtype=np.float32)
    if vectors.ndim != 2:
        raise ValueError("embedding output must be a two-dimensional matrix")
    lengths = np.linalg.norm(vectors, axis=1, keepdims=True)
    if np.any(lengths == 0):
        raise ValueError("embedding output contains a zero-length row")
    return cast(NDArray[np.float32], vectors / lengths)


class AtomicVectorStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.vectors_path = root / "vectors.npy"
        self.note_ids_path = root / "note_ids.json"
        self.lock_path = root / ".vectors.lock"

    def replace(
        self,
        note_ids: Sequence[int],
        vectors: NDArray[np.float32],
    ) -> None:
        ids = list(note_ids)
        matrix = np.asarray(vectors, dtype=np.float32)
        if (
            matrix.ndim != 2
            or matrix.shape[0] != len(ids)
            or len(ids) != len(set(ids))
            or any(note_id <= 0 for note_id in ids)
        ):
            raise ValueError("vector rows and unique positive note IDs must align")
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_path):
            vector_temp = _temporary_path(self.root, ".vectors-", ".npy.tmp")
            ids_temp = _temporary_path(self.root, ".note-ids-", ".json.tmp")
            try:
                with vector_temp.open("wb") as stream:
                    np.save(stream, matrix, allow_pickle=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                with ids_temp.open("w", encoding="utf-8", newline="\n") as stream:
                    json.dump(ids, stream, separators=(",", ":"))
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(vector_temp, self.vectors_path)
                os.replace(ids_temp, self.note_ids_path)
            finally:
                vector_temp.unlink(missing_ok=True)
                ids_temp.unlink(missing_ok=True)

    def load(self) -> tuple[list[int], NDArray[np.float32]]:
        with FileLock(self.lock_path):
            try:
                raw_ids = json.loads(self.note_ids_path.read_text(encoding="utf-8"))
                vectors = np.load(self.vectors_path, allow_pickle=False)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("vector store is missing or invalid") from exc
        if not isinstance(raw_ids, list) or not all(
            isinstance(note_id, int) and not isinstance(note_id, bool) and note_id > 0
            for note_id in raw_ids
        ):
            raise ValueError("vector note-ID order is invalid")
        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] != len(raw_ids):
            raise ValueError("vector matrix and note-ID order do not align")
        return cast(list[int], raw_ids), matrix


def _temporary_path(root: Path, prefix: str, suffix: str) -> Path:
    descriptor, value = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=root)
    os.close(descriptor)
    return Path(value)
