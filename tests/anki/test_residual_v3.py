import asyncio
import hashlib
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace

import pytest

import oms_hub.anki.stages as stages
from oms_hub.anki.card_centric_contracts import CensusTrust, SnapshotCensus
from oms_hub.anki.classification_v3 import r7_pin_document, route_document
from oms_hub.anki.contracts import canonical_payload_sha256
from oms_hub.anki.cost_estimator import FrozenRateTable, ModelRate
from oms_hub.anki.course_policy import CourseCurationPolicy
from oms_hub.anki.domain import (
    CurationStage,
    PipelineContractVersion,
    ResolvedModelConfiguration,
    ResolvedStageModel,
    StageUsage,
)
from oms_hub.anki.pipeline import PinnedInputChanged, pipeline_stages
from oms_hub.anki.scope_contracts import (
    LectureScope,
    ScopedConcept,
    ScopedFact,
    ScopeEvidenceReference,
)
from oms_hub.anki.stages import CurationServicesRunner, _v3_r8_raw_safety, _v3_residual_qualifies


def _add_r0_costs(r0: dict[str, object], *models: str) -> None:
    table = FrozenRateTable(
        tuple(ModelRate(model, 1, 0, 0, 1, 1) for model in sorted(set(models))),
        datetime(2026, 8, 17, tzinfo=UTC),
        "fixture",
    )
    policy = CourseCurationPolicy.model_validate(r0["policy"])
    r0.update(
        rate_table=table.document(),
        rate_table_sha256=table.rate_table_sha256,
        ordinary_cost_limit_microusd=policy.ordinary_cost_limit_microusd,
        hard_stop_cost_limit_microusd=policy.hard_stop_cost_limit_microusd,
        cost_ledger=[],
        cost_ledger_sha256=hashlib.sha256(b"[]").hexdigest(),
    )


def _r5(*, raw_limit: int = 50) -> dict[str, object]:
    return {
        "candidates": [
            {
                "note_id": 1,
                "content_sha256": "a" * 64,
                "semantic_score": 0.8,
                "lexical_rank": None,
                "exact_match_reasons": [],
            }
        ],
        "raw_semantic": [[{"note_id": 1, "score": 0.8, "content_hash": "x" * 64}]],
        "raw_lexical": [],
        "raw_limit": raw_limit,
    }


def _r6() -> dict[str, object]:
    return {
        "all_candidates": [{"note_id": 1, "content_sha256": "a" * 64}],
        "clusters": [
            {
                "representative_note_id": 1,
                "sibling_note_ids": [1],
                "missing_vector_note_ids": [],
            }
        ],
        "per_fact_cap_excluded_note_ids": [],
        "global_cap_excluded_note_ids": [],
    }


def test_r8_raw_reuse_is_closed_and_caps_or_nonidentical_siblings_are_unresolved() -> None:
    expected = {1: "a" * 64}
    semantic = {1: "x" * 64}
    assert _v3_r8_raw_safety(_r5(), _r6(), expected, semantic, 0.5, 50) == []
    tampered = _r5()
    tampered["raw_semantic"] = [[{"note_id": 1, "score": 0.8, "content_hash": "z" * 64}]]
    with pytest.raises(PinnedInputChanged, match="raw-hit closure"):
        _v3_r8_raw_safety(tampered, _r6(), expected, semantic, 0.5, 50)
    capped = _r5()
    capped["raw_lexical"] = [{"note_id": 1, "score": 0.1}] * 50
    assert "lexical raw cap filled" in _v3_r8_raw_safety(capped, _r6(), expected, semantic, 0.5, 50)
    sibling = _r6()
    sibling["all_candidates"] = [
        {"note_id": 1, "content_sha256": "a" * 64},
        {"note_id": 2, "content_sha256": "b" * 64},
    ]
    sibling["clusters"] = [
        {"representative_note_id": 1, "sibling_note_ids": [1, 2], "missing_vector_note_ids": []}
    ]
    problems = _v3_r8_raw_safety(_r5(), sibling, {**expected, 2: "b" * 64}, semantic, 0.5, 50)
    assert "non-identical R6 sibling remains unclassified" in problems


def test_r8_never_retrieves_and_v3_pipeline_exposes_r8() -> None:
    assert _v3_residual_qualifies(
        {"exact_match_reasons": [], "lexical_rank": None, "semantic_score": 0.5}, 0.5
    )
    assert not _v3_residual_qualifies(
        {"exact_match_reasons": [], "lexical_rank": None, "semantic_score": 0.49}, 0.5
    )
    assert any(
        definition.stage is CurationStage.V3_R8_GAP_CONFIRMATION
        for definition in pipeline_stages(PipelineContractVersion.CARD_CENTRIC_V3)
    )


