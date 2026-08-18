import asyncio
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from oms_hub.anki.apply import ApplyCoordinator
from oms_hub.anki.calibration import canonical_sha256
from oms_hub.anki.card_centric_review import V3_PHASE_G_SAFETY, V3ReviewSnapshot, reconcile_v3
from oms_hub.anki.contracts import (
    ActionEnvelopeV2,
    SyncOperation,
    VerifyOperation,
    canonical_payload_sha256,
)
from oms_hub.anki.cost_estimator import (
    CostEstimator,
    CostKind,
    CostLedgerEntry,
    FrozenRateTable,
    ModelRate,
    TokenUsage,
)
from oms_hub.anki.course_policy import CourseCurationPolicy
from oms_hub.anki.domain import (
    ApplyState,
    Candidate,
    CreateCurationJob,
    CurationStage,
    CurationState,
    GapCard,
    GapCardEdit,
    PipelineContractVersion,
    RetrievalPass,
    ReviewChangeSet,
)
from oms_hub.anki.models import AnkiCurationJobModel
from oms_hub.anki.pipeline import PinnedInputChanged, StageArtifactStore, StageProduct
from oms_hub.anki.repository import AnkiCurationRepository
from oms_hub.anki.scope_contracts import (
    LectureScope,
    ScopedConcept,
    ScopedFact,
    ScopeEvidenceReference,
)
from oms_hub.anki.stages import CurationServicesRunner
from oms_hub.db import Database
from oms_hub.models import LectureModel
from tests.anki.test_web import prepared_app as web_prepared_app


def _offline_runner() -> CurationServicesRunner:
    runner = object.__new__(CurationServicesRunner)
    runner.structured = SimpleNamespace(generator=SimpleNamespace(offline_replay_only=True))
    runner.embedder = SimpleNamespace(offline_replay_only=True)
    runner.semantic = SimpleNamespace(embedder=SimpleNamespace(offline_replay_only=True))
    return runner


def _snapshot(**changes: object) -> V3ReviewSnapshot:
    value: dict[str, object] = {
        "policy_sha256": "a" * 64,
        "scope_sha256": "b" * 64,
        "rate_table_sha256": "c" * 64,
        "r0_to_r10_sha256": {f"R{i}": f"{i:x}" * 64 for i in range(11)},
        "evidence": {
            "policy_enforcement": {},
            "retrieval": {},
            "cost_ledger": [],
            "phase_g_safety": V3_PHASE_G_SAFETY,
        },
        "existing_candidates": (
            {
                "note_id": 7,
                "decision": "keep",
                "selected": True,
                "source_refs": [{"revision_id": 3, "source_kind": "slide"}],
            },
            {"note_id": 8, "decision": "exclude", "selected": False},
        ),
        "generated_cards": (
            {
                "card_id": "generated-1",
                "status": "generated",
                "evidence_ids": ["e1"],
                "selected": True,
            },
            {"card_id": "unresolved-1", "status": "unresolved", "selected": False},
        ),
        "selected_existing_note_ids": (7,),
        "selected_generated_card_ids": ("generated-1",),
    }
    value.update(changes)
    return V3ReviewSnapshot.model_validate(value)


def test_v3_review_snapshot_retains_full_visibility_and_defaults() -> None:
    snapshot = _snapshot()
    report = reconcile_v3(snapshot)
    assert not report.can_render_envelope
    assert snapshot.existing_candidates[1]["decision"] == "exclude"
    assert snapshot.generated_cards[1]["status"] == "unresolved"
    assert snapshot.selected_existing_note_ids == (7,)
    assert snapshot.selected_generated_card_ids == ("generated-1",)


def test_v3_review_snapshot_rejects_chain_or_selection_tampering() -> None:
    try:
        _snapshot(r0_to_r10_sha256={"R0": "0" * 64})
    except ValueError as exc:
        assert "R0-R10" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("incomplete chain was accepted")
    try:
        _snapshot(selected_existing_note_ids=(99,))
    except ValueError as exc:
        assert "escape" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unknown selection was accepted")


def _closed_snapshot(**changes: object) -> V3ReviewSnapshot:
    value = _snapshot(
        evidence={
            "phase_g_safety": V3_PHASE_G_SAFETY,
            "scope": {
                "concepts": [
                    {
                        "facts": [
                            {"fact_id": "f-generated", "generation_allowed": True},
                            {"fact_id": "f-covered", "generation_allowed": True},
                            {"fact_id": "f-disabled", "generation_allowed": False},
                        ]
                    }
                ]
            },
            "gap_confirmation": {
                "records": [
                    {
                        "fact_id": "f-generated",
                        "generation_allowed": True,
                        "state": "confirmed_missing",
                    },
                    {
                        "fact_id": "f-covered",
                        "generation_allowed": True,
                        "state": "covered_initial",
                    },
                    {
                        "fact_id": "f-disabled",
                        "generation_allowed": False,
                        "state": "confirmed_missing",
                    },
                ]
            },
            "generation": {
                "resolutions": [
                    {"fact_id": "f-generated", "card_id": "generated-1", "status": "generated"},
                    {
                        "fact_id": "f-disabled",
                        "card_id": "unresolved-1",
                        "status": "unresolved",
                        "reason": "generation disabled by scoped fact",
                    },
                ]
            },
            "dedupe": {
                "resolutions": [
                    {"fact_id": "f-generated", "card_id": "generated-1", "status": "generated"},
                    {"fact_id": "f-disabled", "card_id": "unresolved-1", "status": "unresolved"},
                ]
            },
        },
        existing_candidates=(
            {"note_id": 7, "fact_id": "f-covered", "disposition": "keep", "selected": True},
            {"note_id": 8, "disposition": "exclude", "selected": False},
        ),
        generated_cards=(
            {"card_id": "generated-1", "status": "generated", "selected": True},
            {"card_id": "unresolved-1", "status": "unresolved", "selected": False},
        ),
    )
    value = value.model_copy(update=changes)
    return V3ReviewSnapshot.model_validate({**value.model_dump(mode="json"), "snapshot_sha256": ""})


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value["evidence"]["gap_confirmation"]["records"].pop(),
        lambda value: value["evidence"]["gap_confirmation"]["records"].append(
            {"fact_id": "foreign", "generation_allowed": True, "state": "confirmed_missing"}
        ),
        lambda value: value["evidence"]["dedupe"]["resolutions"][0].update(status="unresolved"),
    ),
)
def test_v3_reconciliation_closes_every_fact_or_withholds_envelope(mutate: object) -> None:
    snapshot = _closed_snapshot()
    document = snapshot.model_dump(mode="json")
    mutate(document)  # type: ignore[operator]
    report = reconcile_v3(V3ReviewSnapshot.model_validate({**document, "snapshot_sha256": ""}))
    assert not report.can_render_envelope


