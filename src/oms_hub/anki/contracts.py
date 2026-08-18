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

from oms_hub.anki.domain import (
    AgentCommandType,
    CreateCurationJob,
    PipelineContractVersion,
    ResolvedClassifierExecution,
    ResolvedModelConfiguration,
    ResolvedStageModel,
)
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
    provider: Literal["openai", "gemini", "anthropic", "openrouter"]
    model: Annotated[str, Field(max_length=200)] | None = None
    pipeline_contract_version: Literal[
        "retrieval_v4", "card_centric_v1", "card_centric_v2", "card_centric_v3"
    ] = "retrieval_v4"
    resolved_model_config: dict[str, Any] | None = None
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
    policy_sha256: Sha256 | None = None
    rate_table_document: dict[str, Any] | None = None
    offline_replay_only: bool = False

    @field_validator("deck_allowlist", mode="before")
    @classmethod
    def normalize_deck_values(cls, values: Any) -> tuple[str, ...]:
        if not isinstance(values, (list, tuple)):
            raise TypeError("scope must be a list")
        ordered: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = str(raw).strip()
            key = value.casefold()
            if value and key not in seen:
                seen.add(key)
                ordered.append(value)
        return tuple(ordered)

    @field_validator("tag_allowlist", mode="before")
    @classmethod
    def normalize_tag_scope_values(cls, values: Any) -> tuple[str, ...]:
        if not isinstance(values, (list, tuple)):
            raise TypeError("scope must be a list")
        normalized = {str(value).strip() for value in values if str(value).strip()}
        return tuple(sorted(normalized))

    @field_validator(
        "target_deck",
        "lcl_prompt_version",
        "judgment_rubric_version",
        "gap_prompt_version",
        mode="before",
    )
    @classmethod
    def strip_required_text(cls, value: Any) -> str:
        return str(value).strip()

    @field_validator("model", mode="before")
    @classmethod
    def strip_optional_model(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            raise ValueError("model cannot be blank")
        return text

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
        if (self.summary_outline_id is None) != (self.summary_outline_sha256 is None):
            raise ValueError("summary outline ID and hash must be supplied together")
        if self.policy_sha256 is not None and self.pipeline_contract_version != "card_centric_v3":
            raise ValueError("policy pin is supported only by card-centric v3")
        if (
            self.rate_table_document is not None or self.offline_replay_only
        ) and self.pipeline_contract_version != "card_centric_v3":
            raise ValueError("v3 execution pins are supported only by card-centric v3")
        v3_routes = {"scope_r3", "cheap_classify_r7", "thorough_classify_r7", "generation_r9"}
        if (
            self.pipeline_contract_version != "card_centric_v3"
            and self.resolved_model_config is not None
            and v3_routes & set(self.resolved_model_config)
        ):
            raise ValueError("v3 model-tier routes are supported only by card-centric v3")
        return self

    def to_domain(self, *, model: str) -> CreateCurationJob:
        version = PipelineContractVersion(self.pipeline_contract_version)
        resolved = _resolved_model_config(
            self.resolved_model_config,
            self.provider,
            model,
            version=version,
        )
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
            model=model,
            pipeline_contract_version=version,
            resolved_model_config=resolved,
            source_revision_hashes=dict(self.source_revision_hashes),
            semantic_generation=self.semantic_generation,
            companion_generation=self.companion_generation,
            summary_outline_id=self.summary_outline_id,
            summary_outline_sha256=self.summary_outline_sha256,
            policy_sha256=self.policy_sha256,
            rate_table_document=self.rate_table_document,
            offline_replay_only=self.offline_replay_only,
        )


