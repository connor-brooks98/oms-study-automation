"""Hash-bound scope contracts; no scope-model execution lives here."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from oms_hub.anki.contracts import canonical_payload_sha256


class ScopeEvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=1, max_length=300)
    source_id: str = Field(min_length=1, max_length=300)
    locator: str = Field(min_length=1, max_length=500)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ScopedFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str = Field(min_length=1, max_length=300)
    statement: str = Field(min_length=1, max_length=10_000)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    generation_allowed: bool
    forbidden_cloze_targets: tuple[str, ...] = ()

    @field_validator("evidence_ids", "forbidden_cloze_targets")
    @classmethod
    def _ordered_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not all(value.strip() for value in values) or values != tuple(sorted(values)):
            raise ValueError("references must be nonblank and deterministically ordered")
        if len(values) != len(set(values)):
            raise ValueError("references must be unique")
        return values


class ScopedConcept(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    concept_id: str = Field(min_length=1, max_length=300)
    canonical_statement: str = Field(min_length=1, max_length=10_000)
    primary_entity: str = Field(min_length=1, max_length=1_000)
    aliases: tuple[str, ...] = ()
    exact_terms: tuple[str, ...] = ()
    depth_tier: int = Field(ge=0, le=20)
    priority: int = Field(ge=0, le=100)
    reason: str = Field(min_length=1, max_length=10_000)
    facts: tuple[ScopedFact, ...] = Field(min_length=1)
    source_evidence_ids: tuple[str, ...] = Field(min_length=1)
    professor_policy_basis: tuple[str, ...] = ()
    retrieval_queries: tuple[str, ...] = Field(min_length=1)

    @field_validator(
        "aliases",
        "exact_terms",
        "source_evidence_ids",
        "professor_policy_basis",
        "retrieval_queries",
    )
    @classmethod
    def _ordered_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not all(value.strip() for value in values) or values != tuple(sorted(values)):
            raise ValueError("concept values must be nonblank and deterministically ordered")
        if len(values) != len(set(values)):
            raise ValueError("concept values must be unique")
        return values

    @model_validator(mode="after")
    def _facts_are_ordered(self) -> "ScopedConcept":
        fact_ids = tuple(fact.fact_id for fact in self.facts)
        if fact_ids != tuple(sorted(fact_ids)) or len(fact_ids) != len(set(fact_ids)):
            raise ValueError("facts must be uniquely and deterministically ordered")
        if any(not set(fact.evidence_ids) <= set(self.source_evidence_ids) for fact in self.facts):
            raise ValueError("fact evidence escapes its concept evidence")
        return self


class LectureScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope_id: str = Field(min_length=1, max_length=300)
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    degraded_mode: Literal["none", "missing_emphasis", "transcript_outline"]
    evidence: tuple[ScopeEvidenceReference, ...] = Field(min_length=1)
    concepts: tuple[ScopedConcept, ...] = Field(min_length=1)
    scope_sha256: str = ""

    def canonical_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"scope_sha256"})

    @model_validator(mode="after")
    def _validate_closure_and_hash(self) -> "LectureScope":
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        concept_ids = tuple(item.concept_id for item in self.concepts)
        if evidence_ids != tuple(sorted(evidence_ids)) or len(evidence_ids) != len(
            set(evidence_ids)
        ):
            raise ValueError("scope evidence must be uniquely and deterministically ordered")
        if concept_ids != tuple(sorted(concept_ids)) or len(concept_ids) != len(set(concept_ids)):
            raise ValueError("scope concepts must be uniquely and deterministically ordered")
        allowed = set(evidence_ids)
        for concept in self.concepts:
            if not set(concept.source_evidence_ids) <= allowed:
                raise ValueError("concept evidence escapes scope evidence")
            for fact in concept.facts:
                if not set(fact.evidence_ids) <= allowed:
                    raise ValueError("fact evidence escapes scope evidence")
        fact_ids = tuple(fact.fact_id for concept in self.concepts for fact in concept.facts)
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("scope fact IDs must be globally unique")
        expected = canonical_payload_sha256(self.canonical_payload())
        if self.scope_sha256 not in {"", expected}:
            raise ValueError("scope hash does not match its canonical payload")
        if not self.scope_sha256:
            object.__setattr__(self, "scope_sha256", expected)
        return self