def test_cross_fact_generated_duplicate_uses_snapshot_wide_canonical_target() -> None:
    snapshot = _snapshot(
        evidence={
            "phase_g_safety": V3_PHASE_G_SAFETY,
            "scope": {
                "concepts": [
                    {
                        "facts": [
                            {"fact_id": "a", "generation_allowed": True},
                            {"fact_id": "b", "generation_allowed": True},
                        ]
                    }
                ]
            },
            "gap_confirmation": {
                "records": [
                    {"fact_id": "a", "generation_allowed": True, "state": "confirmed_missing"},
                    {"fact_id": "b", "generation_allowed": True, "state": "confirmed_missing"},
                ]
            },
            "generation": {
                "resolutions": [
                    {"fact_id": "a", "card_id": "card:a", "status": "generated"},
                    {"fact_id": "b", "card_id": "card:b", "status": "generated"},
                ]
            },
            "dedupe": {
                "resolutions": [
                    {"fact_id": "a", "card_id": "card:a", "status": "generated"},
                    {
                        "fact_id": "b",
                        "card_id": "card:b",
                        "status": "duplicate_of_generated",
                        "dedupe": {"duplicate_of": "card:a"},
                    },
                ]
            },
        },
        existing_candidates=(),
        generated_cards=(
            {"card_id": "card:a", "status": "generated", "selected": True},
            {"card_id": "card:b", "status": "duplicate_of_generated", "selected": False},
        ),
        selected_existing_note_ids=(),
        selected_generated_card_ids=("card:a",),
    )
    assert reconcile_v3(snapshot).can_render_envelope
    broken = snapshot.model_dump(mode="json")
    broken["generated_cards"][0]["selected"] = False
    broken["selected_generated_card_ids"] = []
    broken["snapshot_sha256"] = ""
    assert not reconcile_v3(V3ReviewSnapshot.model_validate(broken)).can_render_envelope


def _seal(payload: dict[str, object]) -> dict[str, object]:
    payload["artifact_sha256"] = canonical_sha256(payload)
    return payload


def _phase_g_context() -> SimpleNamespace:
    policy = CourseCurationPolicy(
        policy_id="policy",
        revision=1,
        course_id="course",
        professor_label="Professor",
        scope_instruction="Use cited evidence.",
        emphasis_mode="transcript_emphasis",
        missing_emphasis_fallback="block",
        tag_scope_mode="hard_filter",
        classification_strictness="strict",
        generation_style_profile="cloze",
        ordinary_cost_limit_microusd=100_000,
        hard_stop_cost_limit_microusd=100_000,
    )
    evidence_text = "The cited lecture evidence."
    source_bundle = {
        "serialization_version": "scope-source-bundle-v1",
        "degraded_mode": "none",
        "evidence": [
            {
                "evidence_id": "e1",
                "source_id": "slides",
                "locator": "slide:1",
                "normalized_text": evidence_text,
                "content_sha256": hashlib.sha256(evidence_text.encode()).hexdigest(),
            }
        ],
    }
    facts = tuple(
        ScopedFact(
            fact_id=fact_id,
            statement=fact_id,
            evidence_ids=("e1",),
            generation_allowed=True,
        )
        for fact_id in ("duplicate", "initial", "residual", "split")
    )
    scope = LectureScope(
        scope_id="scope",
        policy_sha256=policy.policy_sha256,
        source_bundle_sha256=canonical_payload_sha256(source_bundle),
        degraded_mode="none",
        evidence=(
            ScopeEvidenceReference(
                evidence_id="e1",
                source_id="slides",
                revision_id=1,
                source_kind="slide",
                locator="slide:1",
                content_sha256=hashlib.sha256(evidence_text.encode()).hexdigest(),
            ),
        ),
        concepts=(
            ScopedConcept(
                concept_id="concept",
                canonical_statement="Concept",
                primary_entity="Entity",
                depth_tier=2,
                priority=1,
                reason="lecture",
                facts=facts,
                source_evidence_ids=("e1",),
                retrieval_queries=("query",),
            ),
        ),
    )
    empty_ledger: list[object] = []
    empty_ledger_sha256 = hashlib.sha256(b"[]").hexdigest()
    table = FrozenRateTable(
        rates=(ModelRate("fixture", 1, 1, 1, 1, 1),),
        effective_at=datetime(2026, 8, 17, tzinfo=UTC),
        source="fixture",
    )

    def artifact(**values: object) -> dict[str, object]:
        return _seal(
            {
                **values,
                "cost_ledger": empty_ledger,
                "cost_ledger_sha256": empty_ledger_sha256,
            }
        )

    r0 = _seal(
        {
            "policy": policy.model_dump(mode="json"),
            "policy_sha256": policy.policy_sha256,
            "policy_revision": policy.revision,
            "model_config_sha256": "m" * 64,
            "rate_table": table.document(),
            "rate_table_sha256": table.rate_table_sha256,
            "ordinary_cost_limit_microusd": policy.ordinary_cost_limit_microusd,
            "hard_stop_cost_limit_microusd": policy.hard_stop_cost_limit_microusd,
            "cost_ledger": empty_ledger,
            "cost_ledger_sha256": empty_ledger_sha256,
        }
    )
    r1 = artifact(document={"source": "lecture", "degraded": False})
    r2 = artifact(
        fidelity={"grounding": "cited", "degraded_mode": "none"},
        policy_enforcement={"tier": "professor", "grounding": "required"},
    )
    r3 = artifact(scope=scope.model_dump(mode="json"), source_bundle=source_bundle)
    r4 = artifact(
        verification_sha256="4" * 64,
        companion_generation="companion-1",
        semantic_generation="semantic-1",
        card_identities=[
            {"note_id": note_id, "content_sha256": f"{note_id:x}" * 64} for note_id in (1, 2, 3, 4)
        ],
        semantic_identities=[],
    )
    r5 = artifact(
        policy_sha256=policy.policy_sha256,
        scope_sha256=scope.scope_sha256,
        retrieval={"tier": "hybrid", "pollution": {"polluted": False}},
        facts=[],
    )
    r6 = artifact(
        policy_sha256=policy.policy_sha256,
        scope_sha256=scope.scope_sha256,
        records=[
            {
                "fact_id": "initial",
                "all_candidates": [
                    {"note_id": 1, "content_sha256": "1" * 64},
                    {"note_id": 2, "content_sha256": "2" * 64},
                ],
                "clusters": [
                    {"representative_note_id": 1, "sibling_note_ids": [1, 2]},
                ],
            },
            {
                "fact_id": "duplicate",
                "all_candidates": [{"note_id": 4, "content_sha256": "4" * 64}],
                "clusters": [{"representative_note_id": 4, "sibling_note_ids": [4]}],
            },
        ],
    )
    r7 = artifact(
        policy_sha256=policy.policy_sha256,
        scope_sha256=scope.scope_sha256,
        bundles=[
            {"bundle_id": "initial:1", "fact_id": "initial", "candidate": {"note_id": 1}},
        ],
        bundles_sha256=canonical_payload_sha256(
            [{"bundle_id": "initial:1", "fact_id": "initial", "candidate": {"note_id": 1}}]
        ),
        final_partition=[{"bundle_id": "initial:1", "disposition": "keep"}],
    )
    r8 = artifact(
        policy_sha256=policy.policy_sha256,
        scope_sha256=scope.scope_sha256,
        records=[
            {"fact_id": "initial", "generation_allowed": True, "state": "covered_initial"},
            {"fact_id": "residual", "generation_allowed": True, "state": "covered_residual"},
            {"fact_id": "duplicate", "generation_allowed": True, "state": "confirmed_missing"},
            {"fact_id": "split", "generation_allowed": True, "state": "confirmed_missing"},
        ],
        residual_r7={
            "bundles": [
                {"bundle_id": "residual:3", "fact_id": "residual", "candidate": {"note_id": 3}}
            ],
            "final_partition": [{"bundle_id": "residual:3", "disposition": "keep"}],
        },
    )
    r9 = artifact(
        policy_sha256=policy.policy_sha256,
        scope_sha256=scope.scope_sha256,
        resolutions=[
            {
                "fact_id": "duplicate",
                "card_id": "card:duplicate:1",
                "status": "generated",
                "text": "{{c1::duplicate}}",
                "extra": "",
                "evidence_ids": ["e1"],
            },
            {
                "fact_id": "split",
                "card_id": "card:split:1",
                "status": "generated",
                "text": "{{c1::one}}",
                "extra": "",
                "split_index": 1,
                "evidence_ids": ["e1"],
            },
            {
                "fact_id": "split",
                "card_id": "card:split:2",
                "status": "generated",
                "text": "{{c1::two}}",
                "extra": "",
                "split_index": 2,
                "evidence_ids": ["e1"],
            },
        ],
    )
    r10 = artifact(
        policy_sha256=policy.policy_sha256,
        scope_sha256=scope.scope_sha256,
        resolutions=[
            {
                "fact_id": "duplicate",
                "card_id": "card:duplicate:1",
                "status": "duplicate_of_existing",
                "text": "{{c1::duplicate}}",
                "extra": "",
                "dedupe": {"duplicate_of": "note:4"},
                "evidence_ids": ["e1"],
            },
            {
                "fact_id": "split",
                "card_id": "card:split:1",
                "status": "duplicate_of_generated",
                "text": "{{c1::one}}",
                "extra": "",
                "dedupe": {"duplicate_of": "card:split:2"},
                "evidence_ids": ["e1"],
            },
            {
                "fact_id": "split",
                "card_id": "card:split:2",
                "status": "generated",
                "text": "{{c1::two}}",
                "extra": "",
                "evidence_ids": ["e1"],
            },
        ],
    )
    stages = (
        CurationStage.V3_R0_PREFLIGHT,
        CurationStage.V3_R1_SOURCE_INDEX,
        CurationStage.V3_R2_FIDELITY,
        CurationStage.V3_R3_SCOPE,
        CurationStage.V3_R4_INDEX_VERIFICATION,
        CurationStage.V3_R5_RETRIEVAL,
        CurationStage.V3_R6_CALIBRATION,
        CurationStage.V3_R7_CLASSIFICATION,
        CurationStage.V3_R8_GAP_CONFIRMATION,
        CurationStage.V3_R9_GENERATION,
        CurationStage.V3_R10_DEDUPE,
    )
    return SimpleNamespace(
        job=SimpleNamespace(
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V3,
            id="phase-g-job",
            policy_sha256=policy.policy_sha256,
            model_config_sha256="m" * 64,
            offline_replay_only=True,
        ),
        stage=CurationStage.V3_R11_REVIEW,
        prior_payloads=dict(
            zip(stages, (r0, r1, r2, r3, r4, r5, r6, r7, r8, r9, r10), strict=True)
        ),
    )