def test_r8_dispatches_residual_bundle_without_initial_r6_representative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = CourseCurationPolicy(
        policy_id="p",
        revision=1,
        course_id="c",
        professor_label="prof",
        scope_instruction="scope",
        emphasis_mode="transcript_emphasis",
        missing_emphasis_fallback="block",
        tag_scope_mode="prior_boost",
        classification_strictness="strict",
        generation_style_profile="cloze",
        ordinary_cost_limit_microusd=10,
        hard_stop_cost_limit_microusd=10,
    )
    fact = ScopedFact(
        fact_id="fact", statement="Fact", evidence_ids=("e",), generation_allowed=True
    )
    scope = LectureScope(
        scope_id="s",
        policy_sha256=policy.policy_sha256,
        source_bundle_sha256="a" * 64,
        degraded_mode="none",
        evidence=(
            ScopeEvidenceReference(
                evidence_id="e",
                source_id="source",
                locator="loc",
                content_sha256=sha256(b"evidence").hexdigest(),
            ),
        ),
        concepts=(
            ScopedConcept(
                concept_id="concept",
                canonical_statement="Concept",
                primary_entity="Entity",
                depth_tier=1,
                priority=1,
                reason="r",
                facts=(fact,),
                source_evidence_ids=("e",),
                retrieval_queries=("q",),
            ),
        ),
    )
    route = ResolvedStageModel("openai", "fake", thinking_mode="disabled")
    model_config = ResolvedModelConfiguration(
        "v3",
        route,
        route,
        route,
        route,
        scope_r3=route,
        cheap_classify_r7=route,
        thorough_classify_r7=route,
        generation_r9=route,
    )
    r0 = {
        "policy": policy.model_dump(mode="json"),
        "policy_sha256": policy.policy_sha256,
        "policy_revision": 1,
        "model_config_sha256": "m" * 64,
    }
    r0.update(
        cheap_classify_r7=route_document(route),
        thorough_classify_r7=route_document(route),
        generation_r9=route_document(route),
    )
    _add_r0_costs(r0, route.model)
    r0["r7_classification"] = r7_pin_document(route, route, str(r0["rate_table_sha256"]))
    candidate = {
        "note_id": 9,
        "content_sha256": "d" * 64,
        "text": "off-tag",
        "extra": "",
        "tags": [],
        "decks": ["Deck"],
        "semantic_score": 0.9,
        "lexical_rank": None,
        "semantic_rank": 1,
        "base_rrf": 0.1,
        "boost_total": 0.99,
        "calibrated_score": 1.09,
        "exact_match_reasons": [],
    }
    r5 = {
        "config": {"semantic_threshold": 0.5, "raw_limit": 50},
        "facts": [
            {"fact_id": "fact", "candidates": [candidate], "raw_semantic": [], "raw_lexical": []}
        ],
        "artifact_sha256": "r5",
    }
    r6 = {
        "records": [
            {
                "fact_id": "fact",
                "all_candidates": [],
                "clusters": [],
                "per_fact_cap_excluded_note_ids": [],
                "global_cap_excluded_note_ids": [],
            }
        ],
        "artifact_sha256": "r6",
    }
    r7 = {
        "artifact_sha256": "r7",
        "blocking": False,
        "bundles": [],
        "final_partition": [],
        "bundles_sha256": canonical_payload_sha256([]),
    }
    r4 = {
        "card_identities": [{"note_id": 9, "content_sha256": "d" * 64}],
        "semantic_identities": [],
        "verification_sha256": "r4",
    }
    job = SimpleNamespace(
        pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V3,
        id="residual-job",
        policy_sha256=policy.policy_sha256,
        model_config_sha256="m" * 64,
        resolved_model_config=model_config,
        offline_replay_only=True,
    )
    context = SimpleNamespace(
        job=job,
        stage=CurationStage.V3_R8_GAP_CONFIRMATION,
        prior_payloads={
            CurationStage.V3_R3_SCOPE: {
                "cost_ledger": [],
                "cost_ledger_sha256": hashlib.sha256(b"[]").hexdigest(),
            },
            CurationStage.V3_R4_INDEX_VERIFICATION: {},
        },
    )
    monkeypatch.setattr(stages, "_v3_phase_f_inputs", lambda _context: (r0, scope, r4, r5, r6, r7))
    monkeypatch.setattr(stages, "_v3_scope_evidence", lambda *_args: {"e": "evidence"})
    calls: list[object] = []
    usage = StageUsage("residual", 1, 2, 3)

    def classify(_self: object, **kwargs: object) -> SimpleNamespace:
        calls.append(kwargs)
        bundle = kwargs["bundles"][0]
        return SimpleNamespace(
            payload={
                "final_partition": [
                    {
                        "bundle_id": bundle.bundle_id,
                        "disposition": "keep",
                        "supporting_passage_ids": ["e"],
                        "redundant_with_candidate_id": None,
                    }
                ],
                "blocking": False,
            },
            blocking_error=None,
            usage=usage,
        )

    monkeypatch.setattr(stages.R7ClassificationService, "classify", classify)
    runner = object.__new__(CurationServicesRunner)
    runner.structured = SimpleNamespace(generator=SimpleNamespace(offline_replay_only=True))
    runner.embedder = SimpleNamespace(offline_replay_only=True)
    runner.semantic = SimpleNamespace(embedder=SimpleNamespace(offline_replay_only=True))
    product = asyncio.run(runner.run(context))
    assert product.payload["records"][0]["state"] == "covered_residual" and len(calls) == 1
    assert product.usage == usage
    scores = calls[0]["bundles"][0].retrieval_scores
    assert {score.identity for score in scores}.isdisjoint({"boost_total", "calibrated_score"})
    calls.clear()
    candidate["tags"] = ["tag:a", "Tag:z"]
    r6["records"][0].update(
        all_candidates=[candidate],
        per_fact_cap_excluded_note_ids=[10],
        clusters=[
            {
                "representative_note_id": 9,
                "sibling_note_ids": [9],
                "missing_vector_note_ids": [],
            }
        ],
    )
    product = asyncio.run(runner.run(context))
    assert product.payload["records"][0]["state"] == "covered_residual" and len(calls) == 1
    assert calls[0]["bundles"][0].candidate.tags == ("tag:a", "Tag:z")
    r6["records"][0].update(all_candidates=[], per_fact_cap_excluded_note_ids=[], clusters=[])
    monkeypatch.setattr(
        stages.R7ClassificationService,
        "classify",
        lambda _self, **_kwargs: SimpleNamespace(
            payload={"final_partition": [], "blocking": True},
            blocking_error="residual blocked",
            usage=None,
        ),
    )
    assert asyncio.run(runner.run(context)).blocking_error == "residual blocked"
    r5["facts"][0]["candidates"] = []
    r4["card_identities"] = [
        {"note_id": 9, "content_sha256": "d" * 64},
        {"note_id": 10, "content_sha256": "e" * 64},
    ]
    for semantic_identities, expected_state in (
        ([], "unresolved"),
        ([{"note_id": 9, "semantic_content_sha256": "x" * 64}], "unresolved"),
        (
            [
                {"note_id": 9, "semantic_content_sha256": "x" * 64},
                {"note_id": 10, "semantic_content_sha256": "y" * 64},
            ],
            "confirmed_missing",
        ),
    ):
        r4["semantic_identities"] = semantic_identities
        context.stage = CurationStage.V3_R8_GAP_CONFIRMATION
        confirmed = asyncio.run(runner.run(context))
        assert confirmed.payload["records"][0]["state"] == expected_state
        if expected_state == "unresolved":
            context.prior_payloads[CurationStage.V3_R8_GAP_CONFIRMATION] = confirmed.payload
            context.stage = CurationStage.V3_R9_GENERATION
            generated = asyncio.run(runner.run(context))
            assert generated.payload["requests"] == [] and generated.payload["resolutions"] == []