def _resolved_model_config(
    value: dict[str, Any] | None,
    provider: str,
    model: str,
    *,
    version: PipelineContractVersion = PipelineContractVersion.RETRIEVAL_V4,
) -> ResolvedModelConfiguration:
    if value is None:
        if version is PipelineContractVersion.CARD_CENTRIC_V1:
            return ResolvedModelConfiguration.card_centric_default(provider, model)
        if version is PipelineContractVersion.CARD_CENTRIC_V2:
            return ResolvedModelConfiguration.card_centric_v2_default(provider, model)
        return ResolvedModelConfiguration.legacy(provider, model)
    try:

        def stage(name: str) -> ResolvedStageModel:
            raw = value[name]
            if not isinstance(raw, dict):
                raise ValueError("stage configuration must be an object")
            return ResolvedStageModel(
                provider=str(raw["provider"]),
                model=str(raw["model"]),
                thinking_mode=str(raw.get("thinking_mode", "default")),
                fixture_validation_signature=(
                    str(raw["fixture_validation_signature"])
                    if raw.get("fixture_validation_signature") is not None
                    else None
                ),
            )

        def optional_stage(name: str) -> ResolvedStageModel | None:
            return stage(name) if value.get(name) is not None else None

        classifier_execution: ResolvedClassifierExecution | None
        if "classifier_execution" in value:
            execution_value = value["classifier_execution"]
            if not isinstance(execution_value, dict):
                raise ValueError("classifier execution configuration must be an object")
            classifier_execution = ResolvedClassifierExecution.from_document(execution_value)
        else:
            classifier_execution = (
                ResolvedClassifierExecution()
                if version is PipelineContractVersion.CARD_CENTRIC_V2
                else None
            )

        resolved = ResolvedModelConfiguration(
            profile=str(value["profile"]),
            ledger_s2=stage("ledger_s2"),
            classify_s4=stage("classify_s4"),
            residual_s6=stage("residual_s6"),
            gap_fill_s7=stage("gap_fill_s7"),
            residual_unlocked=bool(value.get("residual_unlocked", False)),
            fast_classify_s4b=(
                stage("fast_classify_s4b") if value.get("fast_classify_s4b") is not None else None
            ),
            classifier_execution=classifier_execution,
            scope_r3=optional_stage("scope_r3"),
            cheap_classify_r7=optional_stage("cheap_classify_r7"),
            thorough_classify_r7=optional_stage("thorough_classify_r7"),
            generation_r9=optional_stage("generation_r9"),
        )
        if version in {
            PipelineContractVersion.CARD_CENTRIC_V1,
            PipelineContractVersion.CARD_CENTRIC_V2,
        } and (
            resolved.classify_s4.thinking_mode != "disabled"
            or resolved.residual_s6.thinking_mode != "disabled"
        ):
            raise ValueError("card-centric S4/S6 thinking must be disabled")
        if version is PipelineContractVersion.CARD_CENTRIC_V2:
            resolved.require_card_centric_v2_fast_classifier()
        return resolved
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"resolved model configuration is invalid: {exc}") from exc


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
    supported_envelope_contract_versions: tuple[Literal[1, 2], ...] = (1,)


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


class _ActionEnvelopeBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
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
    def validate_operation_order(self) -> "_ActionEnvelopeBase":
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
            raise ValueError("envelope requires exactly one sync and one verify operation")
        touched_ids = set(self.touched_note_hashes)
        if self.expected_tag_hashes and set(self.expected_tag_hashes) != touched_ids:
            raise ValueError("expected tag hashes must match touched note IDs")
        if self.expected_note_tags and set(self.expected_note_tags) != touched_ids:
            raise ValueError("expected note tags must match touched note IDs")
        operation_ids = [operation.operation_id for operation in self.operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("envelope operation IDs must be unique")
        return self


class ActionEnvelopeV1(_ActionEnvelopeBase):
    contract_version: Literal[1] = 1


# Public compatibility name: this schema and its canonical bytes are immutable.
ActionEnvelope = ActionEnvelopeV1


class ActionEnvelopeV2(_ActionEnvelopeBase):
    contract_version: Literal[2] = 2
    job_id: UUID
    pipeline_contract_version: Literal[
        "card_centric_v1", "card_centric_v2", "card_centric_v3"
    ] = "card_centric_v1"
    model_config_sha256: Sha256
    # The digest is an integrity check; the canonical document makes the frozen
    # plan independently auditable and reproducible.
    # Empty is accepted only to parse historical pre-document V2 plans; all new
    # card-centric plans populate this and repository persistence rejects a
    # mismatched non-empty document.
    resolved_model_config: dict[str, Any] = Field(default_factory=dict)
    reconciliation_contract_version: Annotated[str, Field(min_length=1, max_length=100)]
    review_revision: Annotated[int, Field(ge=0)]
    overflow_acknowledgement_provenance: dict[str, Any]
    # V3 identity is additive.  Legacy documents omit these fields exactly.
    policy_sha256: Sha256 | None = None
    scope_sha256: Sha256 | None = None
    r11_artifact_sha256: Sha256 | None = None
    r11_snapshot_sha256: Sha256 | None = None
    rate_table_sha256: Sha256 | None = None
    cost_ledger_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def _v3_identity(self) -> "ActionEnvelopeV2":
        values = (
            self.policy_sha256,
            self.scope_sha256,
            self.r11_artifact_sha256,
            self.r11_snapshot_sha256,
            self.rate_table_sha256,
            self.cost_ledger_sha256,
        )
        if self.pipeline_contract_version == "card_centric_v3":
            if any(value is None for value in values):
                raise ValueError(
                    "v3 envelopes require policy, scope, R11, rate-table, and ledger identity"
                )
        elif any(value is not None for value in values):
            raise ValueError("v3 envelope identity is not valid for legacy contracts")
        return self

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        value = super().model_dump(**kwargs)
        if self.pipeline_contract_version != "card_centric_v3":
            for key in (
                "policy_sha256",
                "scope_sha256",
                "r11_artifact_sha256",
                "r11_snapshot_sha256",
                "rate_table_sha256",
                "cost_ledger_sha256",
            ):
                value.pop(key, None)
        return value


type ActionEnvelopeDocument = ActionEnvelopeV1 | ActionEnvelopeV2


def parse_action_envelope(value: str | bytes | dict[str, Any]) -> ActionEnvelopeDocument:
    if isinstance(value, (str, bytes)):
        raw = json.loads(value)
    else:
        raw = value
    return (
        ActionEnvelopeV2.model_validate(raw)
        if raw.get("contract_version") == 2
        else ActionEnvelopeV1.model_validate(raw)
    )


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
