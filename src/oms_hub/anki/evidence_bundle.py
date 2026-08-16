"""Candidate-scoped evidence contract for future v3 provider calls."""

import json
import math

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from oms_hub.anki.contracts import canonical_payload_sha256
from oms_hub.anki.scope_contracts import ScopedConcept


class CandidateCardFields(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1, max_length=300)
    note_id: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=50_000)
    extra: str = Field(max_length=50_000)
    tags: tuple[str, ...] = ()
    deck: str = Field(min_length=1, max_length=1_000)

    @field_validator("tags")
    @classmethod
    def _ordered_tags(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not all(value.strip() for value in values) or values != tuple(sorted(values)):
            raise ValueError("candidate tags must be nonblank and deterministically ordered")
        if len(values) != len(set(values)):
            raise ValueError("candidate tags must be unique")
        return values


class SelectedPassage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passage_id: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=50_000)
    selection_reason: str = Field(min_length=1, max_length=1_000)


class RetrievalScore(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: str = Field(min_length=1, max_length=300)
    score: float

    @field_validator("score", mode="before")
    @classmethod
    def _finite_score(cls, value: object) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("retrieval scores must be finite JSON numbers")
        return float(value)


class CandidateEvidenceBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle_id: str = Field(min_length=1, max_length=300)
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    concept: ScopedConcept
    fact_id: str = Field(min_length=1, max_length=300)
    candidate: CandidateCardFields
    retrieval_scores: tuple[RetrievalScore, ...] = ()
    exact_match_reasons: tuple[str, ...] = ()
    selected_passages: tuple[SelectedPassage, ...] = Field(min_length=1)
    duplicate_sibling_ids: tuple[str, ...] = ()
    allowed_concept_ids: tuple[str, ...] = Field(min_length=1)
    allowed_fact_ids: tuple[str, ...] = Field(min_length=1)
    allowed_passage_ids: tuple[str, ...] = Field(min_length=1)
    input_byte_estimate: int = Field(ge=0)
    input_token_estimate: int = Field(ge=0)
    max_input_bytes: int = Field(gt=0)
    max_input_tokens: int = Field(gt=0)
    truncated: bool
    degraded: bool
    bundle_sha256: str = ""

    @field_validator(
        "exact_match_reasons",
        "duplicate_sibling_ids",
        "allowed_concept_ids",
        "allowed_fact_ids",
        "allowed_passage_ids",
    )
    @classmethod
    def _ordered_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not all(value.strip() for value in values) or values != tuple(sorted(values)):
            raise ValueError("bundle references must be nonblank and deterministically ordered")
        if len(values) != len(set(values)):
            raise ValueError("bundle references must be unique")
        return values

    def canonical_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"bundle_sha256"})

    @model_validator(mode="after")
    def _validate_bounds_closure_and_hash(self) -> "CandidateEvidenceBundle":
        passage_ids = tuple(item.passage_id for item in self.selected_passages)
        if passage_ids != tuple(sorted(passage_ids)) or len(passage_ids) != len(set(passage_ids)):
            raise ValueError("selected passages must be uniquely and deterministically ordered")
        score_ids = tuple(score.identity for score in self.retrieval_scores)
        if score_ids != tuple(sorted(score_ids)) or len(score_ids) != len(set(score_ids)):
            raise ValueError("retrieval scores must be uniquely and deterministically ordered")
        if self.allowed_concept_ids != (self.concept.concept_id,):
            raise ValueError("bundle concept is outside its allowed concepts")
        defined_fact_ids = {fact.fact_id for fact in self.concept.facts}
        if not set(self.allowed_fact_ids) <= defined_fact_ids:
            raise ValueError("bundle allowed facts are not defined by its scoped concept")
        if self.fact_id not in self.allowed_fact_ids:
            raise ValueError("bundle fact is outside its allowed facts")
        if self.fact_id not in defined_fact_ids:
            raise ValueError("bundle fact is not defined by its scoped concept")
        if not set(passage_ids) <= set(self.allowed_passage_ids):
            raise ValueError("bundle passages escape the allowed passage set")
        if self.input_token_estimate > self.max_input_tokens:
            raise ValueError("bundle token estimate exceeds its maximum")
        payload = self.canonical_payload()
        actual_bytes = len(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        )
        if actual_bytes > self.max_input_bytes or self.input_byte_estimate != actual_bytes:
            raise ValueError("bundle byte estimate does not bound its actual payload")
        expected = canonical_payload_sha256(payload)
        if self.bundle_sha256 not in {"", expected}:
            raise ValueError("bundle hash does not match its canonical payload")
        if not self.bundle_sha256:
            object.__setattr__(self, "bundle_sha256", expected)
        return self
