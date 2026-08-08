"""Decision-locked shared contracts for card-centric v2 correction lanes.

S0 freezes these interfaces; P1--P4 own their integration into stage behavior.
The types are additive and do not alter persisted v1 or existing v2 artifacts.
"""

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from oms_hub.anki.domain import CurationStage

WARNING_FLOOR = 60
ORDINARY_TARGET = 65
SOFT_CAP = 70

QUALITY_FIRST_MODEL_INSTRUCTION = (
    "Optimize for the smallest set of the best-supported, highest-yield, "
    "nonredundant cards. Card counts are soft targets, not quotas. Do not invent "
    "facts, split one fact into unnecessary cards, preserve a weak card, or label "
    "a card eligible merely to reach a count. Prefer fewer excellent, grounded, "
    "nonredundant cards over more marginal cards."
)


class FrozenCorrectionContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    correction_contract_version: Literal[1] = 1


class CanonicalJsonObject(BaseModel):
    """Deeply immutable JSON object represented by its canonical bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    canonical_json: str = Field(min_length=2)

    @field_validator("canonical_json")
    @classmethod
    def validate_canonical_object(cls, value: str) -> str:
        try:
            parsed = json.loads(value, parse_constant=_reject_json_constant)
        except (TypeError, ValueError) as exc:
            raise ValueError("value must be a finite JSON object") from exc
        if not isinstance(parsed, dict):
            raise ValueError("value must be a JSON object")
        canonical = _canonical_json(parsed)
        if value != canonical:
            raise ValueError("JSON object must use canonical serialization")
        return value

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> Self:
        return cls(canonical_json=_canonical_json(dict(value)))

    def as_dict(self) -> dict[str, Any]:
        value = json.loads(self.canonical_json, parse_constant=_reject_json_constant)
        if not isinstance(value, dict):  # pragma: no cover - validated at construction
            raise AssertionError("canonical JSON object changed after validation")
        return value


class EvidenceQuality(StrEnum):
    PRIMARY_SOURCE = "primary_source"
    SUMMARY_GROUNDED = "summary_grounded"
    FAST_PASS = "fast_pass"


class GeneratedResolutionKind(StrEnum):
    GENERATED = "generated"
    UNRESOLVED = "unresolved"
    DUPLICATE_OF_EXISTING = "duplicate_of_existing"


class SelectionTier(StrEnum):
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"
    T4 = "T4"
    T5 = "T5"
    T6 = "T6"


class MarginalValueReason(StrEnum):
    ONLY_VALID_REQUIRED_FACT = "only_valid_required_fact"
    UNIQUE_EMPHASIZED_DISTINCTION = "unique_emphasized_distinction"
    VALIDATED_NECESSARY_SPLIT = "validated_necessary_split"


class DeckSizingPolicy(FrozenCorrectionContract):
    warning_floor: Literal[60] = 60
    ordinary_target: Literal[65] = 65
    soft_cap: Literal[70] = 70
    counts_are_quotas: Literal[False] = False
    padding_allowed: Literal[False] = False


class FactForbiddenClozeTargets(FrozenCorrectionContract):
    fact_id: str = Field(min_length=1)
    targets: tuple[str, ...]

    @model_validator(mode="after")
    def clean_targets(self) -> "FactForbiddenClozeTargets":
        fact_id = self.fact_id.strip()
        targets = tuple(target.strip() for target in self.targets)
        if not fact_id or any(not target for target in targets):
            raise ValueError("fact IDs and forbidden cloze targets must be nonblank")
        if len({target.casefold() for target in targets}) != len(targets):
            raise ValueError("forbidden cloze targets must be unique within each fact")
        object.__setattr__(self, "fact_id", fact_id)
        object.__setattr__(self, "targets", targets)
        return self


class FactForbiddenClozeMap(FrozenCorrectionContract):
    """V2 cloze exclusions remain keyed by their stable fact identity."""

    facts: tuple[FactForbiddenClozeTargets, ...]

    @model_validator(mode="after")
    def unique_fact_ids(self) -> "FactForbiddenClozeMap":
        ordered = tuple(sorted(self.facts, key=lambda item: item.fact_id))
        ids = [item.fact_id for item in ordered]
        if len(ids) != len(set(ids)):
            raise ValueError("forbidden cloze fact IDs must be unique")
        object.__setattr__(self, "facts", ordered)
        return self

    @property
    def targets_by_fact_id(self) -> dict[str, tuple[str, ...]]:
        return {item.fact_id: item.targets for item in self.facts}


class GeneratedCardIdentity(FrozenCorrectionContract):
    card_id: str = Field(min_length=1)
    fact_id: str = Field(min_length=1)
    split: bool = False
    split_index: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_split_index(self) -> "GeneratedCardIdentity":
        if self.split != (self.split_index is not None):
            raise ValueError("split cards require split_index; unsplit cards must omit it")
        return self


class DuplicateIdentity(FrozenCorrectionContract):
    existing_note_id: int | None = Field(default=None, gt=0)
    generated_card_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def exactly_one_identity(self) -> "DuplicateIdentity":
        if (self.existing_note_id is None) == (self.generated_card_id is None):
            raise ValueError("duplicate identity must name exactly one existing or generated card")
        return self


class GeneratedFactResolution(FrozenCorrectionContract):
    fact_id: str = Field(min_length=1)
    kind: GeneratedResolutionKind
    generated_card_ids: tuple[str, ...] = ()
    duplicate_of: DuplicateIdentity | None = None
    unresolved_reason: str | None = None

    @model_validator(mode="after")
    def validate_terminal_semantics(self) -> "GeneratedFactResolution":
        ids = tuple(card_id.strip() for card_id in self.generated_card_ids)
        if any(not card_id for card_id in ids) or len(ids) != len(set(ids)):
            raise ValueError("generated card IDs must be nonblank and unique")
        object.__setattr__(self, "generated_card_ids", ids)
        if self.kind is GeneratedResolutionKind.GENERATED:
            valid = bool(ids) and self.duplicate_of is None and self.unresolved_reason is None
        elif self.kind is GeneratedResolutionKind.DUPLICATE_OF_EXISTING:
            valid = not ids and self.duplicate_of is not None and self.unresolved_reason is None
        else:
            valid = (
                not ids
                and self.duplicate_of is None
                and self.unresolved_reason is not None
                and bool(self.unresolved_reason.strip())
            )
        if not valid:
            raise ValueError("generated fact resolution fields conflict with terminal kind")
        return self


class GeneratedOutputSet(FrozenCorrectionContract):
    """Canonical generation is conserved independently of selected deck IDs."""

    required_fact_ids: tuple[str, ...]
    canonical_all_generated: tuple[GeneratedCardIdentity, ...]
    selected_generated_card_ids: tuple[str, ...]
    resolutions: tuple[GeneratedFactResolution, ...]

    @model_validator(mode="after")
    def validate_conservation_and_splits(self) -> "GeneratedOutputSet":
        required_fact_ids = tuple(fact_id.strip() for fact_id in self.required_fact_ids)
        if any(not fact_id for fact_id in required_fact_ids) or len(required_fact_ids) != len(
            set(required_fact_ids)
        ):
            raise ValueError("required fact IDs must be nonblank and unique")
        object.__setattr__(self, "required_fact_ids", required_fact_ids)
        cards_by_id = {card.card_id: card for card in self.canonical_all_generated}
        if len(cards_by_id) != len(self.canonical_all_generated):
            raise ValueError("canonical generated card IDs must be unique")
        selected = set(self.selected_generated_card_ids)
        if len(selected) != len(self.selected_generated_card_ids) or selected - set(cards_by_id):
            raise ValueError("selected generated IDs must be a unique canonical subset")
        fact_ids = [resolution.fact_id for resolution in self.resolutions]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("each required fact must have one terminal resolution")
        if set(fact_ids) != set(required_fact_ids):
            raise ValueError("terminal resolutions must exactly cover required facts")
        generated_ids = {
            card_id
            for resolution in self.resolutions
            if resolution.kind is GeneratedResolutionKind.GENERATED
            for card_id in resolution.generated_card_ids
        }
        if generated_ids != set(cards_by_id):
            raise ValueError("generated resolutions must conserve every canonical generated card")
        for resolution in self.resolutions:
            if resolution.kind is not GeneratedResolutionKind.GENERATED:
                continue
            if any(
                cards_by_id[card_id].fact_id != resolution.fact_id
                for card_id in resolution.generated_card_ids
            ):
                raise ValueError("generated cards must remain linked to their resolved fact")
        cards_by_fact: dict[str, list[GeneratedCardIdentity]] = {}
        for card in self.canonical_all_generated:
            cards_by_fact.setdefault(card.fact_id, []).append(card)
        for cards in cards_by_fact.values():
            if len(cards) == 1 and not cards[0].split:
                continue
            indices = sorted(card.split_index for card in cards if card.split_index is not None)
            if len(indices) != len(cards) or indices != list(range(1, len(cards) + 1)):
                raise ValueError("split_index values must be sequential from one per fact")
        return self


class SelectionMetadata(FrozenCorrectionContract):
    identity: str = Field(min_length=1)
    selected_position: int = Field(gt=0)
    tier: SelectionTier
    evidence_quality: EvidenceQuality
    mandatory: bool = False
    marginal_value_reason: MarginalValueReason | None = None
    overflow_reason: str | None = None
    manual_acknowledgement_required: bool = False

    @model_validator(mode="after")
    def validate_soft_cap_reasons(self) -> "SelectionMetadata":
        if 66 <= self.selected_position <= SOFT_CAP and self.marginal_value_reason is None:
            raise ValueError("cards 66-70 require a nonredundant marginal-value reason")
        if self.selected_position > SOFT_CAP and (
            not self.mandatory
            or not _nonblank(self.overflow_reason)
            or not self.manual_acknowledgement_required
        ):
            raise ValueError(
                "cards above 70 require mandatory status, an overflow reason, "
                "and signed manual acknowledgement before issuance"
            )
        return self


class PromptSnapshotIdentity(FrozenCorrectionContract):
    prompt_id: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    content: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_content_hash(self) -> "PromptSnapshotIdentity":
        if hashlib.sha256(self.content.encode("utf-8")).hexdigest() != self.content_sha256:
            raise ValueError("prompt content hash does not match exact prompt contents")
        return self


class ResolvedStageModelIdentity(FrozenCorrectionContract):
    stage: CurationStage
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    prompts: tuple[PromptSnapshotIdentity, ...] = Field(min_length=1)
    generation_parameters: CanonicalJsonObject
    identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_identity_hash(self) -> "ResolvedStageModelIdentity":
        payload = {
            "stage": self.stage.value,
            "provider": self.provider,
            "model": self.model,
            "prompts": [prompt.model_dump(mode="json") for prompt in self.prompts],
            "generation_parameters": self.generation_parameters.as_dict(),
        }
        if _sha(payload) != self.identity_sha256:
            raise ValueError("resolved model identity hash does not match its inputs")
        return self


class PinnedLectureMetadata(FrozenCorrectionContract):
    lecture_id: int = Field(gt=0)
    title: str = Field(min_length=1)
    metadata: CanonicalJsonObject
    metadata_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_metadata_hash(self) -> "PinnedLectureMetadata":
        payload = {
            "lecture_id": self.lecture_id,
            "title": self.title,
            "metadata": self.metadata.as_dict(),
        }
        if _sha(payload) != self.metadata_sha256:
            raise ValueError("pinned lecture metadata hash does not match its contents")
        return self


class PinnedSemanticGeneration(FrozenCorrectionContract):
    generation: UUID
    model: str = Field(min_length=1)
    dimensions: int = Field(gt=0)


class A11HistoryEntry(FrozenCorrectionContract):
    job_id: UUID
    review_revision: int = Field(gt=0)
    yes_rate: float = Field(ge=0, le=1)
    reviewed_at: datetime


class A11HistorySnapshot(FrozenCorrectionContract):
    entries: tuple[A11HistoryEntry, ...]
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_distinct_jobs_and_hash(self) -> "A11HistorySnapshot":
        job_ids = [entry.job_id for entry in self.entries]
        if len(job_ids) != len(set(job_ids)):
            raise ValueError("A11 history must sample distinct jobs")
        payload = [entry.model_dump(mode="json") for entry in self.entries]
        if _sha(payload) != self.snapshot_sha256:
            raise ValueError("A11 history snapshot hash does not match its entries")
        return self


class OrphanArtifactAdoptionEvidence(FrozenCorrectionContract):
    """Evidence required before P1 may adopt one exact durable orphan."""

    job_id: UUID
    stage: CurationStage
    stage_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_kind: str = Field(min_length=1)
    artifact_schema_version: int = Field(gt=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    complete_write_marker: str = Field(min_length=1)
    conflicting_committed_artifact: Literal[False] = False


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("replay identity values must be finite JSON data") from exc


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"nonfinite JSON constant {value} is not allowed")


def _nonblank(value: str | None) -> bool:
    return value is not None and bool(value.strip())


__all__ = [
    "A11HistoryEntry",
    "A11HistorySnapshot",
    "CanonicalJsonObject",
    "DeckSizingPolicy",
    "DuplicateIdentity",
    "EvidenceQuality",
    "FactForbiddenClozeMap",
    "FactForbiddenClozeTargets",
    "GeneratedCardIdentity",
    "GeneratedFactResolution",
    "GeneratedOutputSet",
    "GeneratedResolutionKind",
    "MarginalValueReason",
    "ORDINARY_TARGET",
    "OrphanArtifactAdoptionEvidence",
    "PinnedLectureMetadata",
    "PinnedSemanticGeneration",
    "PromptSnapshotIdentity",
    "QUALITY_FIRST_MODEL_INSTRUCTION",
    "ResolvedStageModelIdentity",
    "SOFT_CAP",
    "SelectionMetadata",
    "SelectionTier",
    "WARNING_FLOOR",
]
