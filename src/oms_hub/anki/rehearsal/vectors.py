from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from oms_hub.anki.provider_attempts import begin_provider_call, emit_provider_event
from oms_hub.anki.semantic.domain import FloatMatrix, InputType


class ReplayVectorMiss(RuntimeError):
    """A deterministic rehearsal requested an unseeded vector."""


@dataclass(slots=True)
class EmbeddingReplayEvidence:
    document_replay_hits: int = 0
    query_replay_hits: int = 0
    live_document_calls: int = 0
    live_query_calls: int = 0
    replay_misses: int = 0


class ReplayEmbeddingClient:
    offline_replay_only = True

    def __init__(self, root: Path, *, model: str, dimensions: int) -> None:
        self.root = root
        self.model = model
        self.dimensions = dimensions
        self.evidence = EmbeddingReplayEvidence()

    def seed(
        self,
        texts: Sequence[str],
        *,
        input_type: InputType,
        vectors: FloatMatrix,
    ) -> None:
        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.shape != (len(texts), self.dimensions) or not np.isfinite(matrix).all():
            raise ValueError("replay seed vectors violate the configured dimensions")
        self.root.mkdir(parents=True, exist_ok=True)
        manifest = self._manifest(allow_missing=True)
        for index, text in enumerate(texts):
            key = self._key(text, input_type)
            relative = f"{input_type}/{key}.npy"
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                existing = np.load(destination, allow_pickle=False).astype(np.float32)
                if not np.array_equal(existing, matrix[index]):
                    raise ValueError("replay vector identity was reused with different bytes")
            else:
                np.save(destination, matrix[index], allow_pickle=False)
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            record = {
                "path": relative,
                "sha256": digest,
                "input_type": input_type,
                "text_sha256": hashlib.sha256(_normalize(text).encode()).hexdigest(),
                "size_bytes": destination.stat().st_size,
                "dtype": matrix[index].dtype.name,
                "dimensions": self.dimensions,
            }
            if key in manifest and manifest[key] != record:
                raise ValueError("replay vector manifest identity changed")
            manifest[key] = record
        self._write_manifest(manifest)

    async def embed(
        self,
        texts: Sequence[str],
        *,
        input_type: InputType,
    ) -> FloatMatrix:
        normalized_hashes = [
            hashlib.sha256(_normalize(text).encode()).hexdigest() for text in texts
        ]
        handle = begin_provider_call(
            provider="replay",
            model=self.model,
            instruction="content-addressed embedding replay",
            input_text=json.dumps(normalized_hashes, separators=(",", ":")),
            output_schema={"type": "matrix", "dimensions": self.dimensions},
            generation_parameters={"input_type": input_type, "row_count": len(texts)},
            cacheable_source_prefix=None,
        )
        emit_provider_event(handle, "dispatched")
        try:
            manifest = self._manifest()
            rows: list[np.ndarray] = []
            row_hashes: list[str] = []
            for text in texts:
                key = self._key(text, input_type)
                record = manifest.get(key)
                validated = self._validate_record(record, key=key, input_type=input_type, text=text)
                (
                    path,
                    expected_sha256,
                    expected_size,
                    expected_dtype,
                    expected_dimensions,
                ) = validated
                content = path.read_bytes()
                if expected_size is not None and len(content) != expected_size:
                    raise ValueError("replay vector byte size does not match its manifest")
                digest = hashlib.sha256(content).hexdigest()
                if digest != expected_sha256:
                    raise ValueError("replay vector hash does not match its manifest")
                loaded = np.load(path, allow_pickle=False)
                if not isinstance(loaded, np.ndarray):
                    raise ValueError("replay vector file did not contain an array")
                if expected_dtype is not None and loaded.dtype.name != expected_dtype:
                    raise ValueError("replay vector dtype does not match its manifest")
                if loaded.shape != (expected_dimensions,):
                    raise ValueError("replay vector dimensions do not match the replay contract")
                vector = loaded.astype(np.float32, copy=False)
                if not np.isfinite(vector).all():
                    raise ValueError("replay vector contains non-finite values")
                rows.append(vector)
                row_hashes.append(digest)
            matrix = (
                np.stack(rows).astype(np.float32, copy=False)
                if rows
                else np.empty((0, self.dimensions), dtype=np.float32)
            )
            if matrix.shape != (len(texts), self.dimensions):
                raise ValueError("replay embedding matrix violates the configured dimensions")
        except Exception as exc:
            self.evidence.replay_misses += 1
            # Never persist a filesystem path, malformed JSON value, or a
            # provider-adjacent secret in durable attempt evidence.  The
            # replay is binary: malformed local evidence is simply invalid.
            emit_provider_event(
                handle, "validation_failed", error="replay vector validation failed"
            )
            raise ReplayVectorMiss("replay vector validation failed") from exc
        if input_type == "document":
            self.evidence.document_replay_hits += len(matrix)
        else:
            self.evidence.query_replay_hits += len(matrix)
        response = json.dumps(row_hashes, separators=(",", ":"))
        request_id = f"replay:{hashlib.sha256(response.encode()).hexdigest()[:24]}"
        emit_provider_event(
            handle,
            "response_received",
            request_id=request_id,
            response_text=response,
        )
        emit_provider_event(handle, "accepted", request_id=request_id)
        return matrix

    async def aclose(self) -> None:
        return None

    def _key(self, text: str, input_type: InputType) -> str:
        payload = f"{self.model}\0{self.dimensions}\0{input_type}\0{_normalize(text)}"
        return hashlib.sha256(payload.encode()).hexdigest()

    def _manifest(self, *, allow_missing: bool = False) -> dict[str, object]:
        path = self.root / "manifest.json"
        if not path.exists():
            if allow_missing:
                return {}
            raise ReplayVectorMiss("replay vector manifest is missing")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ReplayVectorMiss("replay vector manifest is invalid")
        return payload

    def _validate_record(
        self,
        record: object,
        *,
        key: str,
        input_type: InputType,
        text: str,
    ) -> tuple[Path, str, int | None, str | None, int]:
        if not isinstance(record, dict):
            raise ReplayVectorMiss("replay vector record is missing or invalid")
        required = {"path", "sha256", "input_type", "text_sha256"}
        if set(record).difference(required | {"size_bytes", "dtype", "dimensions"}):
            raise ReplayVectorMiss("replay vector record has unsupported fields")
        if not required.issubset(record):
            raise ReplayVectorMiss("replay vector record is incomplete")
        relative = record["path"]
        expected_sha256 = record["sha256"]
        recorded_input_type = record["input_type"]
        recorded_text_sha256 = record["text_sha256"]
        if not all(
            isinstance(value, str)
            for value in (relative, expected_sha256, recorded_input_type, recorded_text_sha256)
        ):
            raise ReplayVectorMiss("replay vector record has invalid string fields")
        if (
            len(expected_sha256) != 64
            or len(recorded_text_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in expected_sha256 + recorded_text_sha256
            )
        ):
            raise ReplayVectorMiss("replay vector record has invalid hashes")
        if recorded_input_type != input_type:
            raise ReplayVectorMiss("replay vector record input type does not match")
        if recorded_text_sha256 != hashlib.sha256(_normalize(text).encode()).hexdigest():
            raise ReplayVectorMiss("replay vector record text identity does not match")
        relative_path = Path(relative)
        if (
            not relative
            or "\\" in relative
            or relative_path.is_absolute()
            or any(part in {"", ".", ".."} for part in relative_path.parts)
            or relative != relative_path.as_posix()
        ):
            raise ReplayVectorMiss("replay vector path is not a normalized relative path")
        root = self.root.resolve()
        candidate = (root / relative_path).resolve(strict=True)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ReplayVectorMiss("replay vector path escaped the replay root") from exc
        if not candidate.is_file():
            raise ReplayVectorMiss("replay vector path is not a regular file")
        size_bytes = record.get("size_bytes")
        if size_bytes is not None and (type(size_bytes) is not int or size_bytes < 0):
            raise ReplayVectorMiss("replay vector record byte size is invalid")
        dtype = record.get("dtype")
        if dtype is not None and (not isinstance(dtype, str) or not dtype):
            raise ReplayVectorMiss("replay vector record dtype is invalid")
        dimensions = record.get("dimensions", self.dimensions)
        if type(dimensions) is not int or dimensions != self.dimensions:
            raise ReplayVectorMiss("replay vector record dimensions do not match")
        return candidate, expected_sha256, size_bytes, dtype, dimensions

    def _write_manifest(self, payload: dict[str, object]) -> None:
        (self.root / "manifest.json").write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )


def _normalize(value: str) -> str:
    return " ".join(value.split())
