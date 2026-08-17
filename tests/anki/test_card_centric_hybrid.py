import asyncio
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

import oms_hub.anki.stages as stages
from oms_hub.anki.calibration import (
    canonical_sha256,
    frozen_config_payload,
)
from oms_hub.anki.card_centric_contracts import CensusTrust, SnapshotCensus
from oms_hub.anki.card_centric_hybrid import query_variants
from oms_hub.anki.course_policy import CourseCurationPolicy
from oms_hub.anki.domain import CurationStage, PipelineContractVersion
from oms_hub.anki.index import SearchHit
from oms_hub.anki.normalize import NormalizedNote, semantic_text
from oms_hub.anki.pipeline import PinnedInputChanged
from oms_hub.anki.retrieval import hybrid_rank_fusion
from oms_hub.anki.scope_contracts import (
    LectureScope,
    ScopedConcept,
    ScopedFact,
    ScopeEvidenceReference,
)
from oms_hub.anki.semantic.domain import SemanticHit
from oms_hub.anki.semantic.service import content_hash
from oms_hub.anki.stages import CurationServicesRunner


def test_variants_dedupe_and_cap_deterministically() -> None:
    variants, trace = query_variants(
        fact_statement="Fact",
        canonical_statement="Canonical",
        primary_entity="Entity",
        aliases=("Alias",),
        exact_terms=("Rare Eponym",),
        professor_policy_basis=("B", "A", "C"),
        retrieval_queries=("Q2", "Q1", "Q3"),
    )
    assert variants == ("Rare Eponym", "Entity Alias", "Fact", "Canonical", "A", "B", "Q1", "Q2")
    assert all("query" in entry for entry in trace)


def test_two_level_rrf_is_stable() -> None:
    rows = hybrid_rank_fusion(
        {"one": (2, 1), "two": (1,)}, (2,), variant_weights={"one": 1.0, "two": 0.9}
    )
    assert [row.note_id for row in rows] == [2, 1]


class _FakeCompanion:
    def __init__(self, notes: tuple[NormalizedNote, ...], *, lexical_ids: tuple[int, ...]) -> None:
        self.notes = {note.note_id: note for note in notes}
        self.note = notes[0]
        self.current_snapshot = "companion-1"
        self.live_ids = set(self.notes)
        self.lexical_ids = lexical_ids
        self.fts_calls: list[tuple[str, int]] = []

    def snapshot_id(self) -> str:
        return self.current_snapshot

    def eligible_note_ids(self, _filters: object) -> set[int]:
        return set(self.live_ids)

    def get_note(self, note_id: int) -> NormalizedNote | None:
        return self.notes.get(note_id)

    def search_fts(self, query: str, *, filters: object, limit: int) -> list[SearchHit]:
        del filters
        self.fts_calls.append((query, limit))
        return [
            SearchHit(note_id=note_id, score=1.0 - index / 100)
            for index, note_id in enumerate(self.lexical_ids[:limit], start=1)
            if note_id in self.live_ids
        ]


