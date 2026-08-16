import pytest

from oms_hub.anki.scope_contracts import (
    LectureScope,
    ScopedConcept,
    ScopedFact,
    ScopeEvidenceReference,
)


def _scope(*, fact_statement: str = "Iron deficiency causes microcytosis.") -> LectureScope:
    evidence = ScopeEvidenceReference(
        evidence_id="evidence-1", source_id="SLD:1", locator="slide:1", content_sha256="a" * 64
    )
    fact = ScopedFact(
        fact_id="fact-1",
        statement=fact_statement,
        evidence_ids=("evidence-1",),
        generation_allowed=True,
    )
    concept = ScopedConcept(
        concept_id="concept-1",
        canonical_statement="Iron deficiency anemia",
        primary_entity="iron",
        depth_tier=1,
        priority=10,
        reason="emphasized",
        facts=(fact,),
        source_evidence_ids=("evidence-1",),
        retrieval_queries=("iron deficiency",),
    )
    return LectureScope(
        scope_id="scope-1",
        policy_sha256="b" * 64,
        source_bundle_sha256="c" * 64,
        degraded_mode="none",
        evidence=(evidence,),
        concepts=(concept,),
    )


def test_scope_hash_is_deterministic_and_bound() -> None:
    scope = _scope()
    assert scope.scope_sha256 == _scope().scope_sha256
    assert scope.scope_sha256 != _scope(fact_statement="Iron causes anemia.").scope_sha256


def test_scope_rejects_reference_escape_and_nondeterministic_order() -> None:
    payload = _scope().model_dump()
    payload["concepts"][0]["facts"][0]["evidence_ids"] = ("missing",)
    with pytest.raises(ValueError, match="escapes"):
        LectureScope.model_validate(payload)


def test_scope_rejects_fact_id_reused_by_different_concepts() -> None:
    payload = _scope().model_dump()
    duplicate = payload["concepts"][0].copy()
    duplicate["concept_id"] = "concept-2"
    duplicate["canonical_statement"] = "another"
    payload["concepts"] = (payload["concepts"][0], duplicate)
    with pytest.raises(ValueError, match="globally unique"):
        LectureScope.model_validate(payload)

    payload = _scope().model_dump()
    payload["concepts"][0]["source_evidence_ids"] = ("evidence-1", "missing")
    payload["concepts"][0]["facts"][0]["evidence_ids"] = ("missing",)
    with pytest.raises(ValueError, match="concept evidence"):
        LectureScope.model_validate(payload)
