import hashlib
import json
import re
from datetime import datetime
from pathlib import PurePath
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from oms_hub.anki.domain import AgentCommandType

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def canonical_payload_sha256(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude={"payload_sha256"})
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal[1] = 1


class AgentHeartbeat(ContractModel):
    agent_id: Annotated[str, Field(min_length=1, max_length=100)]
    agent_version: Annotated[str, Field(min_length=1, max_length=100)]
    anki_version: Annotated[str, Field(min_length=1, max_length=100)]
    ankiconnect_version: Annotated[int, Field(ge=6)]
    active_snapshot_id: Annotated[str, Field(max_length=200)] | None
    health: Literal["ok", "degraded", "error"]
    observed_at: datetime


class AgentCommand(ContractModel):
    command_id: UUID
    command_type: AgentCommandType
    payload: dict[str, Any]
    payload_sha256: Sha256
    created_at: datetime


class SnapshotManifest(ContractModel):
    snapshot_id: Annotated[str, Field(min_length=1, max_length=200)]
    source_deck: Literal["Anking Step Deck"]
    note_count: Annotated[int, Field(ge=0)]
    id_set_sha256: Sha256
    content_sha256: Sha256
    export_version: Annotated[str, Field(min_length=1, max_length=50)]
    producer_version: Annotated[str, Field(min_length=1, max_length=100)]
    ankiconnect_version: Annotated[int, Field(ge=6)]
    exported_at: datetime
    payload_sha256: Sha256


class SnapshotNote(ContractModel):
    note_id: Annotated[int, Field(gt=0)]
    model_name: Annotated[str, Field(min_length=1, max_length=300)]
    fields: dict[str, str]
    tags: tuple[str, ...]
    card_ids: tuple[Annotated[int, Field(gt=0)], ...]
    media: tuple[str, ...]
    content_sha256: Sha256

    @field_validator("media")
    @classmethod
    def validate_media_filenames(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _validated_filename(value)
        return values


class SnapshotDelta(ContractModel):
    manifest: SnapshotManifest
    upserts: tuple[SnapshotNote, ...]
    deleted_note_ids: tuple[Annotated[int, Field(gt=0)], ...]
    payload_sha256: Sha256


class MediaFetchRequest(ContractModel):
    command_id: UUID
    filenames: tuple[str, ...]
    max_bytes: Annotated[int, Field(ge=1, le=100 * 1024 * 1024)]

    @field_validator("filenames")
    @classmethod
    def validate_filenames(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("filenames cannot be empty")
        for value in values:
            _validated_filename(value)
        return values


class MediaUpload(ContractModel):
    command_id: UUID
    filename: str
    mime_type: Literal["image/png", "image/jpeg", "image/webp", "image/gif"]
    content_base64: Annotated[str, Field(min_length=1)]
    byte_count: Annotated[int, Field(ge=1, le=100 * 1024 * 1024)]
    sha256: Sha256

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        return _validated_filename(value)


class EnvelopeOperation(ContractModel):
    operation_id: UUID
    content_sha256: Sha256


class StoreMediaOperation(EnvelopeOperation):
    operation_type: Literal["store_media"] = "store_media"
    filename: str
    content_base64: Annotated[str, Field(min_length=1)]
    sha256: Sha256

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        return _validated_filename(value)


class AddTagsOperation(EnvelopeOperation):
    operation_type: Literal["add_tags"] = "add_tags"
    note_ids: tuple[Annotated[int, Field(gt=0)], ...]
    tag: Annotated[str, Field(min_length=1, max_length=500)]

    @field_validator("note_ids")
    @classmethod
    def validate_note_ids(
        cls,
        values: tuple[int, ...],
    ) -> tuple[int, ...]:
        if not values:
            raise ValueError("note_ids cannot be empty")
        if len(values) > 1_000:
            raise ValueError("add_tags cannot contain more than 1000 note IDs")
        return values


class AddNotesOperation(EnvelopeOperation):
    operation_type: Literal["add_notes"] = "add_notes"
    notes: tuple[dict[str, Any], ...]

    @field_validator("notes")
    @classmethod
    def validate_notes(
        cls,
        values: tuple[dict[str, Any], ...],
    ) -> tuple[dict[str, Any], ...]:
        if not values:
            raise ValueError("notes cannot be empty")
        return values


class SyncOperation(EnvelopeOperation):
    operation_type: Literal["sync"] = "sync"


class VerifyOperation(EnvelopeOperation):
    operation_type: Literal["verify"] = "verify"
    note_ids: tuple[Annotated[int, Field(gt=0)], ...]


Operation = Annotated[
    StoreMediaOperation
    | AddTagsOperation
    | AddNotesOperation
    | SyncOperation
    | VerifyOperation,
    Field(discriminator="operation_type"),
]


class ActionEnvelope(ContractModel):
    envelope_id: UUID
    snapshot_id: Annotated[str, Field(min_length=1, max_length=200)]
    target_deck: Annotated[str, Field(min_length=1, max_length=1_000)]
    target_tag: Annotated[str, Field(min_length=1, max_length=1_000)]
    touched_note_hashes: dict[Annotated[int, Field(gt=0)], Sha256]
    operations: tuple[Operation, ...]
    payload_sha256: Sha256

    @model_validator(mode="after")
    def validate_operation_order(self) -> "ActionEnvelope":
        if not self.operations:
            raise ValueError("operations cannot be empty")
        phases = {
            "store_media": 0,
            "add_tags": 1,
            "add_notes": 2,
            "sync": 3,
            "verify": 4,
        }
        observed = [phases[operation.operation_type] for operation in self.operations]
        if observed != sorted(observed):
            raise ValueError("envelope operations are out of order")
        if observed.count(3) != 1 or observed.count(4) != 1:
            raise ValueError("envelope requires exactly one sync and one verify operation")
        return self


class OperationReceipt(ContractModel):
    operation_id: UUID
    status: Literal["complete", "retryable", "failed"]
    result: dict[str, Any]
    error: str | None = None


class EnvelopeReceipt(ContractModel):
    envelope_id: UUID
    executor_id: Literal["nuc-local"]
    operations: tuple[OperationReceipt, ...]
    sync_status: Literal["complete", "retryable", "failed"]
    verified: bool
    created_note_ids: tuple[Annotated[int, Field(gt=0)], ...]
    media_filenames: tuple[str, ...]
    safe_error: str | None = None
    payload_sha256: Sha256

    @field_validator("media_filenames")
    @classmethod
    def validate_media_filenames(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _validated_filename(value)
        return values


def _validated_filename(value: str) -> str:
    if (
        not value
        or len(value) > 255
        or PurePath(value).name != value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]*", value)
    ):
        raise ValueError("filename must be a safe basename")
    return value