class _FakeSemantic:
    def __init__(
        self,
        notes: tuple[NormalizedNote, ...],
        *,
        hit_lists: tuple[tuple[tuple[int, float], ...], ...],
        covered_note_ids: tuple[int, ...] | None = None,
    ) -> None:
        self.semantic_hash = content_hash(semantic_text(notes[0]))
        self.semantic_hashes = {note.note_id: content_hash(semantic_text(note)) for note in notes}
        self.search_calls = 0
        self.pinned_calls = 0
        self.search_requests: list[tuple[tuple[str, ...], int]] = []
        self.semantic_eligible_requests: list[set[int]] = []
        self.pinned_requests: list[tuple[int, ...]] = []
        self.model = "fake-model"
        self.dimensions = 2
        self.hit_hash = self.semantic_hash
        self.hit_lists = hit_lists
        self.vectors = {note.note_id: (1.0, 0.0) for note in notes}
        manifest = SimpleNamespace(
            generation="semantic-1",
            model=self.model,
            dimensions=self.dimensions,
            matrix_sha256="m" * 64,
            note_ids=covered_note_ids or tuple(note.note_id for note in notes),
            content_hashes=tuple(
                self.semantic_hashes[note_id]
                for note_id in (covered_note_ids or tuple(note.note_id for note in notes))
            ),
        )
        self.snapshot = SimpleNamespace(manifest=manifest)
        self.store = SimpleNamespace(load=lambda **_kwargs: self.snapshot)

    async def search(self, queries: tuple[str, ...], **_kwargs: object) -> list[list[SemanticHit]]:
        self.search_calls += 1
        limit = int(_kwargs["limit"])
        eligible = set(_kwargs["eligible_note_ids"])
        if not eligible.issubset(self.snapshot.manifest.note_ids):
            raise AssertionError("semantic search received an uncovered note")
        self.search_requests.append((queries, limit))
        self.semantic_eligible_requests.append(eligible)
        return [
            [
                SemanticHit(
                    note_id,
                    score,
                    self.hit_hash if note_id == 1 else self.semantic_hashes[note_id],
                )
                for note_id, score in self.hit_lists[index % len(self.hit_lists)][:limit]
                if note_id in eligible
            ]
            for index, _query in enumerate(queries)
        ]

    async def pinned_document_vectors(self, **_kwargs: object) -> dict[int, tuple[float, float]]:
        self.pinned_calls += 1
        note_ids = tuple(_kwargs["note_ids"])
        self.pinned_requests.append(note_ids)
        return {
            note_id: self.vectors[note_id]
            for note_id in note_ids
            if note_id in self.snapshot.manifest.note_ids
        }


