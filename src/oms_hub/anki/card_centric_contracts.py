"""Frozen contracts for the card_centric_v1 pipeline.

These types intentionally do not reuse retrieval_v4 artifacts.  A card-centric
artifact is self-describing, snapshot-bound, and safe to validate independently
of the old retrieval graph.
"""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from oms_hub.anki.correction_contracts import (
    SOFT_CAP,
    WARNING_FLOOR,
    CanonicalJsonObject,
    DuplicateIdentity,
    SelectionMetadata,
)
from oms_hub.anki.v2_contracts import FactId


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


class CardEvidenceAudit(CardCentricContract):
    """Deterministic S2b diagnostics, retained for later review surfacing."""

    evidence_poor_concept_ids: tuple[str, ...]
    matched_slide_passage_ids: dict[str, tuple[str, ...]]
    matched_slide_char_counts: dict[str, int]
    threshold_chars: int = Field(ge=0)
    total_concepts: int = Field(ge=0)

    @model_validator(mode="after")
    def valid_concept_evidence(self) -> "CardEvidenceAudit":
        passage_keys = set(self.matched_slide_passage_ids)
        if (
            passage_keys != set(self.matched_slide_char_counts)
            or len(passage_keys) != self.total_concepts
            or not set(self.evidence_poor_concept_ids) <= passage_keys
            or len(self.evidence_poor_concept_ids) != len(set(self.evidence_poor_concept_ids))
            or any(not concept_id.strip() for concept_id in passage_keys)
            or any(count < 0 for count in self.matched_slide_char_counts.values())
            or any(
                len(passage_ids) != len(set(passage_ids))
                for passage_ids in self.matched_slide_passage_ids.values()
            )
        ):
            raise ValueError("evidence audit concept diagnostics are inconsistent")
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
    covered_fact_ids: tuple[FactId, ...] = ()
    supporting_passage_ids: tuple[str, ...] = ()
    flags: tuple[CardFlag, ...] = ()

    @field_validator(
        "covered_concept_ids", "covered_fact_ids", "supporting_passage_ids", "flags"
    )
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
    suggested_fact_count: int = Field(default=1, ge=1, le=5)
    fact_descriptions: tuple[str, ...] = ()
    forbidden_cloze_targets_by_fact: tuple[tuple[str, ...], ...] = ()
    is_mechanism: bool = False

    @property
    def fact_ids(self) -> tuple[str, ...]:
        return tuple(
            f"{self.concept_id}-M{index + 1}" for index in range(self.suggested_fact_count)
        )

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
        # v1 ledgers have no fact fields.  Their canonical statement is exactly
        # the single v2 fact, so old prompt output remains loadable.
        descriptions = tuple(value.strip() for value in self.fact_descriptions)
        if not descriptions and self.suggested_fact_count == 1:
            descriptions = (self.canonical_statement,)
            object.__setattr__(self, "fact_descriptions", descriptions)
        if len(descriptions) != self.suggested_fact_count or any(
            not value for value in descriptions
        ):
            raise ValueError("fact_descriptions length must equal suggested_fact_count")
        by_fact = tuple(
            tuple(value.strip() for value in targets)
            for targets in self.forbidden_cloze_targets_by_fact
        )
        if by_fact and len(by_fact) != self.suggested_fact_count:
            raise ValueError("forbidden_cloze_targets_by_fact must have one tuple per fact")
        if any(not value for targets in by_fact for value in targets):
            raise ValueError("per-fact forbidden cloze targets cannot be blank")
        object.__setattr__(self, "forbidden_cloze_targets_by_fact", by_fact)
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

    @property
    def all_forbidden_targets(self) -> tuple[str, ...]:
        values = set(self.forbidden_cloze_targets)
        for concept in self.concepts:
            for targets in concept.forbidden_cloze_targets_by_fact:
                values.update(targets)
        return tuple(sorted(values, key=str.casefold))


