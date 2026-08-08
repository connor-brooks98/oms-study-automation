"""Bounded durable-manifest recovery for uncommitted stage artifacts.

The manifest path is derived from the exact job, stage, and stage-input hash.  It
is intentionally not a discovery mechanism: a replay may inspect only its own
pointer, after the pipeline has recomputed the same stage identity.
"""

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import UUID

from oms_hub.anki.correction_contracts import OrphanArtifactAdoptionEvidence
from oms_hub.anki.domain import CurationStage, PipelineContractVersion, StageArtifact

_ARTIFACT_SCHEMA_VERSION = 3
_MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class RecoveredOrphan:
    artifact: StageArtifact
    product: Mapping[str, Any]


class OrphanArtifactStore:
    """Records and validates only exact, completed orphan artifact manifests."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def record_complete(
        self,
        artifact: StageArtifact,
        *,
        job_id: UUID,
        artifact_schema_version: int,
    ) -> None:
        if artifact_schema_version != _ARTIFACT_SCHEMA_VERSION:
            raise ValueError("unsupported orphan artifact schema version")
        marker = _complete_marker(artifact.content_sha256)
        evidence = OrphanArtifactAdoptionEvidence(
            job_id=job_id,
            stage=artifact.stage,
            stage_input_sha256=artifact.input_sha256,
            artifact_kind=artifact.kind,
            artifact_schema_version=artifact_schema_version,
            content_sha256=artifact.content_sha256,
            complete_write_marker=marker,
        )
        manifest = {
            "manifest_version": _MANIFEST_SCHEMA_VERSION,
            "artifact_relative_path": artifact.relative_path,
            "evidence": evidence.model_dump(mode="json"),
        }
        manifest_path = self._manifest_path(job_id, artifact.stage, artifact.input_sha256)
        _atomic_write(manifest_path, _encoded(manifest))

    def recover(
        self,
        *,
        job_id: UUID,
        stage: CurationStage,
        input_sha256: str,
        pipeline_contract_version: PipelineContractVersion,
        model_config_sha256: str,
        committed_artifacts: Sequence[StageArtifact],
    ) -> RecoveredOrphan | None:
        """Return one exact valid orphan, or ``None`` without adopting anything."""
        if any(artifact.stage is stage for artifact in committed_artifacts):
            return None
        manifest_path = self._manifest_path(job_id, stage, input_sha256)
        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = _load_canonical_object(manifest_bytes)
            if manifest.get("manifest_version") != _MANIFEST_SCHEMA_VERSION:
                return None
            raw_evidence = manifest.get("evidence")
            if not isinstance(raw_evidence, dict):
                return None
            evidence = OrphanArtifactAdoptionEvidence.model_validate(raw_evidence)
            if (
                evidence.job_id != job_id
                or evidence.stage is not stage
                or evidence.stage_input_sha256 != input_sha256
                or evidence.artifact_schema_version != _ARTIFACT_SCHEMA_VERSION
                or evidence.complete_write_marker != _complete_marker(evidence.content_sha256)
            ):
                return None
            relative_path = manifest.get("artifact_relative_path")
            if not isinstance(relative_path, str):
                return None
            artifact_path = _safe_artifact_path(self.root, relative_path)
            if artifact_path is None:
                return None
            encoded = artifact_path.read_bytes()
            if hashlib.sha256(encoded).hexdigest() != evidence.content_sha256:
                return None
            document = _load_canonical_object(encoded)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not _matches_document(
            document,
            job_id=job_id,
            stage=stage,
            input_sha256=input_sha256,
            evidence=evidence,
            pipeline_contract_version=pipeline_contract_version,
            model_config_sha256=model_config_sha256,
            relative_path=relative_path,
        ):
            return None
        product = document.get("recovery_product")
        if not isinstance(product, dict) or not _is_json_value(product):
            return None
        return RecoveredOrphan(
            artifact=StageArtifact(
                artifact_id=f"{stage.value}:{evidence.content_sha256}",
                stage=stage,
                kind=evidence.artifact_kind,
                relative_path=relative_path,
                input_sha256=input_sha256,
                content_sha256=evidence.content_sha256,
                pipeline_contract_version=pipeline_contract_version,
                model_config_sha256=model_config_sha256,
                metadata=dict(document["metadata"]),
            ),
            product=product,
        )

    def _manifest_path(self, job_id: UUID, stage: CurationStage, input_sha256: str) -> Path:
        if len(input_sha256) != 64 or any(char not in "0123456789abcdef" for char in input_sha256):
            raise ValueError("stage input hash is invalid")
        return self.root / str(job_id) / stage.value / ".orphan" / f"{input_sha256}.json"


def product_snapshot(product: Any) -> dict[str, Any]:
    """Serialize every ``StageProduct`` field that affects ``commit_stage``."""
    return {
        "kind": product.kind,
        "payload": product.payload,
        "metadata": product.metadata,
        "usage": _json_data(product.usage),
        "cache_hits": product.cache_hits,
        "candidates": _json_data(product.candidates),
        "source_evidence": _json_data(product.source_evidence),
        "gap_cards": _json_data(product.gap_cards),
        "job_pins": product.job_pins,
        "blocking_error": product.blocking_error,
    }


def _matches_document(
    document: Mapping[str, Any],
    *,
    job_id: UUID,
    stage: CurationStage,
    input_sha256: str,
    evidence: OrphanArtifactAdoptionEvidence,
    pipeline_contract_version: PipelineContractVersion,
    model_config_sha256: str,
    relative_path: str,
) -> bool:
    expected_path = f"{job_id}/{stage.value}/{evidence.content_sha256}.json"
    recovery_product = document.get("recovery_product")
    return (
        document.get("artifact_version") == _ARTIFACT_SCHEMA_VERSION
        and document.get("job_id") == str(job_id)
        and document.get("stage") == stage.value
        and document.get("kind") == evidence.artifact_kind
        and document.get("pipeline_contract_version") == pipeline_contract_version.value
        and document.get("model_config_sha256") == model_config_sha256
        and document.get("input_sha256") == input_sha256
        and relative_path == expected_path
        and isinstance(document.get("payload"), dict)
        and isinstance(document.get("metadata"), dict)
        and isinstance(recovery_product, dict)
        and recovery_product.get("kind") == document.get("kind")
        and recovery_product.get("payload") == document.get("payload")
        and recovery_product.get("metadata") == document.get("metadata")
    )


def _json_data(value: Any) -> Any:
    if value is None:
        return None
    if is_dataclass(value):
        return _json_data(asdict(cast(Any, value)))
    if isinstance(value, Mapping):
        return {str(key): _json_data(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_data(item) for item in value]
    return value


def _is_json_value(value: object) -> bool:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


def _load_canonical_object(encoded: bytes) -> dict[str, Any]:
    document = json.loads(encoded)
    if not isinstance(document, dict) or _encoded(document) != encoded:
        raise ValueError("document is not canonical")
    return document


def _encoded(document: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _safe_artifact_path(root: Path, relative_path: str) -> Path | None:
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        return None
    return root.joinpath(*relative.parts)


def _complete_marker(content_sha256: str) -> str:
    return f"orphan-artifact-v1:{content_sha256}"


def _atomic_write(destination: Path, encoded: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}-",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