def _r5_fixture(
    *,
    requested: str = "hard_filter",
    trusted: bool = True,
    polluted: bool = False,
    partial_semantic_coverage: bool = False,
) -> tuple[CurationServicesRunner, SimpleNamespace, _FakeCompanion, _FakeSemantic]:
    target = NormalizedNote(
        note_id=1,
        model_name="Basic",
        text="Rare Eponym is exact",
        extra="",
        raw_fields={},
        tags=("#target",),
        card_ids=(1,),
        media=(),
        token_signature="one",
        content_sha256="c" * 64,
        deck_names=("Deck",),
    )
    notes = (target,)
    lexical_ids = (1,)
    hit_lists = (((1, 0.9),),)
    if polluted:
        notes = (
            target,
            NormalizedNote(
                note_id=2,
                model_name="Basic",
                text="Related but untagged",
                extra="",
                raw_fields={},
                tags=(),
                card_ids=(2,),
                media=(),
                token_signature="two",
                content_sha256="d" * 64,
                deck_names=("Deck",),
            ),
            NormalizedNote(
                note_id=3,
                model_name="Basic",
                text="Other system related card",
                extra="",
                raw_fields={},
                tags=("#other::system",),
                card_ids=(3,),
                media=(),
                token_signature="three",
                content_sha256="e" * 64,
                deck_names=("Deck",),
            ),
        )
        lexical_ids = (1, 2, 3)
        hit_lists = (((1, 0.9), (2, 0.8), (3, 0.7)), ((1, 0.95),))
        if trusted:
            notes += tuple(
                NormalizedNote(
                    note_id=note_id,
                    model_name="Basic",
                    text=f"Target filler {note_id}",
                    extra="",
                    raw_fields={},
                    tags=("#target",),
                    card_ids=(note_id,),
                    media=(),
                    token_signature=str(note_id),
                    content_sha256=f"{note_id:064x}",
                    deck_names=("Deck",),
                )
                for note_id in range(4, 101)
            )
    if partial_semantic_coverage:
        notes += (
            NormalizedNote(
                note_id=2,
                model_name="Basic",
                text="Rare Eponym lexical-only recovery",
                extra="",
                raw_fields={},
                tags=("#target",),
                card_ids=(2,),
                media=(),
                token_signature="two",
                content_sha256="d" * 64,
                deck_names=("Deck",),
            ),
        )
        lexical_ids = (2,)
    companion, semantic = _FakeCompanion(notes, lexical_ids=lexical_ids), _FakeSemantic(
        notes,
        hit_lists=hit_lists,
        covered_note_ids=(1,) if partial_semantic_coverage else None,
    )
    policy = CourseCurationPolicy(
        policy_id="policy",
        revision=1,
        course_id="course",
        professor_label="prof",
        scope_instruction="scope",
        emphasis_mode="transcript_emphasis",
        missing_emphasis_fallback="transcript_outline",
        tag_scope_mode=requested,
        classification_strictness="strict",
        generation_style_profile="basic",
        ordinary_cost_limit_microusd=1,
        hard_stop_cost_limit_microusd=1,
    )
    evidence = ScopeEvidenceReference(
        evidence_id="evidence",
        source_id="source",
        locator="locator",
        content_sha256="a" * 64,
    )
    scope = LectureScope(
        scope_id="scope",
        policy_sha256=policy.policy_sha256,
        source_bundle_sha256="b" * 64,
        degraded_mode="none",
        evidence=(evidence,),
        concepts=(
            ScopedConcept(
                concept_id="concept",
                canonical_statement="Canonical",
                primary_entity="Entity",
                aliases=("Alias",),
                exact_terms=("Rare Eponym",),
                depth_tier=1,
                priority=1,
                reason="reason",
                facts=(
                    ScopedFact(
                        fact_id="fact",
                        statement="Fact",
                        evidence_ids=("evidence",),
                        generation_allowed=True,
                    ),
                ),
                source_evidence_ids=("evidence",),
                professor_policy_basis=("Policy",),
                retrieval_queries=("Query",),
            ),
        ),
    )
    mapping = {1: "target_tagged"}
    if polluted:
        mapping.update({2: "untagged", 3: "other_system_excluded"})
        mapping.update({note.note_id: "target_tagged" for note in notes[3:]})
    elif partial_semantic_coverage:
        mapping[2] = "target_tagged"
    census = SnapshotCensus(
        snapshot_id="companion-1",
        denominator_count=len(notes),
        tagged_count=len(notes) - 2 if polluted else len(notes),
        other_system_tagged_count=1 if polluted else 0,
        untagged_count=1 if polluted else 0,
        deck_excluded_count=0,
        excluded_count=1 if polluted else 0,
        mapping=mapping,
        filters_sha256="d" * 64,
        trust=CensusTrust(
            decision="trusted" if trusted else "blocked",
            reason="trusted" if trusted else "blocked",
            untagged_rate=1 / len(notes) if polluted else 0.0,
            safe_untagged_rate=0.03,
        ),
    )
    cards = [{"note_id": note.note_id, "content_sha256": note.content_sha256} for note in notes]
    semantic_ids = [
        {"note_id": note.note_id, "semantic_content_sha256": semantic.semantic_hashes[note.note_id]}
        for note in notes
        if note.note_id in semantic.snapshot.manifest.note_ids
    ]
    manifest = {
        "generation": "semantic-1",
        "model": "fake-model",
        "dimensions": 2,
        "matrix_sha256": "m" * 64,
    }
    r4 = {
        "kind": "card_centric_v3_index_verification",
        "policy_sha256": policy.policy_sha256,
        "companion_generation": "companion-1",
        "lexical_generation": "companion-1",
        "semantic_generation": "semantic-1",
        "deck_allowlist": ["Deck"],
        "tag_allowlist": ["#target"],
        "card_identities": cards,
        "cards_sha256": canonical_sha256(cards),
        "semantic_identities": semantic_ids,
        "semantic_manifest": manifest,
        "semantic_manifest_sha256": canonical_sha256(manifest),
        "census": census.model_dump(mode="json"),
        "census_sha256": canonical_sha256(census.model_dump(mode="json")),
    }
    r4["verification_sha256"] = canonical_sha256(r4)
    job = SimpleNamespace(
        pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V3,
        policy_sha256=policy.policy_sha256,
        model_config_sha256="z" * 64,
        companion_generation="companion-1",
        semantic_generation="semantic-1",
        deck_allowlist=("Deck",),
        tag_allowlist=("#target",),
    )
    context = SimpleNamespace(
        job=job,
        prior_payloads={
            CurationStage.V3_R0_PREFLIGHT: {
                "policy": policy.model_dump(mode="json"),
                "policy_sha256": policy.policy_sha256,
                "policy_revision": policy.revision,
                "model_config_sha256": "z" * 64,
            },
            CurationStage.V3_R3_SCOPE: {"scope": scope.model_dump(mode="json")},
            CurationStage.V3_R4_INDEX_VERIFICATION: r4,
        },
        stage=CurationStage.V3_R5_RETRIEVAL,
    )
    runner = object.__new__(CurationServicesRunner)
    runner.companion, runner.semantic = companion, semantic
    return runner, context, companion, semantic