def test_r11_runner_projects_visible_candidates_and_exact_frozen_evidence() -> None:
    context = _phase_g_context()
    product = asyncio.run(_offline_runner().run(context))
    snapshot = product.payload["snapshot"]
    assert product.kind == "card_centric_v3_review"
    assert {candidate.note_id for candidate in product.candidates or ()} == {1, 2, 3, 4}
    cards = {card.card_id: card for card in product.gap_cards or ()}
    assert set(cards) == {"card:duplicate:1", "card:split:1", "card:split:2"}
    assert cards["card:duplicate:1"].selected is False
    assert cards["card:duplicate:1"].validation_state == "duplicate_of_existing"
    assert cards["card:split:2"].selected is True
    assert product.payload["artifact_sha256"] == canonical_sha256(
        {key: value for key, value in product.payload.items() if key != "artifact_sha256"}
    )
    assert product.payload["cost_ledger"] == []
    assert {item["note_id"] for item in snapshot["existing_candidates"]} == {1, 2, 3, 4}
    assert snapshot["selected_existing_note_ids"] == [1, 3, 4]
    assert next(item for item in snapshot["existing_candidates"] if item["note_id"] == 2) == {
        "note_id": 2,
        "fact_id": "initial",
        "content_sha256": "2" * 64,
        "disposition": "redundant",
        "redundant_with_candidate_id": "note:1",
        "selected": False,
    }
    generated = {item["card_id"]: item for item in snapshot["generated_cards"]}
    assert generated["card:split:1"]["selected"] is False
    assert generated["card:split:2"]["selected"] is True
    assert snapshot["selected_generated_card_ids"] == ["card:split:2"]
    assert product.payload["reconciliation"]["can_render_envelope"] is True
    evidence = snapshot["evidence"]
    assert set(evidence) >= {
        "r0",
        "r1_source",
        "r2_fidelity",
        "r4_index_verification",
        "scope",
        "retrieval",
        "calibration",
        "classification",
        "gap_confirmation",
        "generation",
        "dedupe",
        "cost_ledger",
        "policy_enforcement",
    }
    r0 = context.prior_payloads[CurationStage.V3_R0_PREFLIGHT]
    assert evidence["r0"]["policy"] == r0["policy"]
    assert evidence["r0"]["rate_table"] == r0["rate_table"]
    assert snapshot["r0_to_r10_sha256"] == {
        f"R{index}": context.prior_payloads[stage]["artifact_sha256"]
        for index, stage in enumerate(context.prior_payloads)
    }


@pytest.mark.parametrize("field", ("revision_id", "source_kind"))
def test_r11_runner_rejects_scope_evidence_without_pinned_provenance(field: str) -> None:
    control = asyncio.run(_offline_runner().run(_phase_g_context()))
    assert control.payload["reconciliation"]["can_render_envelope"] is True

    context = _phase_g_context()
    r3 = context.prior_payloads[CurationStage.V3_R3_SCOPE]
    scope = dict(r3["scope"])
    evidence = [dict(item) for item in scope["evidence"]]
    evidence[0].pop(field)
    scope["evidence"] = evidence
    scope["scope_sha256"] = ""
    context.prior_payloads[CurationStage.V3_R3_SCOPE] = _seal(
        {
            **{key: value for key, value in r3.items() if key != "artifact_sha256"},
            "scope": scope,
        }
    )

    with pytest.raises(PinnedInputChanged, match="R11 scope evidence lacks pinned provenance"):
        asyncio.run(_offline_runner().run(context))


def test_r11_runner_withholds_envelope_for_unresolved_r8_r9_or_r10_overlap() -> None:
    context = _phase_g_context()
    r8 = context.prior_payloads[CurationStage.V3_R8_GAP_CONFIRMATION]
    r8["records"][3]["state"] = "unresolved"
    context.prior_payloads[CurationStage.V3_R8_GAP_CONFIRMATION] = _seal(
        {key: value for key, value in r8.items() if key != "artifact_sha256"}
    )
    product = asyncio.run(_offline_runner().run(context))
    assert product.payload["reconciliation"]["can_render_envelope"] is False


@pytest.mark.parametrize("reverse_rows", (False, True))
def test_r11_runner_deduplicates_selected_note_ids_across_fact_scoped_keep_rows(
    reverse_rows: bool,
) -> None:
    context = _phase_g_context()
    r7 = context.prior_payloads[CurationStage.V3_R7_CLASSIFICATION]
    r7["bundles"].append(
        {"bundle_id": "residual:1", "fact_id": "residual", "candidate": {"note_id": 1}}
    )
    rows = [
        {"bundle_id": "initial:1", "disposition": "keep"},
        {"bundle_id": "residual:1", "disposition": "keep"},
    ]
    r7["final_partition"] = list(reversed(rows)) if reverse_rows else rows
    r7["bundles_sha256"] = canonical_payload_sha256(r7["bundles"])
    context.prior_payloads[CurationStage.V3_R7_CLASSIFICATION] = _seal(
        {key: value for key, value in r7.items() if key != "artifact_sha256"}
    )

    product = asyncio.run(_offline_runner().run(context))
    snapshot = product.payload["snapshot"]
    kept = [
        item
        for item in snapshot["existing_candidates"]
        if item["note_id"] == 1 and item["disposition"] == "keep"
    ]

    assert [item["fact_id"] for item in kept] == (
        ["residual", "initial"] if reverse_rows else ["initial", "residual"]
    )
    assert all(item["selected"] is True for item in kept)
    assert snapshot["selected_existing_note_ids"].count(1) == 1
    assert V3ReviewSnapshot.model_validate(snapshot).selected_existing_note_ids == tuple(
        sorted(set(snapshot["selected_existing_note_ids"]))
    )


