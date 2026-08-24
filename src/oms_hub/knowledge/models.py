"""Immutable source-trust domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from oms_hub.models import utc_now
from oms_hub.providers.contracts import AuthorityClass

__all__ = [
    "EvidenceLocator",
    "EvidenceLocatorKind",
    "EvidenceUnit",
    "KnowledgeSource",
    "SourceRevision",
    "SourceRevisionState",
]


class SourceRevisionState(StrEnum):
    STAGED = "staged"
    NORMALIZING = "normalizing"
    READY = "ready"
    STALE = "stale"
    FAILED = "failed"
    RETIRED = "retired"


class EvidenceLocatorKind(StrEnum):
    PAGE = "page"
    SLIDE = "slide"
    SPEAKER_NOTE = "speaker_note"
    TRANSCRIPT_SEGMENT = "transcript_segment"
    SECTION = "section"
    FIGURE = "figure"
    TABLE = "table"
    ARTICLE_PAGE = "article_page"


@dataclass(frozen=True, slots=True)
class KnowledgeSource:
    source_document_id: str
    authority_class: AuthorityClass

    def __post_init__(self) -> None:
        object.__setattr__(self, "authority_class", AuthorityClass(self.authority_class))


@dataclass(frozen=True, slots=True)
class SourceRevision:
    source_document_id: str
    source_revision_id: str
    file_sha256: str
    state: SourceRevisionState

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", SourceRevisionState(self.state))

    @property
    def revision_id(self) -> str:
        """Compatibility name for the Task 1.3 repository consumer."""

        return self.source_revision_id


@dataclass(frozen=True, slots=True)
class EvidenceLocator:
    kind: EvidenceLocatorKind
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", EvidenceLocatorKind(self.kind))


def _validate_utc_timestamp(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a timezone-aware UTC ISO-8601 timestamp")
    candidate = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ValueError(
            f"{field_name} must be a timezone-aware UTC ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be a timezone-aware UTC ISO-8601 timestamp")


@dataclass(frozen=True, slots=True)
class EvidenceUnit:
    evidence_id: str
    source_revision_id: str
    authority_class: AuthorityClass
    course_id: str | None
    exam_id: str | None
    lecture_id: str | None
    locator: EvidenceLocator
    normalized_text: str
    content_sha256: str
    image_asset_id: str | None = None
    source_priority: int = 0
    created_at: str = field(default_factory=utc_now)
    retired_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "authority_class", AuthorityClass(self.authority_class))
        if self.authority_class == AuthorityClass.COURSE_MATERIAL and (
            not isinstance(self.course_id, str) or not self.course_id.strip()
        ):
            raise ValueError("course_id is required for course material evidence")
        _validate_utc_timestamp(self.created_at, "created_at")
        if self.retired_at is not None:
            _validate_utc_timestamp(self.retired_at, "retired_at")

    @property
    def supports_medical_claims(self) -> bool:
        return self.authority_class in (
            AuthorityClass.COURSE_MATERIAL,
            AuthorityClass.PUBLISHED_JOURNAL,
        )