def _stage_config(**overrides: object) -> dict[str, object]:
    config = frozen_config_payload()
    config.update(overrides)
    config["rrf_floor"] = 1 / (int(config["rrf_k"]) + int(config["raw_limit"]))
    return config


def _run_r5_r6(
    runner: CurationServicesRunner, context: SimpleNamespace
) -> tuple[dict[str, object], dict[str, object]]:
    r5 = asyncio.run(runner._v3_r5_retrieval(context)).payload
    context.prior_payloads[CurationStage.V3_R5_RETRIEVAL] = r5
    context.stage = CurationStage.V3_R6_CALIBRATION
    return r5, asyncio.run(runner._v3_r6_calibration(context)).payload


@pytest.mark.parametrize(
    "mutation",
    (
        lambda _runner, context, _companion, _semantic: setattr(
            context.job, "policy_sha256", "f" * 64
        ),
        lambda _runner, context, _companion, _semantic: context.prior_payloads[
            CurationStage.V3_R3_SCOPE
        ]["scope"].__setitem__("policy_sha256", "f" * 64),
        lambda _runner, context, _companion, _semantic: setattr(
            context.job, "semantic_generation", "other"
        ),
        lambda _runner, context, _companion, _semantic: setattr(
            context.job, "companion_generation", "other"
        ),
        lambda _runner, context, _companion, _semantic: setattr(
            context.job, "deck_allowlist", ("Other",)
        ),
        lambda _runner, context, _companion, _semantic: setattr(
            context.job, "tag_allowlist", ("#other",)
        ),
        lambda _runner, context, _companion, _semantic: context.prior_payloads[
            CurationStage.V3_R4_INDEX_VERIFICATION
        ].__setitem__("kind", "wrong"),
        lambda _runner, context, _companion, _semantic: context.prior_payloads[
            CurationStage.V3_R4_INDEX_VERIFICATION
        ].__setitem__("cards_sha256", "0" * 64),
        lambda _runner, context, _companion, _semantic: context.prior_payloads[
            CurationStage.V3_R4_INDEX_VERIFICATION
        ]["semantic_identities"].append({"note_id": 2, "semantic_content_sha256": "e" * 64}),
        lambda _runner, context, _companion, _semantic: context.prior_payloads[
            CurationStage.V3_R4_INDEX_VERIFICATION
        ]["card_identities"].append({"note_id": 1, "content_sha256": "c" * 64}),
        lambda _runner, context, _companion, _semantic: context.prior_payloads[
            CurationStage.V3_R4_INDEX_VERIFICATION
        ]["semantic_identities"].append({"note_id": 1, "semantic_content_sha256": "s" * 64}),
        lambda _runner, context, _companion, _semantic: context.prior_payloads[
            CurationStage.V3_R4_INDEX_VERIFICATION
        ].__setitem__("lexical_generation", "other"),
        lambda _runner, context, _companion, _semantic: context.prior_payloads[
            CurationStage.V3_R4_INDEX_VERIFICATION
        ].__setitem__("semantic_manifest_sha256", "0" * 64),
        lambda _runner, context, _companion, _semantic: context.prior_payloads[
            CurationStage.V3_R4_INDEX_VERIFICATION
        ].__setitem__("census_sha256", "0" * 64),
        lambda _runner, context, _companion, _semantic: context.prior_payloads[
            CurationStage.V3_R4_INDEX_VERIFICATION
        ].__setitem__("verification_sha256", "0" * 64),
        lambda _runner, _context, companion, _semantic: setattr(
            companion, "current_snapshot", "other"
        ),
        lambda _runner, _context, companion, _semantic: companion.live_ids.add(2),
        lambda _runner, _context, companion, _semantic: object.__setattr__(
            companion.note, "content_sha256", "f" * 64
        ),
        lambda _runner, context, _companion, _semantic: context.prior_payloads[
            CurationStage.V3_R4_INDEX_VERIFICATION
        ]["census"].__setitem__("mapping", {}),
        lambda _runner, _context, _companion, semantic: setattr(
            semantic.snapshot,
            "manifest",
            SimpleNamespace(
                generation="other",
                model="fake-model",
                dimensions=2,
                matrix_sha256="m" * 64,
                note_ids=(1,),
                content_hashes=(semantic.semantic_hash,),
            ),
        ),
    ),
)
def test_r5_rejects_every_pinned_prequery_mismatch_without_embedding(mutation: object) -> None:
    runner, context, companion, semantic = _r5_fixture()
    mutation(runner, context, companion, semantic)
    with pytest.raises((PinnedInputChanged, ValueError)):
        asyncio.run(runner._v3_r5_retrieval(context))
    assert semantic.search_calls == 0