def test_r11_runner_snapshot_persists_canonical_redundant_sibling(tmp_path) -> None:
    product = asyncio.run(_offline_runner().run(_phase_g_context()))
    snapshot = product.payload["snapshot"]
    repository, job = _repository(tmp_path)
    with repository.database.engine.begin() as connection:
        connection.execute(
            text("UPDATE anki_curation_jobs SET policy_sha256 = :policy WHERE id = :id"),
            {"policy": snapshot["policy_sha256"], "id": str(job.id)},
        )
    repository.replace_candidates(
        job.id,
        tuple(
            Candidate(
                note_id,
                f"{note_id:x}" * 64,
                "fixture",
                {},
                {},
                "yes",
                "keep",
                1,
                "",
                False,
                "",
                "",
                "keep",
                note_id in {1, 3, 4},
                RetrievalPass.PASS_1,
            )
            for note_id in (1, 2, 3, 4)
        ),
    )
    repository.save_gap_cards(
        job.id,
        (
            GapCard(
                "duplicate",
                "{{c1::duplicate}}",
                "",
                selected=False,
                card_id="card:duplicate:1",
            ),
            GapCard("split", "{{c1::one}}", "", selected=False, card_id="card:split:1"),
            GapCard("split", "{{c1::two}}", "", card_id="card:split:2"),
        ),
    )
    saved = repository.save_review(
        job.id,
        ReviewChangeSet(0),
        card_centric_snapshot=snapshot,
        v3_review_artifact_sha256=product.payload["artifact_sha256"],
        v3_cost_ledger_sha256=product.payload["cost_ledger_sha256"],
    )
    persisted = repository.reviewed_reconciliation(job.id, saved.revision)
    assert persisted and persisted["can_render_envelope"] is True
    reloaded = V3ReviewSnapshot.model_validate(persisted["snapshot"])
    rows = {item["note_id"]: item for item in reloaded.existing_candidates}
    assert rows[1]["selected"] is True and rows[2]["disposition"] == "redundant"
    assert rows[2]["selected"] is False and reloaded.snapshot_sha256


def _repository(tmp_path) -> tuple[AnkiCurationRepository, object]:
    database = Database(f"sqlite:///{tmp_path / 'review.db'}")
    database.migrate()
    with database.session() as session:
        lecture = LectureModel(subject="Heme", exam_number=1, lecture_number=1, topic="Topic")
        session.add(lecture)
        session.flush()
        lecture_id = lecture.id
    repository = AnkiCurationRepository(database)
    job = repository.create_job(
        CreateCurationJob(
            lecture_id=lecture_id,
            block_id=None,
            source_revision_ids=(),
            deck_allowlist=("Deck",),
            tag_allowlist=("Tag",),
            instruction_text="review",
            target_deck="Deck",
            target_tag="Tag",
            index_snapshot_id="snapshot",
            lcl_prompt_version="lcl-v1",
            judgment_rubric_version="judgment-v1",
            gap_prompt_version="gap-v1",
            provider="openai",
            model="model",
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
        )
    )
    rate_table = FrozenRateTable(
        (ModelRate("model", 1, 1, 1, 1, 1),), datetime(2026, 8, 17, tzinfo=UTC), "fixture"
    )
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE anki_curation_jobs SET pipeline_contract_version = 'card_centric_v3', "
                "state = :state, policy_sha256 = :policy, v3_rate_table_json = :rate, "
                "v3_rate_table_sha256 = :rate_sha WHERE id = :id"
            ),
            {
                "state": CurationState.READY_FOR_REVIEW.value,
                "policy": "a" * 64,
                "rate": json.dumps(rate_table.document(), sort_keys=True, separators=(",", ":")),
                "rate_sha": rate_table.rate_table_sha256,
                "id": str(job.id),
            },
        )
    return repository, job


def _persist_r11_snapshot(
    repository: AnkiCurationRepository, job: object, product: object
) -> dict[str, object]:
    payload = product.payload
    snapshot = payload["snapshot"]
    with repository.database.engine.begin() as connection:
        connection.execute(
            text("UPDATE anki_curation_jobs SET policy_sha256 = :policy WHERE id = :id"),
            {"policy": snapshot["policy_sha256"], "id": str(job.id)},
        )
    repository.replace_candidates(job.id, product.candidates or ())
    repository.save_gap_cards(job.id, product.gap_cards or ())
    saved = repository.save_review(
        job.id,
        ReviewChangeSet(0),
        card_centric_snapshot=snapshot,
        v3_review_artifact_sha256=payload["artifact_sha256"],
        v3_cost_ledger_sha256=payload["cost_ledger_sha256"],
    )
    persisted = repository.reviewed_reconciliation(job.id, saved.revision)
    assert persisted is not None
    return persisted


def test_r11_unresolved_rows_without_card_ids_persist_but_withhold_envelope(tmp_path) -> None:
    context = _phase_g_context()
    context.prior_payloads[CurationStage.V3_R8_GAP_CONFIRMATION]["records"][1].update(
        state="unresolved", reason="R8 unresolved"
    )
    context.prior_payloads[CurationStage.V3_R8_GAP_CONFIRMATION] = _seal(
        {
            key: value
            for key, value in context.prior_payloads[CurationStage.V3_R8_GAP_CONFIRMATION].items()
            if key != "artifact_sha256"
        }
    )
    for stage, reason in (
        (CurationStage.V3_R9_GENERATION, "R9 unresolved"),
        (CurationStage.V3_R10_DEDUPE, "R10 unresolved"),
    ):
        payload = context.prior_payloads[stage]
        payload["resolutions"].append(
            {"fact_id": "residual", "status": "unresolved", "reason": reason}
        )
        context.prior_payloads[stage] = _seal(
            {key: value for key, value in payload.items() if key != "artifact_sha256"}
        )
    product = asyncio.run(_offline_runner().run(context))
    assert not any(
        item.get("fact_id") == "residual" for item in product.payload["snapshot"]["generated_cards"]
    )

    repository, job = _repository(tmp_path)
    persisted = _persist_r11_snapshot(repository, job, product)
    cards = {card.card_id: card for card in repository.list_gap_cards(job.id)}
    assert cards["card:duplicate:1"].selected is False
    assert cards["card:duplicate:1"].validation_state == "duplicate_of_existing"
    assert not any(
        card.provenance.get("card_centric_v3", {}).get("fact_id") == "residual"
        for card in cards.values()
    )
    assert persisted["can_render_envelope"] is False


