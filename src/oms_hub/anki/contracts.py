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

from oms_hub.anki.domain import AgentCommandType, CreateCurationJob
from oms_hub.anki.tag_policy import normalize_tag

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


class CreateCurationJobRequest(ContractModel):
    lecture_id: Annotated[int, Field(gt=0)]
    block_id: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    source_revision_ids: tuple[Annotated[int, Field(gt=0)], ...] = Field(
        min_length=1,
        max_length=20,
    )
    deck_allowlist: tuple[
        Annotated[str, Field(min_length=1, max_length=1_000)],
        ...,
    ] = Field(min_length=1, max_length=100)
    tag_allowlist: tuple[Annotated[str, Field(min_length=1, max_length=1_000)], ...]
    target_deck: Annotated[str, Field(min_length=1, max_length=1_000)]
    target_tag: Annotated[str, Field(min_length=1, max_length=1_000)]
    index_snapshot_id: Annotated[str, Field(min_length=1, max_length=200)]
    instruction_text: Annotated[str, Field(max_length=20_000)] = ""
    lcl_prompt_version: Annotated[str, Field(min_length=1, max_length=100)]
    judgment_rubric_version: Annotated[str, Field(min_length=1, max_length=100)]
    gap_prompt_version: Annotated[str, Field(min_length=1, max_length=100)]
    provider: Literal["openai", "gemini", "anthropic"]
    model: Annotated[str, Field(min_length=1, max_length=200)]
    source_revision_hashes: dict[
        Annotated[int, Field(gt=0)],
        Sha256,
    ] = Field(default_factory=dict)
    summary_outline_id: Annotated[int, Field(gt=0)] | None = None
    summary_outline_sha256: Sha256 | None = None
    semantic_generation: (
        Annotated[
            str,
            Field(min_length=1, max_length=200),
        ]
        | None
    ) = None
    companion_generation: (
        Annotated[
            str,
            Field(min_length=1, max_length=200),
        ]
        | None
    ) = None

    @field_validator("deck_allowlist", "tag_allowlist", mode="before")
    @classmethod
    def normalize_scope_values(cls, values: Any) -> tuple[str, ...]:
        if not isinstance(values, (list, tuple)):
            raise TypeError("scope must be a list")
        normalized = {str(value).strip() for value in values if str(value).strip()}
        return tuple(sorted(normalized))

    @field_validator(
        "target_deck",
        "model",
        "lcl_prompt_version",
        "judgment_rubric_version",
        "gap_prompt_version",
        mode="before",
    )
    @classmethod
    def strip_required_text(cls, value: Any) -> str:
        return str(value).strip()

    @field_validator("target_tag", mode="before")
    @classmethod
    def safe_target_tag(cls, value: Any) -> str:
        return normalize_tag(str(value))

    @field_validator("source_revision_ids")
    @classmethod
    def unique_source_revisions(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if len(values) != len(set(values)):
            raise ValueError("source revision IDs must be unique")
        return values

    @model_validator(mode="after")
    def validate_source_hashes(self) -> "CreateCurationJobRequest":
        if self.source_revision_hashes and set(self.source_revision_hashes) != set(
            self.source_revision_ids
        ):
            raise ValueError("source revision hashes must match selected revisions")
        if (self.summary_outline_id is None) != (
            self.summary_outline_sha256 is None
        ):
            raise ValueError("summary outline ID and hash must be supplied together")
        return self

    def to_domain(self) -> CreateCurationJob:
        return CreateCurationJob(
            lecture_id=self.lecture_id,
            block_id=self.block_id,
            source_revision_ids=self.source_revision_ids,
            deck_allowlist=self.deck_allowlist,
            tag_allowlist=self.tag_allowlist,
            instruction_text=self.instruction_text,
            target_deck=self.target_deck,
            target_tag=self.target_tag,
            index_snapshot_id=self.index_snapshot_id,
            lcl_prompt_version=self.lcl_prompt_version,
            judgment_rubric_version=self.judgment_rubric_version,
            gap_prompt_version=self.gap_prompt_version,
            provider=self.provider,
            model=self.model,
            source_revision_hashes=dict(self.source_revision_hashes),
            semantic_generation=self.semantic_generation,
            companion_generation=self.companion_generation,
            summary_outline_id=self.summary_outline_id,
            summary_outline_sha256=self.summary_outline_sha256,
        )


class TagPatchContract(ContractModel):
    note_id: Annotated[int, Field(gt=0)]
    before: tuple[Annotated[str, Field(min_length=1, max_length=500)], ...]
    after: tuple[Annotated[str, Field(min_length=1, max_length=500)], ...]
    add_tags: tuple[Annotated[str, Field(min_length=1, max_length=500)], ...]
    remove_tags: tuple[Annotated[str, Field(min_length=1, max_length=500)], ...]
    expected_tag_hash: Sha256
    tag_policy_version: Annotated[str, Field(min_length=1, max_length=100)]

    @field_validator(
        "before",
        "after",
        "add_tags",
        "remove_tags",
        mode="before",
    )
    @classmethod
    def normalize_tags(cls, values: Any) -> tuple[str, ...]:
        if not isinstance(values, (list, tuple)):
            raise TypeError("tag values must be a list")
        return tuple(normalize_tag(str(value)) for value in values)

    @model_validator(mode="after")
    def validate_exact_diff(self) -> "TagPatchContract":
        sequences = (self.before, self.after, self.add_tags, self.remove_tags)
        if any(len(values) != len(set(values)) for values in sequences):
            raise ValueError("tag lists cannot contain duplicates")
        add = set(self.add_tags)
        remove = set(self.remove_tags)
        if add & remove:
            raise ValueError("the same tag cannot be added and removed")
        expected = (set(self.before) - remove) | add
        if set(self.after) != expected:
            raise ValueError("tag patch does not match its before/after state")
        return self


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
    agent_version: Annotated[str, Field(min_length=1, max_length=100)]
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


class RemoveTagsOperation(EnvelopeOperation):
    operation_type: Literal["remove_tags"] = "remove_tags"
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
            raise ValueError("remove_tags cannot contain more than 1000 note IDs")
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
    | RemoveTagsOperation
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
    expected_tag_hashes: dict[
        Annotated[int, Field(gt=0)],
        Sha256,
    ] = Field(default_factory=dict)
    expected_note_tags: dict[
        Annotated[int, Field(gt=0)],
        tuple[Annotated[str, Field(min_length=1, max_length=500)], ...],
    ] = Field(default_factory=dict)
    operations: tuple[Operation, ...]
    payload_sha256: Sha256

    @model_validator(mode="after")
    def validate_operation_order(self) -> "ActionEnvelope":
        if not self.operations:
            raise ValueError("operations cannot be empty")
        phases = {
            "store_media": 0,
            "remove_tags": 1,
            "add_tags": 2,
            "add_notes": 3,
            "sync": 4,
            "verify": 5,
        }
        observed = [phases[operation.operation_type] for operation in self.operations]
        if observed != sorted(observed):
            raise ValueError("envelope operations are out of order")
        if observed.count(4) != 1 or observed.count(5) != 1:
            raise ValueError(
                "envelope requires exactly one sync and one verify operation"
            )
        touched_ids = set(self.touched_note_hashes)
        if self.expected_tag_hashes and set(self.expected_tag_hashes) != touched_ids:
            raise ValueError("expected tag hashes must match touched note IDs")
        if self.expected_note_tags and set(self.expected_note_tags) != touched_ids:
            raise ValueError("expected note tags must match touched note IDs")
        operation_ids = [operation.operation_id for operation in self.operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("envelope operation IDs must be unique")
        return self


class OperationReceipt(ContractModel):
    operation_id: UUID
    status: Literal["complete", "retryable", "failed"]
    result: dict[str, Any]
    error: str | None = None


class EnvelopeReceipt(ContractModel):
    envelope_id: UUID
    agent_id: Annotated[str, Field(min_length=1, max_length=100)]
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