def test_r5_r6_fake_only_success_and_hit_identity_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    runner, context, _companion, semantic = _r5_fixture()
    calls = []

    @contextmanager
    def scope(**kwargs: object):
        calls.append(kwargs)
        yield

    monkeypatch.setattr(stages, "provider_call_scope", scope)
    r5 = asyncio.run(runner.run(context))
    assert r5.kind == "card_centric_v3_retrieval"
    assert semantic.search_calls == 1 and calls == [{"batch_index": 0, "batch_note_ids": ()}]
    context.prior_payloads[CurationStage.V3_R5_RETRIEVAL] = r5.payload
    context.stage = CurationStage.V3_R6_CALIBRATION
    r6 = asyncio.run(runner.run(context))
    assert r6.kind == "card_centric_v3_calibration"
    assert semantic.search_calls == 1 and semantic.pinned_calls == 1 and len(calls) == 1
    context.stage = CurationStage.V3_R4_INDEX_VERIFICATION
    with pytest.raises(KeyError):
        asyncio.run(runner.run(context))

    runner, context, _companion, semantic = _r5_fixture()
    semantic.hit_hash = "f" * 64
    product = asyncio.run(runner._v3_r5_retrieval(context))
    assert product.payload["facts"][0]["candidates"][0]["semantic_rank"] is None
    assert semantic.search_calls == 1


