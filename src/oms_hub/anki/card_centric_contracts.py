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

    @model_validator(mode="after")
    def validate_classifier_passage_id(self) -> "CardCentricPassage":
        prefixes = {
            "summary": "SUM:",
            "transcript": "TRX:",
            "slide": "SLD:",
        }
        prefix = prefixes[self.authority]
        if not self.source_id.startswith(prefix) or not self.passage_id.startswith(
            f"{self.source_id}:P:"
        ):
            raise ValueError("classifier passage IDs must be unique source-prefixed IDs")
        return self


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
            revision_id <= 0 or len(digest) != 64 for revision_id, digest in value.items()
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

    @field_validator("safe_untagged_rate")
    @classmethod
    def three_percent_threshold(cls, value: float) -> float:
        if value != 0.03:
            raise ValueError("card-centric untagged threshold must be three percent")
        return value


class SnapshotCensus(CardCentricContract):
    snapshot_id: str = Field(min_length=1)
    denominator_count: int = Field(ge=0)
    tagged_count: int = Field(ge=0)
    other_system_tagged_count: int = Field(ge=0)
    untagged_count: int = Field(ge=0)
    deck_excluded_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    mapping: dict[
        int,
        Literal[
            "target_tagged",
            "other_system_excluded",
            "untagged",
            "deck_excluded",
        ],
    ]
    filters_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trust: CensusTrust

    @model_validator(mode="after")
    def validate_accounting(self) -> "SnapshotCensus":
        counts = {
            status: sum(value == status for value in self.mapping.values())
            for status in (
                "target_tagged",
                "other_system_excluded",
                "untagged",
                "deck_excluded",
            )
        }
        if (
            len(self.mapping) != self.denominator_count + self.deck_excluded_count
            or counts["target_tagged"] != self.tagged_count
            or counts["other_system_excluded"] != self.other_system_tagged_count
            or counts["untagged"] != self.untagged_count
            or counts["deck_excluded"] != self.deck_excluded_count
            or self.excluded_count != self.other_system_tagged_count + self.deck_excluded_count
            or (self.tagged_count + self.other_system_tagged_count + self.untagged_count)
            != self.denominator_count
        ):
            raise ValueError("snapshot census counts do not exactly account for notes")
        expected_rate = (
            0.0 if self.denominator_count == 0 else self.untagged_count / self.denominator_count
        )
        expected_decision = (
            "trusted"
            if self.denominator_count > 0 and expected_rate < self.trust.safe_untagged_rate
            else "blocked"
        )
        if self.trust.untagged_rate != expected_rate or self.trust.decision != expected_decision:
            raise ValueError("snapshot census trust does not match counted untagged rate")
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
    "context_trap",
    "enumeration",
    "stat_cloze",
    "over_cloze",
]


class CardClassification(CardCentricContract):
    note_id: int = Field(gt=0)
    verdict: CardVerdict
    primary_subject: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=500)
    covered_concept_ids: tuple[str, ...] = ()
    supporting_passage_ids: tuple[str, ...] = ()
    flags: tuple[CardFlag, ...] = ()

    @field_validator("covered_concept_ids", "supporting_passage_ids", "flags")
    @classmethod
    def unique_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("classifier identifiers and flags must be unique and nonblank")
        return value

    @field_validator("reason")
    @classmethod
    def one_line_reason(cls, value: str) -> str:
        if not value.strip() or "\n" in value or "\r" in value:
            raise ValueError("classifier reason must be nonblank and one line")
        return value.strip()


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
            or tuple(batch.batch_index for batch in self.batches) != tuple(range(self.batch_count))
        ):
            raise ValueError("classifier telemetry batches are incomplete or unordered")
        return self


class ClassifierResult(CardCentricContract):
    results: tuple[CardClassification, ...]
    telemetry: ClassifierTelemetry


# S2--S10 are deliberately separate from the retrieval-v4 concepts.  In
# particular, this ledger is a coverage checklist and never contains search
# paraphrases or retrieval scores.
class CardConcept(CardCentricContract):
    concept_id: str = Field(pattern=r"^C[0-9]{2,4}$")
    canonical_statement: str = Field(min_length=1, max_length=4_000)
    primary_entity: str = Field(min_length=1, max_length=500)
    aliases: tuple[str, ...] = ()
    depth: Literal["deep", "medium", "surface"]
    emphasis_flag: bool
    importance: Literal["high", "medium", "low"]

    @field_validator("canonical_statement", "primary_entity")
    @classmethod
    def nonblank_fact(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("concept facts cannot be blank")
        return value

    @field_validator("aliases")
    @classmethod
    def clean_aliases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values)
        if any(not value for value in cleaned) or len(
            {value.casefold() for value in cleaned}
        ) != len(cleaned):
            raise ValueError("concept aliases must be nonblank and unique")
        return cleaned

    @model_validator(mode="after")
    def consistent_importance(self) -> "CardConcept":
        expected = (
            "high"
            if self.depth == "deep" or self.emphasis_flag
            else "medium"
            if self.depth == "medium"
            else "low"
        )
        if self.importance != expected:
            raise ValueError("concept importance conflicts with depth/emphasis")
        return self


