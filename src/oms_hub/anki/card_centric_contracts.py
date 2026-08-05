"""Frozen contracts for the card_centric_v1 pipeline.

These types intentionally do not reuse retrieval_v4 artifacts.  A card-centric
artifact is self-describing, snapshot-bound, and safe to validate independently
of the old retrieval graph.
"""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CardCentricContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal[1] = 1


class CardRecord(CardCentricContract):
    note_id: int = Field(gt=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str
    extra: str
    tags: tuple[str, ...]
    deck_names: tuple[str, ...]


class CardCentricPassage(CardCentricContract):
    passage_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_kind: Literal["summary", "transcript", "slide"]
    authority: Literal["summary", "transcript", "slide"]
    revision_id: int = Field(gt=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(min_length=1)


class CardCentricSourceIndex(CardCentricContract):
    snapshot_id: str = Field(min_length=1)
    source_revision_hashes: dict[int, str]
    summary_outline_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    passages: tuple[CardCentricPassage, ...]
    prefix: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("source_revision_hashes")
    @classmethod
    def valid_revision_hashes(cls, value: dict[int, str]) -> dict[int, str]:
        if not value or any(
            revision_id <= 0 or len(digest) != 64
            for revision_id, digest in value.items()
        ):
            raise ValueError("source revision hashes are invalid")
        return dict(sorted(value.items()))

    @model_validator(mode="after")
    def validate_order_and_hash(self) -> "CardCentricSourceIndex":
        authority_order = {"summary": 0, "transcript": 1, "slide": 2}
        expected_order = tuple(
            sorted(
                self.passages,
                key=lambda passage: (
                    authority_order[passage.authority],
                    passage.source_id,
                    passage.passage_id,
                ),
            )
        )
        if self.passages != expected_order or len(
            {passage.passage_id for passage in self.passages}
        ) != len(self.passages):
            raise ValueError("source passages are not a unique deterministic order")
        document = {
            "snapshot_id": self.snapshot_id,
            "source_revision_hashes": self.source_revision_hashes,
            "summary_outline_sha256": self.summary_outline_sha256,
            "passages": [passage.model_dump(mode="json") for passage in self.passages],
            "prefix": self.prefix,
        }
        if _sha(document) != self.source_sha256:
            raise ValueError("source index hash does not match its immutable contents")
        return self


class CensusTrust(CardCentricContract):
    decision: Literal["trusted", "blocked"]
    reason: str = Field(min_length=1)
    untagged_rate: float = Field(ge=0, le=1)
    safe_untagged_rate: float = Field(gt=0, le=1)


class SnapshotCensus(CardCentricContract):
    snapshot_id: str = Field(min_length=1)
    denominator_count: int = Field(ge=0)
    tagged_count: int = Field(ge=0)
    untagged_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    mapping: dict[int, Literal["tagged", "untagged", "excluded"]]
    filters_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trust: CensusTrust

    @model_validator(mode="after")
    def validate_accounting(self) -> "SnapshotCensus":
        counts = {
            status: sum(value == status for value in self.mapping.values())
            for status in ("tagged", "untagged", "excluded")
        }
        if (
            len(self.mapping) != self.denominator_count
            or counts["tagged"] != self.tagged_count
            or counts["untagged"] != self.untagged_count
            or counts["excluded"] != self.excluded_count
            or self.tagged_count + self.untagged_count + self.excluded_count
            != self.denominator_count
        ):
            raise ValueError("snapshot census counts do not exactly account for notes")
        return self


class TagScopeResult(CardCentricContract):
    snapshot_id: str = Field(min_length=1)
    filters_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scoped_note_ids: tuple[int, ...]
    unscoped_note_ids: tuple[int, ...]

    @model_validator(mode="after")
    def validate_partition(self) -> "TagScopeResult":
        scoped = set(self.scoped_note_ids)
        unscoped = set(self.unscoped_note_ids)
        if (
            len(scoped) != len(self.scoped_note_ids)
            or len(unscoped) != len(self.unscoped_note_ids)
            or scoped & unscoped
        ):
            raise ValueError("tag scope note IDs must be a disjoint partition")
        return self


CardVerdict = Literal["YES", "MAYBE", "NO"]
CardFlag = Literal[
    "wrong",
    "outdated",
    "ambiguous",
    "non_atomic",
    "poor_cloze",
]


class CardClassification(CardCentricContract):
    note_id: int = Field(gt=0)
    verdict: CardVerdict
    primary_subject: str = Field(min_length=1, max_length=500)
    reason: str = Field(default="", max_length=500)
    covered_concept_ids: tuple[str, ...] = ()
    supporting_passage_ids: tuple[str, ...] = ()
    flags: tuple[CardFlag, ...] = ()

    @field_validator("covered_concept_ids", "supporting_passage_ids", "flags")
    @classmethod
    def unique_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("classifier identifiers and flags must be unique and nonblank")
        return value


class CardClassificationBatchOutput(CardCentricContract):
    results: tuple[CardClassification, ...]


class ClassifierBatchAudit(CardCentricContract):
    batch_index: int = Field(ge=0)
    note_ids: tuple[int, ...]
    request_id: str = Field(min_length=1)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_microusd: int = Field(ge=0)
    cache_creation_input_tokens: int = Field(ge=0)
    cache_read_input_tokens: int = Field(ge=0)


class ClassifierTelemetry(CardCentricContract):
    batch_count: int = Field(ge=0)
    cache_prefix_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cache_mode: Literal["ephemeral", "ordinary_prefix"]
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    thinking_mode: Literal["disabled"] = "disabled"
    request_ids: tuple[str, ...]
    batches: tuple[ClassifierBatchAudit, ...]

    @model_validator(mode="after")
    def validate_batches(self) -> "ClassifierTelemetry":
        if (
            self.batch_count != len(self.batches)
            or self.request_ids != tuple(batch.request_id for batch in self.batches)
            or tuple(batch.batch_index for batch in self.batches)
            != tuple(range(self.batch_count))
        ):
            raise ValueError("classifier telemetry batches are incomplete or unordered")
        return self


class ClassifierResult(CardCentricContract):
    results: tuple[CardClassification, ...]
    telemetry: ClassifierTelemetry


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