def serialize_card_centric_ledger(
    ledger: CardConceptLedger,
    *,
    pipeline_contract_version: str,
) -> dict[str, object]:
    """Emit the immutable artifact shape for the requested card contract.

    v1 ledger artifacts are content-addressed pinned inputs. The v2-only fact
    fields must stay out of their serialized document even though the shared
    reader supplies defaults so legacy output remains consumable.
    """
    if pipeline_contract_version == "card_centric_v1":
        return {
            "contract_version": ledger.contract_version,
            "concepts": [
                {
                    "contract_version": concept.contract_version,
                    "concept_id": concept.concept_id,
                    "canonical_statement": concept.canonical_statement,
                    "primary_entity": concept.primary_entity,
                    "aliases": list(concept.aliases),
                    "depth": concept.depth,
                    "emphasis_flag": concept.emphasis_flag,
                    "importance": concept.importance,
                }
                for concept in ledger.concepts
            ],
            "lecture_entity_count": ledger.lecture_entity_count,
            "forbidden_cloze_targets": list(ledger.forbidden_cloze_targets),
        }
    return ledger.model_dump(mode="json")


class SemanticPreFilterResult(CardCentricContract):
    pre_filtered_note_ids: tuple[int, ...]
    pre_excluded_note_ids: tuple[int, ...]
    threshold: float = Field(ge=0, le=1)
    similarity_stats: dict[str, float]

    @model_validator(mode="after")
    def partitions_notes(self) -> "SemanticPreFilterResult":
        if set(self.pre_filtered_note_ids) & set(self.pre_excluded_note_ids):
            raise ValueError("semantic prefilter note IDs must be disjoint")
        return self


class FastCardClassification(CardCentricContract):
    note_id: int = Field(gt=0)
    verdict: Literal["LIKELY_YES", "NEEDS_REVIEW", "LIKELY_NO"]
    grounded_concept_ids: tuple[str, ...] = ()
    supporting_passage_ids: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    reason: str = ""


class FastClassificationResult(CardCentricContract):
    results: tuple[FastCardClassification, ...]

    @model_validator(mode="after")
    def unique_note_ids(self) -> "FastClassificationResult":
        note_ids = [item.note_id for item in self.results]
        if len(note_ids) != len(set(note_ids)):
            raise ValueError("fast classification contains duplicate note IDs")
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
    split_index: int | None = Field(default=None, ge=1)
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
        if self.status == "duplicate_of_existing" and (
            self.duplicate_of_existing_note_id is not None
            and self.duplicate_of_generated_card_id is not None
        ):
            raise ValueError("duplicate generated card must identify exactly one duplicate")
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
    split_index: int | None = Field(default=None, ge=1)
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


class DedupeAdvisoryCandidate(CardCentricContract):
    """Lexical-only evidence after semantic dedupe exhausts its retry budget."""

    card_id: str = Field(min_length=1)
    fact_id: str = Field(pattern=r"^C[0-9]{2,4}-M[0-9]{1,4}$")
    identity: DuplicateIdentity
    lexical_score: float = Field(ge=0, le=1)


class SemanticDedupeReview(CardCentricContract):
    """Non-terminal review metadata; it never makes a card automatically unique."""

    card_id: str = Field(min_length=1)
    fact_id: str = Field(pattern=r"^C[0-9]{2,4}-M[0-9]{1,4}$")
    retry_exhausted: Literal[True] = True
    automatic_unique: Literal[False] = False
    lexical_candidates: tuple[DedupeAdvisoryCandidate, ...]

    @model_validator(mode="after")
    def candidates_describe_this_generated_card(self) -> "SemanticDedupeReview":
        if any(
            candidate.card_id != self.card_id or candidate.fact_id != self.fact_id
            for candidate in self.lexical_candidates
        ):
            raise ValueError("dedupe advisory candidates must match the reviewed card and fact")
        identities = [candidate.identity.model_dump_json() for candidate in self.lexical_candidates]
        if len(identities) != len(set(identities)):
            raise ValueError("dedupe advisory candidate identities must be unique")
        return self


def _existing_selection_identity(note_id: int) -> str:
    return f"existing:{note_id}"