def test_phase_f_rejects_an_r5_r4_closure_mismatch_before_every_handler_dispatches() -> None:
    policy = CourseCurationPolicy(
        policy_id="p",
        revision=1,
        course_id="c",
        professor_label="prof",
        scope_instruction="scope",
        emphasis_mode="transcript_emphasis",
        missing_emphasis_fallback="block",
        tag_scope_mode="disabled",
        classification_strictness="strict",
        generation_style_profile="cloze",
        ordinary_cost_limit_microusd=10,
        hard_stop_cost_limit_microusd=10,
    )
    route = ResolvedStageModel("openai", "fake", thinking_mode="disabled")
    config = ResolvedModelConfiguration(
        "v3",
        route,
        route,
        route,
        route,
        cheap_classify_r7=route,
        thorough_classify_r7=route,
        generation_r9=route,
    )
    source_bundle = {
        "evidence": [
            {
                "evidence_id": "e",
                "normalized_text": "evidence",
                "content_sha256": sha256(b"evidence").hexdigest(),
            }
        ]
    }
    scope = LectureScope(
        scope_id="s",
        policy_sha256=policy.policy_sha256,
        source_bundle_sha256=canonical_payload_sha256(source_bundle),
        degraded_mode="none",
        evidence=(
            ScopeEvidenceReference(
                evidence_id="e",
                source_id="source",
                locator="locator",
                content_sha256=sha256(b"evidence").hexdigest(),
            ),
        ),
        concepts=(
            ScopedConcept(
                concept_id="concept",
                canonical_statement="Concept",
                primary_entity="Entity",
                depth_tier=1,
                priority=1,
                reason="reason",
                facts=(
                    ScopedFact(
                        fact_id="fact",
                        statement="Fact",
                        evidence_ids=("e",),
                        generation_allowed=True,
                    ),
                ),
                source_evidence_ids=("e",),
                retrieval_queries=("query",),
            ),
        ),
    )
    census = SnapshotCensus(
        snapshot_id="snapshot",
        denominator_count=0,
        tagged_count=0,
        other_system_tagged_count=0,
        untagged_count=0,
        deck_excluded_count=0,
        excluded_count=0,
        mapping={},
        filters_sha256="d" * 64,
        trust=CensusTrust(
            decision="blocked", reason="empty", untagged_rate=0.0, safe_untagged_rate=0.03
        ),
    )
    manifest = {
        "generation": "semantic-1",
        "model": "fake",
        "dimensions": 2,
        "matrix_sha256": "m" * 64,
    }
    cards: list[dict[str, object]] = []
    r4 = {
        "kind": "card_centric_v3_index_verification",
        "policy_sha256": policy.policy_sha256,
        "companion_generation": "companion-1",
        "lexical_generation": "companion-1",
        "semantic_generation": "semantic-1",
        "deck_allowlist": ["Deck"],
        "tag_allowlist": [],
        "card_identities": cards,
        "cards_sha256": stages.canonical_sha256(cards),
        "semantic_identities": [],
        "semantic_manifest": manifest,
        "semantic_manifest_sha256": stages.canonical_sha256(manifest),
        "census": census.model_dump(mode="json"),
        "census_sha256": stages.canonical_sha256(census.model_dump(mode="json")),
    }
    r4["verification_sha256"] = stages.canonical_sha256(r4)
    r0 = {
        "policy": policy.model_dump(mode="json"),
        "policy_sha256": policy.policy_sha256,
        "policy_revision": policy.revision,
        "model_config_sha256": "m" * 64,
        "cheap_classify_r7": route_document(route),
        "thorough_classify_r7": route_document(route),
    }
    _add_r0_costs(r0, route.model)
    r0["r7_classification"] = r7_pin_document(route, route, str(r0["rate_table_sha256"]))
    r5 = {
        "policy_sha256": policy.policy_sha256,
        "scope_sha256": scope.scope_sha256,
        "r4_verification_sha256": "x" * 64,
        "semantic_generation": "semantic-1",
    }
    r5["artifact_sha256"] = stages.canonical_sha256(r5)
    r6 = {
        "policy_sha256": policy.policy_sha256,
        "scope_sha256": scope.scope_sha256,
        "r5_artifact_sha256": r5["artifact_sha256"],
        "semantic_generation": "semantic-1",
    }
    r6["artifact_sha256"] = stages.canonical_sha256(r6)
    r7 = {
        "policy_sha256": policy.policy_sha256,
        "scope_sha256": scope.scope_sha256,
        "r6_artifact_sha256": r6["artifact_sha256"],
    }
    r7["artifact_sha256"] = stages.canonical_sha256(r7)
    job = SimpleNamespace(
        pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V3,
        id="residual-closure-job",
        policy_sha256=policy.policy_sha256,
        model_config_sha256="m" * 64,
        resolved_model_config=config,
        companion_generation="companion-1",
        semantic_generation="semantic-1",
        deck_allowlist=("Deck",),
        tag_allowlist=(),
        offline_replay_only=True,
    )
    context = SimpleNamespace(
        job=job,
        stage=CurationStage.V3_R8_GAP_CONFIRMATION,
        prior_payloads={
            CurationStage.V3_R0_PREFLIGHT: r0,
            CurationStage.V3_R3_SCOPE: {
                "scope": scope.model_dump(mode="json"),
                "source_bundle": source_bundle,
            },
            CurationStage.V3_R4_INDEX_VERIFICATION: r4,
            CurationStage.V3_R5_RETRIEVAL: r5,
            CurationStage.V3_R6_CALIBRATION: r6,
            CurationStage.V3_R7_CLASSIFICATION: r7,
            CurationStage.V3_R8_GAP_CONFIRMATION: {},
            CurationStage.V3_R9_GENERATION: {},
        },
    )
    runner = object.__new__(CurationServicesRunner)
    runner.structured = SimpleNamespace(generator=SimpleNamespace(offline_replay_only=True))
    runner.embedder = SimpleNamespace(offline_replay_only=True)
    runner.semantic = SimpleNamespace(embedder=SimpleNamespace(offline_replay_only=True))
    for stage in (
        CurationStage.V3_R8_GAP_CONFIRMATION,
        CurationStage.V3_R9_GENERATION,
        CurationStage.V3_R10_DEDUPE,
    ):
        context.stage = stage
        with pytest.raises(PinnedInputChanged, match="R0/R3/R5/R6/R7 closure"):
            asyncio.run(runner.run(context))
