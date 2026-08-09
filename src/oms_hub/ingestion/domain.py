from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class UploadKind(StrEnum):
    SLIDES = "slides"
    TRANSCRIPTS = "transcripts"


class UploadState(StrEnum):
    UPLOADING = "uploading"
    MATCHING = "matching"
    QUARANTINED = "quarantined"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    QUEUED = "queued"
    PROCESSING = "processing"
    NEEDS_REVIEW = "needs_review"
    COMPLETE = "complete"
    DISCARDED = "discarded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class UploadEvidence:
    filename: str
    embedded_title: str
    opening_text: str


@dataclass(frozen=True, slots=True)
class MatchDecision:
    state: str
    lecture_id: int | None
    confidence: float
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UploadBatchRef:
    id: str
    kind: UploadKind


@dataclass(frozen=True, slots=True)
class StagedUpload:
    batch_id: str
    item_id: str
    path: Path
    sha256: str
    size_bytes: int
    original_filename: str


@dataclass(frozen=True, slots=True)
class ChunkSession:
    id: str
    batch_id: str
    item_id: str
    kind: UploadKind
    filename: str
    total_size: int
    expected_sha256: str
    received: int
    expires_at: str


@dataclass(frozen=True, slots=True)
class UploadManifestSlot:
    """An immutable member of a browser upload action.

    Slots deliberately exist only in staging until every member has passed
    validation.  They are not queue records.
    """

    id: str
    filename: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class UploadManifest:
    id: str
    kind: UploadKind
    lecture_id: int | None
    slots: tuple[UploadManifestSlot, ...]


@dataclass(frozen=True, slots=True)
class UploadItem:
    id: str
    kind: UploadKind
    original_filename: str
    sha256: str
    size_bytes: int
    state: UploadState
    lecture_id: int | None
    confidence: float
    evidence: tuple[str, ...]
    manual_assignment: bool
    error: str | None


@dataclass(frozen=True, slots=True)
class StoredUploadItem:
    id: str
    batch_id: str
    kind: UploadKind
    original_filename: str
    staged_path: Path
    sha256: str
    size_bytes: int
    state: UploadState
    lecture_id: int | None
    confidence: float
    evidence: tuple[str, ...]
    manual_assignment: bool
    error: str | None


@dataclass(frozen=True, slots=True)
class UploadBatch:
    id: str
    kind: UploadKind
    state: UploadState
    created_at: str
    updated_at: str
    items: tuple[UploadItem, ...]
    lifecycle: str = "active"
    outcome: str = "uploading"

    def public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "state": self.state.value,
            "lifecycle": self.lifecycle,
            "outcome": self.outcome,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "items": [
                {
                    "id": item.id,
                    "kind": item.kind.value,
                    "original_filename": item.original_filename,
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                    "state": item.state.value,
                    "lecture_id": item.lecture_id,
                    "confidence": item.confidence,
                    "evidence": list(item.evidence),
                    "manual_assignment": item.manual_assignment,
                    "error": item.error,
                }
                for item in self.items
            ],
        }


@dataclass(frozen=True, slots=True)
class StudyRevision:
    id: int
    upload_item_id: str
    lecture_id: int
    kind: UploadKind
    source_sha256: str
    immutable_source_path: Path
    derived_sha256: str | None
    immutable_derived_path: Path | None
    canonical_source_path: Path | None
    canonical_derived_path: Path | None
    icloud_path: Path | None
    prompt_sha256: str | None
    state: str
    current: bool


@dataclass(frozen=True, slots=True)
class IngestionJob:
    id: int
    upload_item_id: str
    kind: UploadKind
    action: str
    attempts: int
    claimed_at: datetime