def _generated_selection_identity(card_id: str) -> str:
    return f"generated:{card_id}"


class QualitySelectionResult(CardCentricContract):
    """P3-C's immutable quality-first selection partition and audit record."""

    existing_candidate_note_ids: tuple[int, ...]
    generated_candidate_card_ids: tuple[str, ...]
    selected_existing_note_ids: tuple[int, ...]
    selected_generated_card_ids: tuple[str, ...]
    excluded_existing_note_ids: tuple[int, ...]
    excluded_generated_card_ids: tuple[str, ...]
    selection_metadata: tuple[SelectionMetadata, ...]
    below_warning_floor: bool
    target: int = Field(ge=1)
    cap: int = Field(ge=1)
    minimum_target: int = Field(ge=1)
    mandatory_note_ids: tuple[int, ...] = ()
    mandatory_generated_card_ids: tuple[str, ...] = ()
    semantic_review_required_card_ids: tuple[str, ...] = ()
    overflow_acknowledgement: CanonicalJsonObject | None = None

    @model_validator(mode="after")
    def validate_quality_selection(self) -> "QualitySelectionResult":
        if not self.minimum_target <= self.target <= self.cap:
            raise ValueError("selection minimum, target, and cap are invalid")
        candidate_existing = set(self.existing_candidate_note_ids)
        selected_existing = set(self.selected_existing_note_ids)
        excluded_existing = set(self.excluded_existing_note_ids)
        if (
            len(candidate_existing) != len(self.existing_candidate_note_ids)
            or len(selected_existing) != len(self.selected_existing_note_ids)
            or len(excluded_existing) != len(self.excluded_existing_note_ids)
            or selected_existing & excluded_existing
            or selected_existing | excluded_existing != candidate_existing
        ):
            raise ValueError("existing selection partitions must be disjoint and exact")
        candidate_generated = set(self.generated_candidate_card_ids)
        selected_generated = set(self.selected_generated_card_ids)
        excluded_generated = set(self.excluded_generated_card_ids)
        if (
            len(candidate_generated) != len(self.generated_candidate_card_ids)
            or any(not card_id.strip() for card_id in candidate_generated)
            or len(selected_generated) != len(self.selected_generated_card_ids)
            or len(excluded_generated) != len(self.excluded_generated_card_ids)
            or selected_generated & excluded_generated
            or selected_generated | excluded_generated != candidate_generated
        ):
            raise ValueError("generated selection partitions must be disjoint and exact")
        selected_identities = {
            *(_existing_selection_identity(note_id) for note_id in selected_existing),
            *(_generated_selection_identity(card_id) for card_id in selected_generated),
        }
        metadata_identities = {item.identity for item in self.selection_metadata}
        positions = sorted(item.selected_position for item in self.selection_metadata)
        if metadata_identities != selected_identities or len(metadata_identities) != len(
            self.selection_metadata
        ):
            raise ValueError("selection metadata identities must exactly equal selected identities")
        if positions != list(range(1, len(selected_identities) + 1)):
            raise ValueError("selection metadata positions must be unique and contiguous")
        if self.below_warning_floor != (len(selected_identities) < WARNING_FLOOR):
            raise ValueError("below_warning_floor must derive from the selected count")
        if not set(self.mandatory_note_ids) <= selected_existing:
            raise ValueError("mandatory existing identities must be selected")
        if not set(self.mandatory_generated_card_ids) <= set(selected_generated):
            raise ValueError("mandatory generated identities must be selected")
        review_required = set(self.semantic_review_required_card_ids)
        if not review_required <= candidate_generated or review_required & selected_generated:
            raise ValueError("semantic-review identities must be candidates and never selected")
        for metadata in self.selection_metadata:
            if metadata.selected_position > SOFT_CAP and (
                not metadata.mandatory
                or not metadata.overflow_reason or not metadata.overflow_reason.strip()
                or not metadata.manual_acknowledgement_required
            ):
                raise ValueError("overflow selection metadata must be mandatory and review-ready")
        return self


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