class CardConceptLedger(CardCentricContract):
    concepts: tuple[CardConcept, ...] = Field(min_length=1)
    lecture_entity_count: int = Field(ge=1)
    forbidden_cloze_targets: tuple[str, ...] = ()

    @model_validator(mode="after")
    def unique_concept_ids(self) -> "CardConceptLedger":
        ids = [concept.concept_id for concept in self.concepts]
        if len(ids) != len(set(ids)):
            raise ValueError("ledger concept IDs must be stable and unique")
        targets = tuple(value.strip() for value in self.forbidden_cloze_targets)
        if any(not value for value in targets) or len(
            {value.casefold() for value in targets}
        ) != len(targets):
            raise ValueError("forbidden cloze targets must be nonblank and unique")
        object.__setattr__(self, "forbidden_cloze_targets", targets)
        return self


class LedgerProvenance(CardCentricContract):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cache_prefix_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CoverageEvidence(CardCentricContract):
    note_id: int = Field(gt=0)
    supporting_passage_ids: tuple[str, ...] = Field(min_length=1)


class ConceptCoverage(CardCentricContract):
    concept_id: str = Field(pattern=r"^C[0-9]{2,4}$")
    status: Literal["covered", "uncovered"]
    evidence: tuple[CoverageEvidence, ...] = ()

    @model_validator(mode="after")
    def exact_coverage_status(self) -> "ConceptCoverage":
        if (self.status == "covered") != bool(self.evidence):
            raise ValueError("coverage status must exactly match evidence")
        return self


class ResidualHitAudit(CardCentricContract):
    concept_id: str = Field(pattern=r"^C[0-9]{2,4}$")
    query: str = Field(min_length=1)
    hit_note_ids: tuple[int, ...]
    classified_note_ids: tuple[int, ...]


class GeneratedCardResolution(CardCentricContract):
    card_id: str = Field(min_length=1)
    concept_id: str = Field(pattern=r"^C[0-9]{2,4}$")
    fact_id: str = Field(pattern=r"^C[0-9]{2,4}-M[0-9]{1,4}$")
    text: str = ""
    extra: str = ""
    source_passage_ids: tuple[str, ...] = Field(min_length=1)
    evidence_ids: tuple[str, ...] = ()
    split: bool = False
    status: Literal["generated", "unresolved", "duplicate_of_existing"] = "generated"
    duplicate_of_existing_note_id: int | None = None
    duplicate_of_generated_card_id: str | None = None
    reason: str = ""

    @model_validator(mode="after")
    def generated_resolution_integrity(self) -> "GeneratedCardResolution":
        if self.status == "generated" and not self.text.strip():
            raise ValueError("generated card text must not be blank")
        if self.status == "duplicate_of_existing" and (
            self.duplicate_of_existing_note_id is None
            and self.duplicate_of_generated_card_id is None
        ):
            raise ValueError("duplicate generated card must identify its duplicate")
        if self.status != "generated" and not self.reason.strip():
            raise ValueError("unresolved generation needs a reason")
        if self.status == "generated" and not self.evidence_ids:
            raise ValueError("generated cards require materialized evidence")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("generated evidence IDs must be unique")
        return self


class CardGapOutput(CardCentricContract):
    """Exactly the `gap-card-generation.md` output, before pipeline identity."""

    fact_id: str = Field(pattern=r"^C[0-9]{2,4}-M[0-9]{1,4}$")
    status: Literal["generated", "unresolved"]
    text: str = ""
    extra: str = ""
    note_type: str = ""
    source_passage_ids: tuple[str, ...] = ()
    split: bool = False
    image_needed: str | None = None
    reason: str = ""

    @model_validator(mode="after")
    def require_generated_or_explicit_unresolved(self) -> "CardGapOutput":
        if self.status == "generated" and (
            not self.text.strip() or not self.note_type.strip() or not self.source_passage_ids
        ):
            raise ValueError("generated gap output needs cloze text, note type, and sources")
        if self.status == "unresolved" and not self.reason.strip():
            raise ValueError("unresolved gap output needs a reason")
        return self


class CardGapBatch(CardCentricContract):
    resolutions: tuple[CardGapOutput, ...] = Field(min_length=1)


class SelectionEvidence(CardCentricContract):
    selected_existing_note_ids: tuple[int, ...]
    selected_generated_card_ids: tuple[str, ...]
    excluded_existing_note_ids: tuple[int, ...]
    excluded_generated_card_ids: tuple[str, ...]
    target: int = Field(ge=1)
    cap: int = Field(ge=1)
    minimum_target: int = Field(ge=1)
    mandatory_note_ids: tuple[int, ...] = ()
    overflow_acknowledgement: dict[str, object] | None = None

    @model_validator(mode="after")
    def validate_selection_cap(self) -> "SelectionEvidence":
        if not self.minimum_target <= self.target <= self.cap:
            raise ValueError("selection target and cap are invalid")
        selected = len(self.selected_existing_note_ids) + len(self.selected_generated_card_ids)
        if selected > self.cap:
            ack = self.overflow_acknowledgement
            if not ack or not {"acknowledged_by", "acknowledged_at", "reason"} <= set(ack):
                raise ValueError("selection overflow requires immutable acknowledgement")
        if not set(self.mandatory_note_ids) <= set(self.selected_existing_note_ids):
            raise ValueError("mandatory existing cards cannot be excluded")
        return self


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
