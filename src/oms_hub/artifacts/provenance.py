"""Immutable provenance contracts for generated artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from oms_hub.artifacts.models import ArtifactKind
from oms_hub.models import utc_now

__all__ = [
    "ArtifactEvidenceLink",
    "ArtifactRun",
    "compute_artifact_input_hash",
]

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def compute_artifact_input_hash(
    input_payload: object,
    source_revision_ids: Sequence[str] = (),
    evidence_ids: Sequence[str] = (),
) -> str:
    """Hash canonical JSON plus order-independent source/evidence identities."""
    sources = _canonical_ids(source_revision_ids, "source_revision_ids")
    evidence = _canonical_ids(evidence_ids, "evidence_ids")
    _validate_json_value(input_payload, set())
    try:
        encoded = json.dumps(
            {
                "evidence_ids": evidence,
                "input": input_payload,
                "source_revision_ids": sources,
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("artifact input must be canonical JSON data") from error
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ArtifactEvidenceLink:
    artifact_id: str
    source_revision_id: str
    evidence_id: str

    def __post_init__(self) -> None:
        for name in ("artifact_id", "source_revision_id", "evidence_id"):
            _require_text(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class ArtifactRun:
    artifact_id: str
    artifact_kind: ArtifactKind
    recipe_id: str
    recipe_version: str
    provider: str | None
    model: str | None
    prompt_version: str | None
    schema_version: str | None
    source_revision_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    input_hash: str
    output_hash: str
    created_at: str = field(default_factory=utc_now)
    validation_status: str = "valid"
    stale_reason: str | None = None

    def __post_init__(self) -> None:
        for name in ("artifact_id", "recipe_id", "recipe_version", "validation_status"):
            _require_text(getattr(self, name), name)
        for name in ("provider", "model", "prompt_version", "schema_version"):
            value = getattr(self, name)
            if value is not None:
                _require_text(value, name)
        if self.stale_reason is not None:
            _require_text(self.stale_reason, "stale_reason")
        try:
            kind = ArtifactKind(self.artifact_kind)
        except (TypeError, ValueError) as error:
            raise ValueError("artifact_kind is invalid") from error
        object.__setattr__(self, "artifact_kind", kind)
        object.__setattr__(
            self,
            "source_revision_ids",
            _canonical_ids(self.source_revision_ids, "source_revision_ids"),
        )
        object.__setattr__(
            self,
            "evidence_ids",
            _canonical_ids(self.evidence_ids, "evidence_ids"),
        )
        _require_sha256(self.input_hash, "input_hash")
        _require_sha256(self.output_hash, "output_hash")
        _require_utc(self.created_at, "created_at")

    @property
    def kind(self) -> ArtifactKind:
        return self.artifact_kind


def _canonical_ids(values: Sequence[str], name: str) -> tuple[str, ...]:
    items = tuple(values)
    for value in items:
        _require_text(value, name)
    if len(items) != len(set(items)):
        raise ValueError(f"{name} contains duplicate values")
    return tuple(sorted(items))


def _validate_json_value(value: object, ancestors: set[int]) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("artifact input must contain finite JSON numbers")
        return
    if not isinstance(value, (dict, list)):
        raise ValueError("artifact input must contain JSON-compatible data")
    identity = id(value)
    if identity in ancestors:
        raise ValueError("artifact input must not contain cycles")
    ancestors.add(identity)
    try:
        children: Iterable[object]
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise ValueError("artifact input objects must use string keys")
            children = value.values()
        else:
            children = value
        for child in children:
            _validate_json_value(child, ancestors)
    finally:
        ancestors.remove(identity)


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")


def _require_sha256(value: object, name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _require_utc(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a timezone-aware UTC timestamp")
    candidate = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ValueError(f"{name} must be a timezone-aware UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a timezone-aware UTC timestamp")