def test_r6_global_cap_keeps_200_and_hashes_final_two_fact_state() -> None:
    runner, context, _companion, semantic = _r5_fixture()
    r5 = asyncio.run(runner._v3_r5_retrieval(context)).payload
    template = r5["facts"][0]["candidates"][0]

    def fact(fact_id: str, start: int) -> dict[str, object]:
        candidates = []
        for offset in range(20):
            candidate = deepcopy(template)
            note_id = offset + 1 if offset < 10 else start + offset - 10
            candidate.update(
                note_id=note_id,
                content_sha256=f"{note_id:064x}",
                semantic_score=None,
                variant_ranks={},
                semantic_rank=None,
                lexical_rank=offset + 1,
                exact_match_reasons=["exact"],
            )
            candidates.append(candidate)
        payload = {
            "concept_id": "concept",
            "fact_id": fact_id,
            "variants": [],
            "query_trace": [],
            "raw_semantic": [],
            "raw_lexical": [],
            "candidates": candidates,
        }
        payload["query_sha256"] = canonical_sha256(payload)
        payload["fact_sha256"] = canonical_sha256(payload)
        return payload

    r5["facts"] = [fact(f"fact-{index}", index * 10 + 11) for index in range(21)]
    r5["artifact_sha256"] = canonical_sha256(
        {key: value for key, value in r5.items() if key != "artifact_sha256"}
    )
    r4 = context.prior_payloads[CurationStage.V3_R4_INDEX_VERIFICATION]
    cards = [{"note_id": note_id, "content_sha256": f"{note_id:064x}"} for note_id in range(1, 221)]
    census = SnapshotCensus(
        snapshot_id="companion-1",
        denominator_count=220,
        tagged_count=220,
        other_system_tagged_count=0,
        untagged_count=0,
        deck_excluded_count=0,
        excluded_count=0,
        mapping={note_id: "target_tagged" for note_id in range(1, 221)},
        filters_sha256="d" * 64,
        trust=CensusTrust(
            decision="trusted", reason="trusted", untagged_rate=0.0, safe_untagged_rate=0.03
        ),
    )
    r4.update(
        card_identities=cards,
        cards_sha256=canonical_sha256(cards),
        semantic_identities=[],
        census=census.model_dump(mode="json"),
        census_sha256=canonical_sha256(census.model_dump(mode="json")),
    )
    r4["verification_sha256"] = canonical_sha256(
        {key: value for key, value in r4.items() if key != "verification_sha256"}
    )
    context.prior_payloads[CurationStage.V3_R5_RETRIEVAL] = r5
    context.stage = CurationStage.V3_R6_CALIBRATION
    before_queries = semantic.search_calls
    r6 = asyncio.run(runner._v3_r6_calibration(context)).payload
    assert semantic.search_calls == before_queries and semantic.pinned_calls == 0
    retained_ids = {
        row["note_id"] for record in r6["records"] for row in record["all_candidates"]
    }
    assert len(retained_ids) == 200
    assert sum(len(record["all_candidates"]) for record in r6["records"]) > 200
    assert sum(len(record["global_cap_exclusions"]) for record in r6["records"]) == 20
    assert all(
        exclusion["reason"] == "global_unique_cap"
        for record in r6["records"]
        for exclusion in record["global_cap_exclusions"]
    )
    assert sum(len(fact_payload["candidates"]) for fact_payload in r5["facts"]) == 420
    for record in r6["records"]:
        assert record["fact_sha256"] == canonical_sha256(
            {key: value for key, value in record.items() if key != "fact_sha256"}
        )
    assert r6["artifact_sha256"] == canonical_sha256(
        {key: value for key, value in r6.items() if key != "artifact_sha256"}
    )