def test_r11_generation_disabled_unresolved_rows_need_no_card_row(tmp_path) -> None:
    context = _phase_g_context()
    scope_document = context.prior_payloads[CurationStage.V3_R3_SCOPE]["scope"]
    for concept in scope_document["concepts"]:
        for fact in concept["facts"]:
            if fact["fact_id"] == "duplicate":
                fact["generation_allowed"] = False
    scope = LectureScope.model_validate({**scope_document, "scope_sha256": ""})
    r3 = context.prior_payloads[CurationStage.V3_R3_SCOPE]
    r3["scope"] = scope.model_dump(mode="json")
    context.prior_payloads[CurationStage.V3_R3_SCOPE] = _seal(
        {key: value for key, value in r3.items() if key != "artifact_sha256"}
    )
    context.prior_payloads[CurationStage.V3_R8_GAP_CONFIRMATION]["records"][2][
        "generation_allowed"
    ] = False
    context.prior_payloads[CurationStage.V3_R8_GAP_CONFIRMATION] = _seal(
        {
            key: value
            for key, value in context.prior_payloads[CurationStage.V3_R8_GAP_CONFIRMATION].items()
            if key != "artifact_sha256"
        }
    )
    for stage, reason in (
        (CurationStage.V3_R9_GENERATION, "generation disabled by scoped fact"),
        (CurationStage.V3_R10_DEDUPE, "generation disabled by scoped fact"),
    ):
        payload = context.prior_payloads[stage]
        payload["scope_sha256"] = scope.scope_sha256
        payload["resolutions"] = [
            item for item in payload["resolutions"] if item.get("fact_id") != "duplicate"
        ]
        payload["resolutions"].append(
            {"fact_id": "duplicate", "status": "unresolved", "reason": reason}
        )
        context.prior_payloads[stage] = _seal(
            {key: value for key, value in payload.items() if key != "artifact_sha256"}
        )
    product = asyncio.run(_offline_runner().run(context))
    assert not any(
        item.get("fact_id") == "duplicate"
        for item in product.payload["snapshot"]["generated_cards"]
    )

    repository, job = _repository(tmp_path)
    persisted = _persist_r11_snapshot(repository, job, product)
    assert persisted["can_render_envelope"] is True


def test_v3_repository_review_reprojects_rows_and_rolls_back_tampering(tmp_path) -> None:
    repository, job = _repository(tmp_path)
    repository.replace_candidates(
        job.id,
        (
            Candidate(
                7,
                "7" * 64,
                "c",
                {},
                {},
                "yes",
                "keep",
                1,
                "",
                False,
                "",
                "",
                "keep",
                True,
                RetrievalPass.PASS_1,
            ),
            Candidate(
                8,
                "8" * 64,
                "c",
                {},
                {},
                "no",
                "exclude",
                1,
                "",
                False,
                "",
                "",
                "exclude",
                False,
                RetrievalPass.PASS_1,
            ),
        ),
    )
    repository.save_gap_cards(
        job.id,
        (
            GapCard("c", "{{c1::generated}}", "", card_id="generated-1"),
            GapCard("c", "unresolved", "", selected=False, card_id="unresolved-1"),
        ),
    )
    snapshot = _closed_snapshot().model_dump(mode="json")
    for bad_snapshot, artifact_sha256, cost_ledger_sha256 in (
        (None, "d" * 64, "e" * 64),
        (snapshot, None, "e" * 64),
        (snapshot, "d" * 64, None),
        ({**snapshot, "snapshot_sha256": ""}, "d" * 64, "e" * 64),
    ):
        with pytest.raises(ValueError, match="v3 review requires"):
            repository.save_review(
                job.id,
                ReviewChangeSet(expected_revision=0, candidate_selections={8: True}),
                card_centric_snapshot=bad_snapshot,
                v3_review_artifact_sha256=artifact_sha256,
                v3_cost_ledger_sha256=cost_ledger_sha256,
            )
        assert repository.require_job(job.id).review_revision == 0
        assert {item.note_id for item in repository.list_candidates(job.id) if item.selected} == {7}
        assert repository.list_gap_cards(job.id)[0].text == "{{c1::generated}}"
    saved = repository.save_review(
        job.id,
        ReviewChangeSet(
            expected_revision=0,
            candidate_selections={8: True},
            gap_edits=(GapCardEdit("c", "{{c1::edited}}", "", True, "generated-1"),),
        ),
        card_centric_snapshot=snapshot,
        v3_review_artifact_sha256="d" * 64,
        v3_cost_ledger_sha256="e" * 64,
    )
    assert saved.revision == 1
    assert {item.note_id for item in repository.list_candidates(job.id) if item.selected} == {7, 8}
    assert repository.list_gap_cards(job.id)[0].text == "{{c1::edited}}"
    reviewed = repository.reviewed_reconciliation(job.id, 1)
    assert reviewed and reviewed["r11_artifact_sha256"] == "d" * 64
    persisted = V3ReviewSnapshot.model_validate(reviewed["snapshot"])
    assert (
        persisted.snapshot_sha256
        and persisted.snapshot_sha256
        == V3ReviewSnapshot.model_validate(
            {**persisted.canonical_payload(), "snapshot_sha256": ""}
        ).snapshot_sha256
    )
    assert set(persisted.selected_existing_note_ids) <= {
        item["note_id"] for item in persisted.existing_candidates
    }
    tampered = _closed_snapshot().model_dump(mode="json")
    tampered["existing_candidates"][0]["note_id"] = 999
    with pytest.raises(ValueError):
        repository.save_review(
            job.id,
            ReviewChangeSet(expected_revision=1, candidate_selections={8: False}),
            card_centric_snapshot={**tampered, "snapshot_sha256": ""},
            v3_review_artifact_sha256="d" * 64,
            v3_cost_ledger_sha256="e" * 64,
        )
    assert repository.require_job(job.id).review_revision == 1
    assert next(item for item in repository.list_candidates(job.id) if item.note_id == 8).selected


def test_v3_envelope_is_bound_then_stops_at_the_no_mutation_seam(tmp_path) -> None:
    repository, job = _repository(tmp_path)
    snapshot = _closed_snapshot().model_dump(mode="json")
    repository.replace_candidates(
        job.id,
        (
            Candidate(
                7,
                "7" * 64,
                "c",
                {},
                {},
                "yes",
                "keep",
                1,
                "",
                False,
                "",
                "",
                "keep",
                True,
                RetrievalPass.PASS_1,
            ),
            Candidate(
                8,
                "8" * 64,
                "c",
                {},
                {},
                "no",
                "exclude",
                1,
                "",
                False,
                "",
                "",
                "exclude",
                False,
                RetrievalPass.PASS_1,
            ),
        ),
    )
    repository.save_gap_cards(
        job.id,
        (
            GapCard("c", "{{c1::generated}}", "", card_id="generated-1"),
            GapCard("c", "unresolved", "", selected=False, card_id="unresolved-1"),
        ),
    )
    repository.save_review(
        job.id,
        ReviewChangeSet(0),
        card_centric_snapshot=snapshot,
        v3_review_artifact_sha256="d" * 64,
        v3_cost_ledger_sha256="e" * 64,
    )
    repository.record_agent_heartbeat(
        agent_id="agent",
        heartbeat_at="2026-08-17T00:00:00+00:00",
        versions={"supported_envelope_contract_versions": (1, 2)},
        active_snapshot_id="snapshot",
        health={"status": "ok"},
    )
    envelope_id = uuid4()
    envelope = ActionEnvelopeV2(
        envelope_id=envelope_id,
        snapshot_id="snapshot",
        target_deck="Deck",
        target_tag="Tag",
        touched_note_hashes={},
        expected_tag_hashes={},
        expected_note_tags={},
        operations=(
            SyncOperation(operation_id=uuid4(), content_sha256="1" * 64),
            VerifyOperation(operation_id=uuid4(), content_sha256="2" * 64, note_ids=()),
        ),
        payload_sha256="0" * 64,
        job_id=job.id,
        pipeline_contract_version="card_centric_v3",
        model_config_sha256=repository.require_job(job.id).model_config_sha256,
        reconciliation_contract_version="card_centric_v3_r11",
        review_revision=1,
        overflow_acknowledgement_provenance={"required": False},
        policy_sha256="a" * 64,
        scope_sha256="b" * 64,
        r11_artifact_sha256="d" * 64,
        r11_snapshot_sha256=repository.reviewed_reconciliation(job.id, 1)["r11_snapshot_sha256"],
        rate_table_sha256="c" * 64,
        cost_ledger_sha256="e" * 64,
    )
    envelope = envelope.model_copy(update={"payload_sha256": canonical_payload_sha256(envelope)})
    stored = repository.create_action_envelope(job.id, envelope)
    assert stored.id == envelope_id
    assert not repository.validate_card_centric_envelope_acknowledgement(envelope_id)

    class NeverGateway:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"gateway must not be reached: {name}")

    result = asyncio.run(ApplyCoordinator(repository, NeverGateway()).apply(envelope_id))
    assert result.state is ApplyState.FAILED_BEFORE_APPLY


