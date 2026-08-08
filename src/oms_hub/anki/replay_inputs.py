"""Immutable, persisted replay-input documents for Anki curation stages."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from oms_hub.anki.domain import CurationStage


def canonical_json(value: object) -> str:
    """Serialize one replay document in the only representation we persist."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PreparedStageReplayInputs:
    """An exact durable input document for one job stage.

    ``canonical_json`` is the replay input to hash.  ``document`` deliberately
    returns a new decoded value on every access, so callers cannot mutate the
    persisted immutable representation through this value object.
    """

    job_id: UUID
    stage: CurationStage
    canonical_json: str
    sha256: str

    def __post_init__(self) -> None:
        try:
            value = json.loads(self.canonical_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("prepared replay inputs must contain JSON") from exc
        if canonical_json(value) != self.canonical_json:
            raise ValueError("prepared replay inputs must use canonical JSON")
        if sha256_text(self.canonical_json) != self.sha256:
            raise ValueError("prepared replay input SHA-256 does not match its document")

    @property
    def document(self) -> dict[str, Any]:
        value = json.loads(self.canonical_json)
        if not isinstance(value, dict):  # pragma: no cover - repository always stores objects
            raise AssertionError("prepared replay input document is not an object")
        return value


__all__ = ["PreparedStageReplayInputs", "canonical_json", "sha256_text"]