@pytest.mark.parametrize(
    ("requested", "trusted", "effective", "retained_ids", "target_boost"),
    (
        ("hard_filter", True, "hard_filter", {1}, 0.0),
        ("hard_filter", False, "prior_boost", {1, 2, 3}, 0.02),
        ("prior_boost", True, "prior_boost", {1, 2, 3}, 0.02),
        ("disabled", True, "disabled", {1, 2, 3}, 0.0),
    ),
)
def test_r5_r6_tag_pollution_matrix_is_an_actual_stage_artifact(
    monkeypatch: pytest.MonkeyPatch,
    requested: str,
    trusted: bool,
    effective: str,
    retained_ids: set[int],
    target_boost: float,
) -> None:
    runner, context, _companion, _semantic = _r5_fixture(
        requested=requested, trusted=trusted, polluted=True
    )
    config = _stage_config(query_variant_limit=2, pollution_ceiling=3)
    monkeypatch.setattr(stages, "frozen_config_payload", lambda: deepcopy(config))

    r5, r6 = _run_r5_r6(runner, context)

    assert r5["requested_tag_mode"] == requested
    assert r5["effective_tag_mode"] == effective
    assert r5["config_sha256"] == canonical_sha256(config)
    assert r5["artifact_sha256"] == canonical_sha256(
        {key: value for key, value in r5.items() if key != "artifact_sha256"}
    )
    candidates = {row["note_id"]: row for row in r5["facts"][0]["candidates"]}
    assert set(candidates) == {1, 2, 3}
    assert candidates[1]["tags"] == ["#target"]
    assert candidates[2]["tags"] == []
    assert candidates[3]["tags"] == ["#other::system"]
    assert candidates[1]["boost_total"] == target_boost
    assert candidates[2]["boost_total"] == candidates[3]["boost_total"] == 0.0

    record = r6["records"][0]
    polluted, clean = record["query_diagnostics"]
    assert polluted == {
        "variant": "variant_1",
        "raw_semantic_hit_count": 3,
        "raw_lexical_hit_count": 3,
        "above_threshold_count": 3,
        "off_scope_count": 2,
        "ratio": pytest.approx(2 / 3),
        "polluted": True,
        "dominant_pattern": ("Deck", "#other"),
        "semantic_lane_weight": 0.0,
        "fused_candidate_count": 1,
        "exact_only": True,
        "semantic_only": False,
    }
    assert clean == {
        "variant": "variant_2",
        "raw_semantic_hit_count": 1,
        "raw_lexical_hit_count": 3,
        "above_threshold_count": 1,
        "off_scope_count": 0,
        "ratio": 0.0,
        "polluted": False,
        "dominant_pattern": None,
        "semantic_lane_weight": 0.9,
        "fused_candidate_count": 1,
        "exact_only": False,
        "semantic_only": False,
    }
    assert {row["note_id"] for row in record["all_candidates"]} == retained_ids
    assert record["exact_only"] is False and record["semantic_only"] is False
    assert candidates[1]["variant_ranks"] == {"variant_1": 1, "variant_2": 1}
    assert record["all_candidates"][0]["note_id"] == 1
    assert r6["artifact_sha256"] == canonical_sha256(
        {key: value for key, value in r6.items() if key != "artifact_sha256"}
    )


def test_r5_r6_partial_semantic_coverage_keeps_lexical_exact_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, context, companion, semantic = _r5_fixture(partial_semantic_coverage=True)
    calls = []

    @contextmanager
    def scope(**kwargs: object):
        calls.append(kwargs)
        yield

    monkeypatch.setattr(stages, "provider_call_scope", scope)
    r5 = asyncio.run(runner.run(context)).payload
    context.prior_payloads[CurationStage.V3_R5_RETRIEVAL] = r5
    context.stage = CurationStage.V3_R6_CALIBRATION
    r6 = asyncio.run(runner.run(context)).payload

    assert calls == [{"batch_index": 0, "batch_note_ids": ()}]
    assert semantic.search_calls == 1 and semantic.semantic_eligible_requests == [{1}]
    assert companion.fts_calls == [("Rare Eponym", 50)]
    candidates = {row["note_id"]: row for row in r5["facts"][0]["candidates"]}
    assert candidates[2]["semantic_rank"] is None
    assert candidates[2]["lexical_rank"] == 1
    assert candidates[2]["exact_match_reasons"] == ["Rare Eponym"]
    record = r6["records"][0]
    assert {row["note_id"] for row in record["all_candidates"]} == {1, 2}
    assert semantic.pinned_requests == [(1,)]
    clusters = {
        tuple(cluster["sibling_note_ids"]): cluster["missing_vector_note_ids"]
        for cluster in record["clusters"]
    }
    assert clusters[(2,)] == [2]