def test_v3_redundant_representative_uses_selected_canonical_target_and_rejects_edit(
    tmp_path,
) -> None:
    repository, job = _repository(tmp_path)
    repository.replace_candidates(
        job.id,
        (
            Candidate(
                7,
                "7" * 64,
                "c",
                {},
                {},
                "yes",
                "keep",
                1,
                "",
                False,
                "",
                "",
                "keep",
                False,
                RetrievalPass.PASS_1,
            ),
            Candidate(
                8,
                "8" * 64,
                "c",
                {},
                {},
                "yes",
                "keep",
                1,
                "",
                False,
                "",
                "",
                "keep",
                True,
                RetrievalPass.PASS_1,
            ),
        ),
    )
    repository.save_gap_cards(
        job.id,
        (
            GapCard("c", "{{c1::generated}}", "", card_id="generated-1"),
            GapCard("c", "unresolved", "", selected=False, card_id="unresolved-1"),
        ),
    )
    snapshot = _closed_snapshot().model_dump(mode="json")
    snapshot["existing_candidates"] = [
        {
            "note_id": 7,
            "fact_id": "f-covered",
            "disposition": "redundant",
            "redundant_with_candidate_id": "note:8",
            "selected": False,
        },
        {"note_id": 8, "fact_id": "f-covered", "disposition": "keep", "selected": True},
    ]
    snapshot["selected_existing_note_ids"] = [8]
    snapshot["snapshot_sha256"] = ""
    report = reconcile_v3(V3ReviewSnapshot.model_validate(snapshot))
    assert report.can_render_envelope
    snapshot = V3ReviewSnapshot.model_validate(snapshot).model_dump(mode="json")
    repository.save_review(
        job.id,
        ReviewChangeSet(0),
        card_centric_snapshot=snapshot,
        v3_review_artifact_sha256="d" * 64,
        v3_cost_ledger_sha256="e" * 64,
    )
    persisted = repository.reviewed_reconciliation(job.id, 1)
    assert persisted and persisted["snapshot"]["selected_existing_note_ids"] == [8]
    with pytest.raises(ValueError, match="redundant"):
        repository.save_review(
            job.id,
            ReviewChangeSet(1, candidate_selections={7: True}),
            card_centric_snapshot=snapshot,
            v3_review_artifact_sha256="d" * 64,
            v3_cost_ledger_sha256="e" * 64,
        )
    assert repository.require_job(job.id).review_revision == 1


def _same_note_two_fact_snapshot(reverse_rows: bool) -> dict[str, object]:
    snapshot = _closed_snapshot().model_dump(mode="json")
    snapshot["evidence"]["scope"]["concepts"] = [
        {
            "facts": [
                {"fact_id": "fact-a", "generation_allowed": True},
                {"fact_id": "fact-b", "generation_allowed": True},
            ]
        }
    ]
    snapshot["evidence"]["gap_confirmation"]["records"] = [
        {"fact_id": "fact-a", "generation_allowed": True, "state": "covered_initial"},
        {"fact_id": "fact-b", "generation_allowed": True, "state": "covered_initial"},
    ]
    snapshot["evidence"]["generation"]["resolutions"] = []
    snapshot["evidence"]["dedupe"]["resolutions"] = []
    rows = [
        {
            "note_id": 7,
            "fact_id": "fact-a",
            "disposition": "redundant",
            "redundant_with_candidate_id": "note:8",
            "selected": False,
        },
        {"note_id": 8, "fact_id": "fact-a", "disposition": "keep", "selected": True},
        {"note_id": 7, "fact_id": "fact-b", "disposition": "keep", "selected": True},
    ]
    snapshot["existing_candidates"] = list(reversed(rows)) if reverse_rows else rows
    snapshot["generated_cards"] = []
    snapshot["selected_existing_note_ids"] = [7, 8]
    snapshot["selected_generated_card_ids"] = []
    snapshot["snapshot_sha256"] = ""
    return V3ReviewSnapshot.model_validate(snapshot).model_dump(mode="json")


@pytest.mark.parametrize("reverse_rows", (False, True))
def test_v3_same_note_can_remain_selected_for_one_fact_but_not_another(
    tmp_path, reverse_rows: bool
) -> None:
    repository, job = _repository(tmp_path)
    repository.replace_candidates(
        job.id,
        (
            Candidate(
                7,
                "7" * 64,
                "c",
                {},
                {},
                "yes",
                "keep",
                1,
                "",
                False,
                "",
                "",
                "keep",
                False,
                RetrievalPass.PASS_1,
            ),
            Candidate(
                8,
                "8" * 64,
                "c",
                {},
                {},
                "yes",
                "keep",
                1,
                "",
                False,
                "",
                "",
                "keep",
                True,
                RetrievalPass.PASS_1,
            ),
        ),
    )
    snapshot = _same_note_two_fact_snapshot(reverse_rows)
    assert reconcile_v3(V3ReviewSnapshot.model_validate(snapshot)).can_render_envelope
    saved = repository.save_review(
        job.id,
        ReviewChangeSet(0, candidate_selections={7: True}),
        card_centric_snapshot=snapshot,
        v3_review_artifact_sha256="d" * 64,
        v3_cost_ledger_sha256="e" * 64,
    )
    assert saved.revision == 1
    persisted = repository.reviewed_reconciliation(job.id, 1)
    assert persisted is not None
    reviewed = V3ReviewSnapshot.model_validate(persisted["snapshot"])
    assert reviewed.selected_existing_note_ids == (7, 8)
    assert reconcile_v3(reviewed).can_render_envelope

    redundant_only = _same_note_two_fact_snapshot(reverse_rows)
    redundant_only["existing_candidates"] = [
        {
            "note_id": 7,
            "fact_id": "fact-a",
            "disposition": "redundant",
            "redundant_with_candidate_id": "note:8",
            "selected": False,
        }
    ]
    redundant_only["selected_existing_note_ids"] = []
    redundant_only["snapshot_sha256"] = ""
    redundant_only = V3ReviewSnapshot.model_validate(redundant_only).model_dump(mode="json")
    with pytest.raises(ValueError, match="redundant"):
        repository.save_review(
            job.id,
            ReviewChangeSet(1, candidate_selections={7: True}),
            card_centric_snapshot=redundant_only,
            v3_review_artifact_sha256="d" * 64,
            v3_cost_ledger_sha256="e" * 64,
        )
    assert repository.require_job(job.id).review_revision == 1
    assert repository.list_candidates(job.id)[0].selected is True


