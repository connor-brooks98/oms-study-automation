import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PassageId = Annotated[
    str,
    Field(pattern=r"^(?:SLD|TRX|SUM):[A-Z0-9:_-]{2,100}$"),
]
ConceptId = Annotated[str, Field(pattern=r"^C[0-9]{2,4}$")]
FactId = Annotated[str, Field(pattern=r"^C[0-9]{2,4}-M[0-9]{1,4}$")]


class V2Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IntentionallyUncitedV2(V2Contract):
    passage_id: PassageId
    reason: Literal[
        "title_slide",
        "reference_list",
        "objectives_slide",
        "image_only",
    ]


class LectureConceptV2(V2Contract):
    concept_id: ConceptId
    canonical_statement: str = Field(min_length=1, max_length=4_000)
    hypothetical_card: str = Field(min_length=1, max_length=4_000)
    primary_entity: str = Field(min_length=1, max_length=500)
    aliases: tuple[str, ...]
    paraphrases: tuple[str, ...] = Field(min_length=3, max_length=6)
    depth: Literal["deep", "medium", "surface"]
    emphasis_flag: bool
    importance: Literal["high", "medium", "low"]
    passage_ids: tuple[PassageId, ...] = Field(min_length=1)

    @field_validator("aliases", "paraphrases", mode="before")
    @classmethod
    def normalize_text_list(cls, values: object) -> tuple[str, ...]:
        if not isinstance(values, (list, tuple)):
            raise TypeError("concept text lists must be arrays")
        normalized = tuple(str(value).strip() for value in values)
        if any(not value for value in normalized):
            raise ValueError("concept text lists cannot contain blanks")
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("concept text lists cannot contain duplicates")
        return normalized

    @field_validator("passage_ids")
    @classmethod
    def unique_passage_ids(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("concept passage IDs must be unique")
        return values

    @model_validator(mode="after")
    def validate_search_and_importance(self) -> "LectureConceptV2":
        primary = self.primary_entity.casefold()
        if any(primary not in value.casefold() for value in self.paraphrases):
            raise ValueError("every paraphrase must retain the primary entity")
        expected = (
            "high"
            if self.depth == "deep" or self.emphasis_flag
            else "medium"
            if self.depth == "medium"
            else "low"
        )
        if self.importance != expected:
            raise ValueError("concept importance conflicts with depth and emphasis")
        return self


class LectureConceptLedgerV2(V2Contract):
    lecture_entity_count: int = Field(ge=1)
    concepts: tuple[LectureConceptV2, ...] = Field(min_length=1)
    intentionally_uncited: tuple[IntentionallyUncitedV2, ...]

    @model_validator(mode="after")
    def unique_ids(self) -> "LectureConceptLedgerV2":
        concept_ids = [concept.concept_id for concept in self.concepts]
        if len(concept_ids) != len(set(concept_ids)):
            raise ValueError("ledger concept IDs must be unique")
        uncited = [item.passage_id for item in self.intentionally_uncited]
        if len(uncited) != len(set(uncited)):
            raise ValueError("intentionally uncited passage IDs must be unique")
        cited = {
            passage_id
            for concept in self.concepts
            for passage_id in concept.passage_ids
        }
        if cited & set(uncited):
            raise ValueError("a passage cannot be cited and intentionally uncited")
        return self


class MissingFactV2(V2Contract):
    fact_id: FactId
    statement: str = Field(min_length=1, max_length=2_000)
    passage_ids: tuple[PassageId, ...] = Field(min_length=1)

    @field_validator("passage_ids")
    @classmethod
    def unique_passage_ids(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("missing-fact passage IDs must be unique")
        return values


class CoverageJudgmentV2(V2Contract):
    concept_id: ConceptId
    supporting_note_ids: tuple[int, ...]
    missing_facts: tuple[MissingFactV2, ...]
    rationale: str = Field(min_length=1, max_length=4_000)

    @model_validator(mode="after")
    def unique_output_ids(self) -> "CoverageJudgmentV2":
        if len(self.supporting_note_ids) != len(set(self.supporting_note_ids)):
            raise ValueError("supporting note IDs must be unique")
        fact_ids = [fact.fact_id for fact in self.missing_facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("missing fact IDs must be unique")
        if any(not fact.fact_id.startswith(f"{self.concept_id}-M") for fact in self.missing_facts):
            raise ValueError("missing fact IDs must belong to the concept")
        return self


class AuditVerdictV2(V2Contract):
    nid: int = Field(gt=0)
    verdict: Literal["keep", "drop", "uncertain"]
    primary_subject: str = Field(min_length=1, max_length=500)
    support: Literal["transcript", "slides", "both", "summary_only", "none"]
    reason: str = Field(min_length=1, max_length=500)
    structure_issue: tuple[
        Literal["context_trap", "enumeration", "stat_cloze", "over_cloze"],
        ...,
    ]

    @model_validator(mode="after")
    def validate_verdict(self) -> "AuditVerdictV2":
        if self.verdict == "keep" and self.support in {"summary_only", "none"}:
            raise ValueError("summary-only or absent support cannot be kept")
        if len(self.reason.split()) > 15:
            raise ValueError("audit reason cannot exceed 15 words")
        if len(self.structure_issue) != len(set(self.structure_issue)):
            raise ValueError("audit structure issues must be unique")
        return self


class GeneratedGapCardV2(V2Contract):
    fact_id: FactId
    status: Literal["generated"]
    text: str = Field(min_length=1, max_length=10_000)
    extra: str = Field(max_length=20_000)
    note_type: Literal["AnKingOverhaul (AnKing Step Deck / AnKingMed)"]
    source_passage_ids: tuple[PassageId, ...] = Field(min_length=1)
    split: bool
    image_needed: str | None

    @model_validator(mode="after")
    def enforce_source_and_cloze_rules(self) -> "GeneratedGapCardV2":
        if all(value.startswith("SUM:") for value in self.source_passage_ids):
            raise ValueError("generated cards require primary-source evidence")
        if len(re.findall(r"\{\{c\d+::", self.text, flags=re.IGNORECASE)) > 2:
            raise ValueError("generated cards cannot contain more than two clozes")
        return self


class UnresolvedGapV2(V2Contract):
    fact_id: FactId
    status: Literal["unresolved"]
    reason: str = Field(min_length=1, max_length=2_000)
    duplicate_of_note_id: int | None = Field(gt=0)


GapResolutionV2 = GeneratedGapCardV2 | UnresolvedGapV2


class PromptManifestEntryV2(V2Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,99}$")
    version: str = Field(min_length=1, max_length=100)
    prompt_hash: str = Field(pattern=r"^[0-9a-f]{12}$")


class ParaphraseExpansionV2(V2Contract):
    concept_id: ConceptId
    paraphrases: tuple[str, ...] = Field(min_length=3, max_length=3)
    targeting: str = Field(min_length=1, max_length=1_000)

    @field_validator("paraphrases")
    @classmethod
    def normalize_unique_paraphrases(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values)
        if any(not value for value in normalized):
            raise ValueError("expanded paraphrases cannot be blank")
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("expanded paraphrases must be unique")
        return normalized


class ConvergenceConceptV2(V2Contract):
    concept_id: ConceptId
    passes_run: int = Field(ge=1, le=5)
    seen_note_ids: tuple[int, ...]
    growth: tuple[float, ...] = Field(min_length=1, max_length=5)
    converged: bool

    @model_validator(mode="after")
    def reconcile_history(self) -> "ConvergenceConceptV2":
        if self.passes_run != len(self.growth):
            raise ValueError("convergence pass count must match growth history")
        if len(self.seen_note_ids) != len(set(self.seen_note_ids)):
            raise ValueError("convergence note IDs must be unique")
        if any(value < 0 or value > 1 for value in self.growth):
            raise ValueError("convergence growth must be between zero and one")
        return self


class CoverageLedgerEntryV2(V2Contract):
    concept_id: ConceptId
    statement: str = Field(min_length=1)
    importance: Literal["high", "medium", "low"]
    depth: Literal["deep", "medium", "surface"]
    emphasis_flag: bool
    supports: tuple[int, ...]
    rejected: tuple[int, ...]
    missing_facts: tuple[MissingFactV2, ...]
    generated_fact_ids: tuple[FactId, ...]
    status: Literal["covered", "intentional_gap"]


class ReviewEnvelopeV2(V2Contract):
    job_id: str = Field(min_length=1)
    lecture_tag: str = Field(min_length=1)
    prompts: tuple[PromptManifestEntryV2, ...]
    convergence: tuple[ConvergenceConceptV2, ...]
    coverage_ledger: tuple[CoverageLedgerEntryV2, ...]
    audit: tuple[AuditVerdictV2, ...]
    assertions_passed: tuple[str, ...]
    assertions_failed: tuple[str, ...]
    assertions_warned: tuple[str, ...]
    add_tags: tuple[int, ...]
    add_notes: tuple[GeneratedGapCardV2, ...]
    unresolved: tuple[UnresolvedGapV2, ...]

    @model_validator(mode="after")
    def unique_collections(self) -> "ReviewEnvelopeV2":
        for label, values in (
            ("prompt IDs", [item.id for item in self.prompts]),
            ("audit note IDs", [item.nid for item in self.audit]),
            ("tag note IDs", list(self.add_tags)),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"review envelope {label} must be unique")
        return self
