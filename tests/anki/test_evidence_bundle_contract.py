import json

import pytest
from pydantic import ValidationError

from oms_hub.anki.evidence_bundle import (
    CandidateCardFields,
    CandidateEvidenceBundle,
    RetrievalScore,
    SelectedPassage,
)
from oms_hub.anki.scope_contracts import ScopedConcept, ScopedFact


def _bundle(
    *, text: str = "Iron deficiency", scores: tuple[dict[str, object], ...] = ()
) -> CandidateEvidenceBundle:
    candidate = CandidateCardFields(
        candidate_id="candidate-1", note_id=1, text=text, extra="", tags=("tag",), deck="Deck"
    )
    passage = SelectedPassage(
        passage_id="passage-1", text="Relevant source", selection_reason="scope"
    )
    fact = ScopedFact(
        fact_id="fact-1",
        statement="fact",
        evidence_ids=("evidence-1",),
        generation_allowed=True,
    )
    concept = ScopedConcept(
        concept_id="concept-1", canonical_statement="concept", primary_entity="entity",
        depth_tier=1, priority=1, reason="reason", facts=(fact,),
        source_evidence_ids=("evidence-1",), retrieval_queries=("query",),
    )
    payload = {
        "bundle_id": "bundle-1",
        "policy_sha256": "a" * 64,
        "scope_sha256": "b" * 64,
        "concept": concept.model_dump(),
        "fact_id": "fact-1",
        "candidate": candidate.model_dump(),
        "retrieval_scores": scores,
        "selected_passages": [passage.model_dump()],
        "allowed_concept_ids": ["concept-1"],
        "allowed_fact_ids": ["fact-1"],
        "allowed_passage_ids": ["passage-1"],
        "input_token_estimate": 1,
        "max_input_tokens": 100,
        "truncated": False,
        "degraded": False,
    }
    payload["max_input_bytes"] = 10_000
    payload["input_byte_estimate"] = 0
    for _ in range(3):
        seed = CandidateEvidenceBundle.model_construct(
            **{
                **payload,
                "candidate": candidate,
                "concept": concept,
                "retrieval_scores": tuple(RetrievalScore.model_validate(score) for score in scores),
                "selected_passages": (passage,),
                "allowed_concept_ids": ("concept-1",),
                "allowed_fact_ids": ("fact-1",),
                "allowed_passage_ids": ("passage-1",),
            }
        )
        payload["input_byte_estimate"] = len(
            json.dumps(
                seed.canonical_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        )
    return CandidateEvidenceBundle.model_validate(payload)


def test_bundle_hash_is_bound_and_candidate_scoped() -> None:
    bundle = _bundle()
    assert bundle.bundle_sha256 == _bundle().bundle_sha256
    assert bundle.bundle_sha256 != _bundle(text="Different candidate").bundle_sha256


def test_bundle_scores_are_frozen_after_hashing() -> None:
    bundle = _bundle(scores=({"identity": "rank", "score": 0.5},))
    with pytest.raises(ValidationError):
        bundle.retrieval_scores += (RetrievalScore(identity="other", score=0.1),)
    with pytest.raises(ValidationError):
        bundle.retrieval_scores[0].score = 1.0


def test_bundle_rejects_reference_escape_and_actual_byte_overflow() -> None:
    payload = _bundle().model_dump()
    payload["allowed_passage_ids"] = ("other",)
    with pytest.raises(ValueError, match="escape"):
        CandidateEvidenceBundle.model_validate(payload)
    payload = _bundle().model_dump()
    payload["max_input_bytes"] = 1
    with pytest.raises(ValueError, match="byte estimate"):
        CandidateEvidenceBundle.model_validate(payload)


@pytest.mark.parametrize(
    "scores",
    (
        ({"identity": "", "score": 0.5},),
        ({"identity": "rank", "score": True},),
        ({"identity": "rank", "score": float("nan")},),
    ),
)
def test_bundle_rejects_non_json_scores(scores: tuple[dict[str, object], ...]) -> None:
    payload = _bundle().model_dump()
    payload["retrieval_scores"] = scores
    with pytest.raises(ValueError, match="retrieval"):
        CandidateEvidenceBundle.model_validate(payload)


def test_bundle_rejects_duplicate_or_unordered_tags() -> None:
    payload = _bundle().model_dump()
    payload["candidate"]["tags"] = ("b", "a")
    with pytest.raises(ValueError, match="tags"):
        CandidateEvidenceBundle.model_validate(payload)


def test_bundle_rejects_unknown_or_mismatched_fact() -> None:
    payload = _bundle().model_dump()
    payload["fact_id"] = "missing"
    payload["allowed_fact_ids"] = ("missing",)
    with pytest.raises(ValueError, match="not defined"):
        CandidateEvidenceBundle.model_validate(payload)


def test_bundle_rejects_undefined_allowed_identities() -> None:
    payload = _bundle().model_dump()
    payload["allowed_concept_ids"] = ("concept-1", "missing")
    with pytest.raises(ValueError, match="concept"):
        CandidateEvidenceBundle.model_validate(payload)
    payload = _bundle().model_dump()
    payload["allowed_fact_ids"] = ("fact-1", "missing")
    with pytest.raises(ValueError, match="allowed facts"):
        CandidateEvidenceBundle.model_validate(payload)