@pytest.fixture
def v3_review_route_app(tmp_path) -> tuple[TestClient, object, int, int]:
    fixture = web_prepared_app.__wrapped__(tmp_path)
    client, app, lecture_id, revision_id, _gateway = next(fixture)
    try:
        yield client, app, lecture_id, revision_id
    finally:
        fixture.close()


def test_rehearsal_apply_returns_423_before_any_gateway(
    v3_review_route_app: tuple[TestClient, object, int, int],
) -> None:
    client, app, _lecture_id, _revision_id = v3_review_route_app

    class NeverGateway:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"apply gateway must not be reached: {name}")

    app.state.anki_rehearsal_mode = "deterministic"
    app.state.anki_apply_coordinator = NeverGateway()
    response = client.post(
        f"/api/anki/jobs/{uuid4()}/apply",
        json={"review_revision": 0, "confirmation": "APPLY TO ANKI"},
    )

    assert response.status_code == 423
    assert "hard-disabled" in response.json()["detail"]


def test_v3_review_route_projects_committed_bounded_evidence_and_keeps_legacy_shape(
    v3_review_route_app: tuple[TestClient, object, int, int],
) -> None:
    client, app, lecture_id, revision_id = v3_review_route_app
    repository = app.state.anki_repository
    job = repository.create_job(
        CreateCurationJob(
            lecture_id=lecture_id,
            block_id=None,
            source_revision_ids=(revision_id,),
            deck_allowlist=("Deck",),
            tag_allowlist=("Tag",),
            instruction_text="review",
            target_deck="Deck",
            target_tag="Tag",
            index_snapshot_id="snapshot",
            lcl_prompt_version="lcl-v1",
            judgment_rubric_version="judgment-v1",
            gap_prompt_version="gap-v1",
            provider="openai",
            model="model",
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
        )
    )
    context = _phase_g_context()
    context.job.id = str(job.id)
    context.prior_payloads[CurationStage.V3_R1_SOURCE_INDEX]["raw_source"] = "must-not-leak"
    context.prior_payloads[CurationStage.V3_R1_SOURCE_INDEX] = _seal(
        {
            key: value
            for key, value in context.prior_payloads[CurationStage.V3_R1_SOURCE_INDEX].items()
            if key != "artifact_sha256"
        }
    )
    context.prior_payloads[CurationStage.V3_R5_RETRIEVAL].update(effective_tag_mode="hard_filter")
    context.prior_payloads[CurationStage.V3_R5_RETRIEVAL] = _seal(
        {
            key: value
            for key, value in context.prior_payloads[CurationStage.V3_R5_RETRIEVAL].items()
            if key != "artifact_sha256"
        }
    )
    context.prior_payloads[CurationStage.V3_R6_CALIBRATION]["records"][0]["exact_only"] = True
    context.prior_payloads[CurationStage.V3_R6_CALIBRATION]["records"][0]["query_diagnostics"] = [
        {"polluted": True}
    ]
    context.prior_payloads[CurationStage.V3_R6_CALIBRATION] = _seal(
        {
            key: value
            for key, value in context.prior_payloads[CurationStage.V3_R6_CALIBRATION].items()
            if key != "artifact_sha256"
        }
    )
    context.prior_payloads[CurationStage.V3_R8_GAP_CONFIRMATION]["records"][1].update(
        state="unresolved", reason="manual r8 unresolved"
    )
    context.prior_payloads[CurationStage.V3_R8_GAP_CONFIRMATION] = _seal(
        {
            key: value
            for key, value in context.prior_payloads[CurationStage.V3_R8_GAP_CONFIRMATION].items()
            if key != "artifact_sha256"
        }
    )
    context.prior_payloads[CurationStage.V3_R9_GENERATION]["resolutions"].append(
        {"fact_id": "residual", "status": "unresolved", "reason": "manual r9 unresolved"}
    )
    context.prior_payloads[CurationStage.V3_R9_GENERATION] = _seal(
        {
            key: value
            for key, value in context.prior_payloads[CurationStage.V3_R9_GENERATION].items()
            if key != "artifact_sha256"
        }
    )
    context.prior_payloads[CurationStage.V3_R10_DEDUPE]["resolutions"].append(
        {"fact_id": "residual", "status": "unresolved", "reason": "manual r10 unresolved"}
    )
    context.prior_payloads[CurationStage.V3_R7_CLASSIFICATION].update(
        calls=[{"tier": "cheap"}, {"tier": "thorough"}],
        escalations=[{"bundle_id": "initial:1", "reason": "low_confidence"}],
        raw_provider_response="must-not-leak-r7",
    )
    context.prior_payloads[CurationStage.V3_R7_CLASSIFICATION] = _seal(
        {
            key: value
            for key, value in context.prior_payloads[CurationStage.V3_R7_CLASSIFICATION].items()
            if key != "artifact_sha256"
        }
    )
    estimator = CostEstimator(
        FrozenRateTable.from_document(
            context.prior_payloads[CurationStage.V3_R0_PREFLIGHT]["rate_table"]
        )
    )
    usage = TokenUsage(input_tokens=1)
    ledger = [
        CostLedgerEntry(
            call_id="1" * 64,
            stage="R7",
            modality="structured",
            model="fixture",
            request_sha256="2" * 64,
            rate_table_sha256=estimator.rate_table.rate_table_sha256,
            estimator_version=estimator.version,
            predicted=estimator.estimate(CostKind.PREDICTED, model="fixture", usage=usage),
            reserved=estimator.estimate(CostKind.RESERVED, model="fixture", usage=usage),
            observed=estimator.estimate(CostKind.OBSERVED, model="fixture", usage=usage),
        ).document()
    ]
    r10 = {
        key: value
        for key, value in context.prior_payloads[CurationStage.V3_R10_DEDUPE].items()
        if key != "artifact_sha256"
    }
    r10["cost_ledger"] = ledger
    r10["cost_ledger_sha256"] = hashlib.sha256(
        json.dumps(ledger, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    context.prior_payloads[CurationStage.V3_R10_DEDUPE] = _seal(r10)
    product = asyncio.run(_offline_runner().run(context))
    with app.state.database.session() as session:
        stored = session.get(AnkiCurationJobModel, str(job.id))
        assert stored is not None
        stored.pipeline_contract_version = PipelineContractVersion.CARD_CENTRIC_V3.value
        stored.state = CurationState.READY_FOR_REVIEW.value
        stored.policy_sha256 = product.payload["policy_sha256"]
        rate_table = context.prior_payloads[CurationStage.V3_R0_PREFLIGHT]["rate_table"]
        stored.v3_rate_table_json = json.dumps(rate_table, sort_keys=True, separators=(",", ":"))
        stored.v3_rate_table_sha256 = FrozenRateTable.from_document(rate_table).rate_table_sha256
    app.state.anki_curation_pipeline = SimpleNamespace(
        artifacts=StageArtifactStore(app.state.settings.data_dir / "v3-review-artifacts")
    )
    artifact = app.state.anki_curation_pipeline.artifacts.write(
        job.id,
        CurationStage.V3_R11_REVIEW,
        StageProduct(kind=product.kind, payload=product.payload),
        input_sha256="a" * 64,
        pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V3,
        model_config_sha256=repository.require_job(job.id).model_config_sha256,
    )
    repository.save_stage_artifact(job.id, artifact)

    class NeverGateway:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"gateway must not be reached: {name}")

    gateway = app.state.anki_runtime.gateway
    app.state.anki_runtime.gateway = NeverGateway()
    response = client.get(f"/api/anki/jobs/{job.id}/review")
    tag_patch = client.put(
        f"/api/anki/jobs/{job.id}/review",
        json={
            "expected_revision": 0,
            "candidate_selections": {},
            "gap_edits": [],
            "tag_patches": [
                {
                    "note_id": 1,
                    "before": ["OMS::Existing"],
                    "after": ["OMS::Updated"],
                    "add_tags": ["OMS::Updated"],
                    "remove_tags": ["OMS::Existing"],
                    "expected_tag_hash": "a" * 64,
                    "tag_policy_version": "tags-v1",
                }
            ],
        },
    )

    assert response.status_code == 200
    assert tag_patch.status_code == 409
    assert "does not support tag patches" in tag_patch.json()["detail"]
    payload = response.json()
    v3 = payload["review_surface"]["v3"]
    assert set(payload["reconciliation"]) == {
        "contract_version",
        "review_revision",
        "approval_only",
        "approval_state",
        "r11_artifact_sha256",
        "r11_snapshot_sha256",
        "cost_ledger_sha256",
        "existing_note_ids",
        "generated_card_ids",
        "selected_existing_note_ids",
        "selected_generated_card_ids",
    }
    assert payload["reconciliation"]["approval_only"] is True
    assert set(v3) == {
        "approval_only",
        "reason",
        "phase_g_safety",
        "policy",
        "scope",
        "retrieval",
        "evidence",
        "classification",
        "selected_existing_note_ids",
        "selected_generated_card_ids",
        "cost",
        "resolution",
    }
    assert v3["approval_only"] is True and payload["can_build_envelope"] is False
    assert v3["policy"]["enforcement"] == {"present": True, "tier": "professor"}
    assert v3["scope"]["fact_ids"] == ["duplicate", "initial", "residual", "split"]
    assert v3["scope"]["sources"] == [
        {
            "evidence_id": "e1",
            "source_id": "slides",
            "revision_id": 1,
            "source_kind": "slide",
            "locator": "slide:1",
        }
    ]
    assert v3["retrieval"] == {
        "effective_tag_mode": "hard_filter",
        "exact_only_fact_ids": ["initial"],
        "polluted_fact_ids": ["initial"],
    }
    assert v3["evidence"] == {
        "grounding": "cited",
        "degraded_mode": "none",
        "generated_grounding": [
            {"card_id": "card:duplicate:1", "fact_id": "duplicate", "evidence_ids": ["e1"]},
            {"card_id": "card:split:1", "fact_id": "split", "evidence_ids": ["e1"]},
            {"card_id": "card:split:2", "fact_id": "split", "evidence_ids": ["e1"]},
        ],
    }
    assert v3["classification"] == {
        "tiers": ["cheap", "thorough"],
        "escalations": [{"bundle_id": "initial:1", "reason": "low_confidence", "reasons": None}],
    }
    expected_usage = {
        "input_tokens": 1,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "embedding_tokens": 0,
    }
    assert v3["cost"]["calls"][0] == {
        "stage": "R7",
        "call_id": "1" * 64,
        "modality": "structured",
        "model": "fixture",
        "predicted": {"kind": "predicted", "microusd": 1, "usage": expected_usage},
        "reserved": {"kind": "reserved", "microusd": 1, "usage": expected_usage},
        "observed": {"kind": "observed", "microusd": 1, "usage": expected_usage},
        "observed_estimated": False,
    }
    assert v3["resolution"] == {
        "duplicate_fact_ids": ["duplicate", "split"],
        "unresolved": [
            {
                "source": "r8",
                "fact_id": "residual",
                "state": "unresolved",
                "reason": "manual r8 unresolved",
            },
            {
                "source": "r9",
                "fact_id": "residual",
                "state": "unresolved",
                "reason": "manual r9 unresolved",
            },
            {
                "source": "r10",
                "fact_id": "residual",
                "state": "unresolved",
                "reason": "manual r10 unresolved",
            },
            {
                "source": "reconciliation",
                "fact_id": "residual",
                "state": "finding",
                "reason": "residual: R8 is unresolved or incomplete",
            },
        ],
    }
    assert "must-not-leak" not in str(payload)

    _persist_r11_snapshot(repository, job, product)
    before_candidates = [
        (item.note_id, item.selected) for item in repository.list_candidates(job.id)
    ]
    before_cards = [
        (item.card_id, item.selected, item.revision) for item in repository.list_gap_cards(job.id)
    ]
    with app.state.database.engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM anki_reviewed_reconciliations "
                "WHERE job_id = :job_id AND review_revision = :review_revision"
            ),
            {"job_id": str(job.id), "review_revision": 1},
        )
    missing_current = client.get(f"/api/anki/jobs/{job.id}/review")
    missing_current_save = client.put(
        f"/api/anki/jobs/{job.id}/review",
        json={
            "expected_revision": 1,
            "candidate_selections": {},
            "gap_edits": [],
            "tag_patches": [],
        },
    )
    assert missing_current.status_code == 409
    assert missing_current_save.status_code == 409
    assert repository.require_job(job.id).review_revision == 1
    assert [(item.note_id, item.selected) for item in repository.list_candidates(job.id)] == (
        before_candidates
    )
    assert [
        (item.card_id, item.selected, item.revision) for item in repository.list_gap_cards(job.id)
    ] == before_cards

    legacy = repository.create_job(
        CreateCurationJob(
            lecture_id=lecture_id,
            block_id=None,
            source_revision_ids=(revision_id,),
            deck_allowlist=("Deck",),
            tag_allowlist=("Tag",),
            instruction_text="review",
            target_deck="Deck",
            target_tag="Tag",
            index_snapshot_id="snapshot",
            lcl_prompt_version="lcl-v1",
            judgment_rubric_version="judgment-v1",
            gap_prompt_version="gap-v1",
            provider="openai",
            model="model",
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
        )
    )
    with app.state.database.session() as session:
        stored = session.get(AnkiCurationJobModel, str(legacy.id))
        assert stored is not None
        stored.state = CurationState.READY_FOR_REVIEW.value
    app.state.anki_runtime.gateway = gateway
    legacy_surface = client.get(f"/api/anki/jobs/{legacy.id}/review").json()["review_surface"]
    assert set(legacy_surface) == {
        "evidence_quality",
        "s2b_diagnostic",
        "selection",
        "duplicate_resolutions",
    }
    with app.state.database.session() as session:
        stored = session.get(AnkiCurationJobModel, str(legacy.id))
        assert stored is not None
        stored.pipeline_contract_version = PipelineContractVersion.CARD_CENTRIC_V3.value
        rate_table = context.prior_payloads[CurationStage.V3_R0_PREFLIGHT]["rate_table"]
        stored.v3_rate_table_json = json.dumps(rate_table, sort_keys=True, separators=(",", ":"))
        stored.v3_rate_table_sha256 = FrozenRateTable.from_document(rate_table).rate_table_sha256
    app.state.anki_runtime.gateway = NeverGateway()
    missing_r11 = client.get(f"/api/anki/jobs/{legacy.id}/review")
    missing_r11_save = client.put(
        f"/api/anki/jobs/{legacy.id}/review",
        json={
            "expected_revision": 0,
            "candidate_selections": {},
            "gap_edits": [],
            "tag_patches": [],
        },
    )
    assert missing_r11.status_code == 409
    assert "requires a committed R11 snapshot" in missing_r11.json()["detail"]
    assert missing_r11_save.status_code == 409
    assert "requires a committed R11 snapshot" in missing_r11_save.json()["detail"]
