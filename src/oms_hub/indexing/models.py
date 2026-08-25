"""Durable local models for provider-backed source indexes."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar, cast
from uuid import uuid4

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from oms_hub.models import Base


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class IndexState(StrEnum):
    NOT_INDEXED = "not_indexed"
    UPLOADING_FILE = "uploading_file"
    FILE_UPLOADED = "file_uploaded"
    IMPORTING = "importing"
    READY = "ready"
    STALE = "stale"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"
    DELETING = "deleting"
    DELETED = "deleted"


# Keep this graph explicit. A state not listed here is deliberately not a
# transition, including a self-transition.
ALLOWED_TRANSITIONS: dict[IndexState, frozenset[IndexState]] = {
    IndexState.NOT_INDEXED: frozenset({IndexState.UPLOADING_FILE}),
    IndexState.UPLOADING_FILE: frozenset(
        {IndexState.FILE_UPLOADED, IndexState.RETRYABLE_FAILURE, IndexState.TERMINAL_FAILURE}
    ),
    IndexState.FILE_UPLOADED: frozenset(
        {IndexState.IMPORTING, IndexState.RETRYABLE_FAILURE, IndexState.TERMINAL_FAILURE}
    ),
    IndexState.IMPORTING: frozenset(
        {IndexState.READY, IndexState.RETRYABLE_FAILURE, IndexState.TERMINAL_FAILURE}
    ),
    IndexState.READY: frozenset({IndexState.STALE, IndexState.DELETING}),
    IndexState.STALE: frozenset(
        {IndexState.UPLOADING_FILE, IndexState.RETRYABLE_FAILURE, IndexState.TERMINAL_FAILURE,
         IndexState.DELETING}
    ),
    IndexState.RETRYABLE_FAILURE: frozenset(
        {IndexState.UPLOADING_FILE, IndexState.IMPORTING, IndexState.TERMINAL_FAILURE,
         IndexState.DELETING}
    ),
    IndexState.TERMINAL_FAILURE: frozenset({IndexState.UPLOADING_FILE, IndexState.DELETING}),
    IndexState.DELETING: frozenset({IndexState.DELETED}),
    IndexState.DELETED: frozenset(),
}


def validate_transition(before: IndexState, after: IndexState) -> None:
    """Raise when a persisted lifecycle jump is not explicitly approved."""

    try:
        allowed = ALLOWED_TRANSITIONS[before]
    except KeyError as error:
        raise ValueError(f"unknown index state: {before!r}") from error
    if after not in allowed:
        raise ValueError(f"invalid index transition: {before.value} -> {after.value}")


class StoreKey:
    """Validated, deterministic local namespace identity.

    The value form is accepted for callers crossing a persistence boundary;
    classmethods are preferred when constructing a new key.
    """

    MAX_IDENTIFIER_LENGTH: ClassVar[int] = 100
    MAX_DISPLAY_NAME_LENGTH: ClassVar[int] = 100
    _IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

    kind: str
    course_id: str
    exam_id: str | None

    __slots__ = ("kind", "course_id", "exam_id")

    def __init__(
        self,
        value: str | None = None,
        *,
        kind: str | None = None,
        course_id: str | None = None,
        exam_id: str | None = None,
    ) -> None:
        if value is not None:
            parsed_kind, parsed_course, parsed_exam = self._parse_value(value)
        else:
            if kind is None or course_id is None:
                raise ValueError("store key requires a kind and course identifier")
            parsed_kind, parsed_course, parsed_exam = kind, course_id, exam_id
        self._validate_parts(parsed_kind, parsed_course, parsed_exam)
        object.__setattr__(self, "kind", parsed_kind)
        object.__setattr__(self, "course_id", parsed_course)
        object.__setattr__(self, "exam_id", parsed_exam)

    @classmethod
    def course(cls, course_id: str, exam_id: str) -> StoreKey:
        return cls(kind="course", course_id=course_id, exam_id=exam_id)

    @classmethod
    def literature(cls, course_id: str) -> StoreKey:
        return cls(kind="literature", course_id=course_id)

    @classmethod
    def parse(cls, value: str) -> StoreKey:
        return cls(value)

    @classmethod
    def _parse_value(cls, value: str) -> tuple[str, str, str | None]:
        if not isinstance(value, str):
            raise ValueError("store key must be a string")
        parts = value.split(":")
        if len(parts) == 4 and parts[0] == "course" and parts[2] == "exam":
            return parts[0], parts[1], parts[3]
        if len(parts) == 2 and parts[0] == "literature":
            return parts[0], parts[1], None
        raise ValueError("store key does not match an approved namespace")

    @classmethod
    def _validate_parts(cls, kind: str, course_id: str, exam_id: str | None) -> None:
        if kind not in {"course", "literature"}:
            raise ValueError("store key namespace is not approved")
        for label, value in (("course", course_id), ("exam", exam_id)):
            if value is None:
                if kind == "course" or label == "course":
                    raise ValueError(f"{label} identifier is required")
                continue
            if not isinstance(value, str) or not cls._IDENTIFIER.fullmatch(value):
                raise ValueError(f"{label} identifier is blank, unbounded, or ambiguous")
        if kind == "literature" and exam_id is not None:
            raise ValueError("literature store keys cannot carry an exam identifier")
        if kind == "course" and exam_id is None:
            raise ValueError("course store keys require an exam identifier")

    @property
    def value(self) -> str:
        if self.kind == "course":
            return f"course:{self.course_id}:exam:{self.exam_id}"
        return f"literature:{self.course_id}"

    @property
    def namespace(self) -> str:
        return self.kind

    @property
    def authority_namespace(self) -> str:
        return "course_material" if self.kind == "course" else "published_journal"

    @property
    def display_name(self) -> str:
        raw = f"Study Hub {self.kind} {self.course_id}"
        if self.exam_id is not None:
            raw += f" exam {self.exam_id}"
        sanitized = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-")
        return sanitized[: self.MAX_DISPLAY_NAME_LENGTH].rstrip("-") or "Study-Hub-Store"

    def __str__(self) -> str:
        return self.value

    def __repr__(self) -> str:
        return f"StoreKey({self.value!r})"

    def __hash__(self) -> int:
        return hash(self.value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, StoreKey) and self.value == other.value


def _require_text(value: str, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    normalized = value.strip()
    if len(normalized) > maximum or not normalized.isprintable():
        raise ValueError(f"{field_name} is unbounded or contains control characters")
    return normalized


def _validated_json(value: Any) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        return json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ValueError("metadata must be valid JSON") from error


@dataclass(frozen=True, slots=True)
class ProviderStore:
    store_key: StoreKey | str
    provider: str
    provider_store_name: str
    embedding_model: str
    authority_namespace: str
    course_id: str
    exam_id: str | None = None
    state: IndexState = IndexState.READY
    generation: int = 1
    is_current: bool = True
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        key = (
            self.store_key
            if isinstance(self.store_key, StoreKey)
            else StoreKey.parse(self.store_key)
        )
        object.__setattr__(self, "store_key", key)
        object.__setattr__(self, "provider", _require_text(self.provider, "provider", 50))
        if self.course_id != key.course_id:
            raise ValueError("course id does not match store key")
        if self.exam_id != key.exam_id:
            raise ValueError("exam id does not match store key")
        if self.authority_namespace != key.authority_namespace:
            raise ValueError("authority namespace does not match store key")
        object.__setattr__(
            self,
            "provider_store_name",
            _require_text(self.provider_store_name, "provider store name", 500),
        )
        object.__setattr__(
            self,
            "embedding_model",
            _require_text(self.embedding_model, "embedding model", 200),
        )
        object.__setattr__(
            self,
            "authority_namespace",
            _require_text(self.authority_namespace, "authority namespace", 100),
        )
        object.__setattr__(self, "course_id", _require_text(self.course_id, "course id", 100))
        if self.exam_id is not None:
            object.__setattr__(self, "exam_id", _require_text(self.exam_id, "exam id", 100))
        if self.generation < 1:
            raise ValueError("store generation must be positive")
        _require_text(self.id, "store id", 36)

    @property
    def key(self) -> StoreKey:
        return cast(StoreKey, self.store_key)

    @property
    def provider_name(self) -> str:
        return self.provider_store_name


@dataclass(frozen=True, slots=True)
class ProviderDocument:
    store_id: str
    provider: str
    provider_document_id: str | None
    source_revision_id: str
    provider_file_name: str | None = None
    provider_document_name: str | None = None
    provider_operation_name: str | None = None
    input_byte_count: int | None = None
    metadata: Any = field(default_factory=dict)
    state: IndexState = IndexState.READY
    retry_count: int = 0
    last_error_category: str | None = None
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "store_id", _require_text(self.store_id, "store id", 36))
        object.__setattr__(self, "provider", _require_text(self.provider, "provider", 50))
        if self.provider_document_id is not None:
            object.__setattr__(
                self,
                "provider_document_id",
                _require_text(self.provider_document_id, "provider document id", 500),
            )
        object.__setattr__(
            self,
            "source_revision_id",
            _require_text(self.source_revision_id, "source revision id", 200),
        )
        for field_name in (
            "provider_file_name",
            "provider_document_name",
            "provider_operation_name",
            "last_error_category",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _require_text(value, field_name, 500))
        if self.provider_document_name is None and self.provider_document_id is not None:
            object.__setattr__(self, "provider_document_name", self.provider_document_id)
        if self.input_byte_count is not None and self.input_byte_count < 0:
            raise ValueError("input byte count cannot be negative")
        if self.retry_count < 0:
            raise ValueError("retry count cannot be negative")
        object.__setattr__(self, "metadata", _validated_json(self.metadata))
        _require_text(self.id, "document id", 36)

    @property
    def provider_name(self) -> str | None:
        return self.provider_document_id

    @property
    def metadata_json(self) -> str:
        return json.dumps(self.metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class IndexJob:
    store_id: str
    source_revision_id: str
    provider_document_id: str | None = None
    provider_operation_name: str | None = None
    state: IndexState = IndexState.NOT_INDEXED
    retry_count: int = 0
    last_error_category: str | None = None
    last_error_message: str | None = None
    next_attempt_at: str | None = None
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "store_id", _require_text(self.store_id, "store id", 36))
        object.__setattr__(
            self,
            "source_revision_id",
            _require_text(self.source_revision_id, "source revision id", 200),
        )
        for field_name in (
            "provider_document_id",
            "provider_operation_name",
            "last_error_category",
            "last_error_message",
            "next_attempt_at",
            "lease_owner",
            "lease_expires_at",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _require_text(value, field_name, 500))
        if self.retry_count < 0:
            raise ValueError("retry count cannot be negative")
        _require_text(self.id, "job id", 36)


class ProviderStoreModel(Base):
    __tablename__ = "provider_stores"
    __table_args__ = (
        UniqueConstraint("store_key", "generation", name="uq_provider_stores_key_generation"),
        UniqueConstraint(
            "provider",
            "provider_store_name",
            name="uq_provider_stores_provider_name",
        ),
        Index(
            "uq_provider_stores_current_key",
            "store_key",
            unique=True,
            sqlite_where=text("is_current = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    store_key: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_store_name: Mapped[str] = mapped_column(String(500), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(200), nullable=False)
    authority_namespace: Mapped[str] = mapped_column(String(100), nullable=False)
    course_id: Mapped[str] = mapped_column(String(100), nullable=False)
    exam_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class ProviderDocumentModel(Base):
    __tablename__ = "provider_documents"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_document_id",
            name="uq_provider_documents_provider_name",
        ),
        UniqueConstraint(
            "store_id",
            "source_revision_id",
            name="uq_provider_documents_store_revision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("provider_stores.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_document_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_revision_id: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_file_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    provider_document_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    provider_operation_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    input_byte_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_error_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class IndexJobModel(Base):
    __tablename__ = "index_jobs"
    __table_args__ = (
        UniqueConstraint("store_id", "source_revision_id", name="uq_index_jobs_store_revision"),
        Index("ix_index_jobs_state_attempt", "state", "next_attempt_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("provider_stores.id"), nullable=False)
    source_revision_id: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_document_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    provider_operation_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_error_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


__all__ = [
    "ALLOWED_TRANSITIONS",
    "IndexJob",
    "IndexJobModel",
    "IndexState",
    "ProviderDocument",
    "ProviderDocumentModel",
    "ProviderStore",
    "ProviderStoreModel",
    "StoreKey",
    "validate_transition",
]