def test_r6_query_fallback_flags_use_each_variant_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _stage_config(query_variant_limit=3, pollution_ceiling=3)
    monkeypatch.setattr(stages, "frozen_config_payload", lambda: deepcopy(config))

    runner, context, _companion, semantic = _r5_fixture(polluted=True)
    semantic.hit_lists = (((1, 0.9), (2, 0.8), (3, 0.7)), ((1, 0.95),), ())
    _r5, r6 = _run_r5_r6(runner, context)
    assert [
        (entry["fused_candidate_count"], entry["exact_only"], entry["semantic_only"])
        for entry in r6["records"][0]["query_diagnostics"]
    ] == [(1, True, False), (1, False, False), (1, True, False)]

    runner, context, companion, semantic = _r5_fixture(polluted=True)
    companion.lexical_ids = ()
    semantic.hit_lists = (((1, 0.9), (2, 0.8), (3, 0.7)), ((1, 0.95),), ())
    _r5, r6 = _run_r5_r6(runner, context)
    assert [
        (entry["fused_candidate_count"], entry["exact_only"], entry["semantic_only"])
        for entry in r6["records"][0]["query_diagnostics"]
    ] == [(0, False, False), (1, False, True), (0, False, False)]


def test_modified_frozen_config_changes_r5_query_and_raw_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, context, companion, semantic = _r5_fixture(polluted=True)
    config = _stage_config(raw_limit=1, query_variant_limit=1)
    monkeypatch.setattr(stages, "frozen_config_payload", lambda: deepcopy(config))

    r5, r6 = _run_r5_r6(runner, context)

    assert r5["config_sha256"] == r6["config_sha256"] == canonical_sha256(config)
    assert r5["config_sha256"] != canonical_sha256(frozen_config_payload())
    assert semantic.search_requests == [(tuple(r5["facts"][0]["variants"]), 1)]
    assert len(r5["facts"][0]["variants"]) == 1
    assert companion.fts_calls == [("Rare Eponym", 1)]
    assert [row["note_id"] for row in r5["facts"][0]["candidates"]] == [1]
    assert [row["note_id"] for row in r6["records"][0]["all_candidates"]] == [1]


def test_modified_frozen_config_changes_boost_admission_and_cluster_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, context, _companion, semantic = _r5_fixture(requested="prior_boost", polluted=True)
    config = _stage_config(
        query_variant_limit=2,
        pollution_ceiling=99,
        semantic_threshold=0.96,
        boost_parameters={"lecture_tag": 0.4, "block_tag": 0.0, "trusted_source": 0.0, "cap": 0.03},
        cosine_cluster_threshold=0.95,
    )
    semantic.vectors.update({1: (1.0, 0.0), 2: (0.9, 0.435), 3: (0.0, 1.0)})
    _companion.lexical_ids = (1,)
    monkeypatch.setattr(stages, "frozen_config_payload", lambda: deepcopy(config))

    r5, r6 = _run_r5_r6(runner, context)

    assert r5["facts"][0]["candidates"][0]["boost_total"] == 0.03
    assert [row["note_id"] for row in r6["records"][0]["all_candidates"]] == [1]
    assert r6["records"][0]["exact_only"] is True
    assert r6["records"][0]["all_candidates"][0]["disposition"] == "exact_survives"
    assert r6["records"][0]["all_candidates"][0]["clean_semantic_lane"] is False
    assert r6["records"][0]["clusters"] == [
        {"representative_note_id": 1, "sibling_note_ids": [1], "missing_vector_note_ids": []}
    ]

    runner, context, _companion, semantic = _r5_fixture(requested="prior_boost", polluted=True)
    config = _stage_config(
        query_variant_limit=2,
        pollution_ceiling=99,
        cosine_cluster_threshold=0.88,
    )
    semantic.vectors.update({1: (1.0, 0.0), 2: (0.9, 0.435), 3: (0.0, 1.0)})
    monkeypatch.setattr(stages, "frozen_config_payload", lambda: deepcopy(config))
    _r5, clustered = _run_r5_r6(runner, context)
    assert clustered["config_sha256"] == canonical_sha256(config)
    assert clustered["records"][0]["clusters"][0]["sibling_note_ids"] == [1, 2]
