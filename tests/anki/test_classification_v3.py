import asyncio
import json
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest

from oms_hub.anki import classification_v3
from oms_hub.anki.classification_v3 import (
    CHEAP_INSTRUCTION,
    REPAIR_INSTRUCTION,
    SET_COVERAGE_INSTRUCTION,
    THOROUGH_INSTRUCTION,
    ProviderSetCoverageRow,
    R7ClassificationService,
    _provider_input,
    _valid_repair_authorization,
    classify_set_coverage,
    r7_pin_document,
)
from oms_hub.anki.contracts import canonical_payload_sha256
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
    CurationStage,
    PipelineContractVersion,
    ResolvedModelConfiguration,
    ResolvedStageModel,
)
from oms_hub.anki.evidence_bundle import (
    CandidateCardFields,
    CandidateEvidenceBundle,
    SelectedPassage,
)
from oms_hub.anki.pipeline import PinnedInputChanged, pipeline_stages
from oms_hub.anki.provider_attempts import (
    ProviderAttemptBinding,
    bind_provider_attempts,
    provider_cost_reservation,
)
from oms_hub.anki.scope_contracts import (
    LectureScope,
    ScopedConcept,
    ScopedFact,
    ScopeEvidenceReference,
)
from oms_hub.anki.stages import CurationServicesRunner
from oms_hub.llm.domain import GeneratedText, ProviderName
from oms_hub.llm.structured import StructuredTextService


class FakeGenerator:
    offline_replay_only = True

    def __init__(self, responses: list[object]) -> None:
        self.responses = iter(responses)
        self.calls: list[dict[str, object]] = []

    def generate_text(self, instruction: str, input_text: str, **kwargs: object) -> GeneratedText:
        self.calls.append({"instruction": instruction, "input": json.loads(input_text), **kwargs})
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return GeneratedText(
            text=response if isinstance(response, str) else json.dumps(response),
            provider=ProviderName.OPENAI,
            model="fake",
            request_id=f"request-{len(self.calls)}",
            input_tokens=10,
            output_tokens=2,
            cost_microusd=3,
        )


def _bundle(note_id: int, fact_id: str, statement: str = "Fact") -> CandidateEvidenceBundle:
    fact = ScopedFact(
        fact_id=fact_id,
        statement=statement,
        evidence_ids=("passage-1",),
        generation_allowed=True,
    )
    concept = ScopedConcept(
        concept_id="concept-1",
        canonical_statement="Concept",
        primary_entity="Entity",
        depth_tier=1,
        priority=1,
        reason="reason",
        facts=(fact,),
        source_evidence_ids=("passage-1",),
        retrieval_queries=("query",),
    )
    seed: dict[str, object] = {
        "bundle_id": f"bundle:{fact_id}:{note_id}",
        "policy_sha256": "a" * 64,
        "scope_sha256": "b" * 64,
        "concept": concept,
        "fact_id": fact_id,
        "candidate": CandidateCardFields(
            candidate_id=f"note:{note_id}", note_id=note_id, text="front", extra="", deck="Deck"
        ),
        "selected_passages": (
            SelectedPassage(
                passage_id="passage-1", text="evidence", selection_reason="fact_scope_evidence"
            ),
        ),
        "allowed_concept_ids": ("concept-1",),
        "allowed_fact_ids": (fact_id,),
        "allowed_passage_ids": ("passage-1",),
        "input_byte_estimate": 0,
        "input_token_estimate": 0,
        "max_input_bytes": 16_384,
        "max_input_tokens": 16_384,
        "truncated": False,
        "degraded": False,
    }
    estimate = 0
    for _ in range(8):
        seed["input_byte_estimate"] = estimate
        seed["input_token_estimate"] = estimate
        provisional = CandidateEvidenceBundle.model_construct(**seed)
        actual = len(
            json.dumps(
                provisional.canonical_payload(), sort_keys=True, separators=(",", ":")
            ).encode()
        )
        if actual == estimate:
            return CandidateEvidenceBundle.model_validate(seed)
        estimate = actual
    raise AssertionError("bundle estimate did not stabilize")


def _row(bundle: CandidateEvidenceBundle, disposition: str, confidence: int) -> dict[str, object]:
    return {
        "bundle_id": bundle.bundle_id,
        "candidate_id": bundle.candidate.candidate_id,
        "disposition": disposition,
        "confidence_bps": confidence,
        "supporting_passage_ids": ["passage-1"],
        "conflicting_passage_ids": [],
        "redundant_with_candidate_id": None,
        "reason": "supported",
    }


def _routes() -> tuple[ResolvedStageModel, ResolvedStageModel]:
    return (
        ResolvedStageModel("openai", "cheap", thinking_mode="disabled"),
        ResolvedStageModel("openai", "thorough", thinking_mode="disabled"),
    )


@pytest.mark.parametrize(
    "instruction", (CHEAP_INSTRUCTION, THOROUGH_INSTRUCTION, REPAIR_INSTRUCTION)
)
def test_r7_instructions_require_full_candidate_fact_coverage(instruction: str) -> None:
    assert "exact target fact, not the broader concept" in instruction
    assert (
        "Candidate text and extra must themselves fully state every material claim" in instruction
    )
    assert "cannot fill content missing from the candidate" in instruction


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
        cost_ledger_sha256=sha256(b"[]").hexdigest(),
    )


def _empty_ledger(payload: dict[str, object]) -> None:
    payload["cost_ledger"] = []
    payload["cost_ledger_sha256"] = sha256(b"[]").hexdigest()


def _stage_fixture() -> tuple[CurationServicesRunner, SimpleNamespace, FakeGenerator]:
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
        ordinary_cost_limit_microusd=100,
        hard_stop_cost_limit_microusd=200,
    )
    texts = {"passage-1": "first fact evidence", "passage-2": "second fact evidence"}
    evidence = tuple(
        ScopeEvidenceReference(
            evidence_id=evidence_id,
            source_id="source",
            locator=f"slide:{index}",
            content_sha256=sha256(text.encode()).hexdigest(),
        )
        for index, (evidence_id, text) in enumerate(texts.items(), start=1)
    )
    facts = tuple(
        ScopedFact(
            fact_id=f"fact-{index}",
            statement=f"Fact {index}",
            evidence_ids=(f"passage-{index}",),
            generation_allowed=True,
        )
        for index in (1, 2)
    )
    concept = ScopedConcept(
        concept_id="concept-1",
        canonical_statement="Concept",
        primary_entity="Entity",
        depth_tier=1,
        priority=1,
        reason="reason",
        facts=facts,
        source_evidence_ids=("passage-1", "passage-2"),
        retrieval_queries=("query",),
    )
    scope = LectureScope(
        scope_id="scope",
        policy_sha256=policy.policy_sha256,
        source_bundle_sha256="0" * 64,
        degraded_mode="none",
        evidence=evidence,
        concepts=(concept,),
    )
    source_bundle = {
        "serialization_version": "scope-source-bundle-v1",
        "degraded_mode": "none",
        "evidence": [
            {
                "evidence_type": "transcript",
                "evidence_id": evidence_id,
                "source_id": "source",
                "locator": f"slide:{index}",
                "normalized_text": text,
                "content_sha256": sha256(text.encode()).hexdigest(),
            }
            for index, (evidence_id, text) in enumerate(texts.items(), start=1)
        ],
    }
    scope = scope.model_copy(
        update={"source_bundle_sha256": canonical_payload_sha256(source_bundle), "scope_sha256": ""}
    )
    # Revalidate so the changed source-bundle identity participates in scope hash.
    scope = LectureScope.model_validate(scope.model_dump(mode="json"))

    def candidate(note_id: int, text: str) -> dict[str, object]:
        return {
            "note_id": note_id,
            "text": text,
            "extra": f"extra-{note_id}",
            "tags": ["tag:a", "Tag:z"],
            "decks": ["Deck Z", "Deck A"],
            "base_rrf": 0.1,
            "boost_total": 0.02,
            "calibrated_score": 0.12,
            "semantic_score": 0.9,
            "semantic_rank": 1,
            "lexical_rank": 2,
            "exact_match_reasons": ["exact:a", "exact:z"],
        }

    records = [
        {
            "concept_id": "concept-1",
            "fact_id": "fact-1",
            "all_candidates": [candidate(1, "front-1"), candidate(2, "front-2")],
            "clusters": [
                {
                    "representative_note_id": 1,
                    "sibling_note_ids": [1, 2],
                    "missing_vector_note_ids": [],
                }
            ],
        },
        {
            "concept_id": "concept-1",
            "fact_id": "fact-2",
            "all_candidates": [candidate(3, "front-3")],
            "clusters": [
                {
                    "representative_note_id": 3,
                    "sibling_note_ids": [3],
                    "missing_vector_note_ids": [],
                }
            ],
        },
    ]
    for record in records:
        record["fact_sha256"] = canonical_payload_sha256(record)
    r5 = {
        "policy_sha256": policy.policy_sha256,
        "scope_sha256": scope.scope_sha256,
        "facts": [],
    }
    _empty_ledger(r5)
    r5["artifact_sha256"] = canonical_payload_sha256(r5)
    r6 = {
        "policy_sha256": policy.policy_sha256,
        "scope_sha256": scope.scope_sha256,
        "r5_artifact_sha256": r5["artifact_sha256"],
        "config_sha256": "c" * 64,
        "semantic_generation": "semantic",
        "records": records,
    }
    _empty_ledger(r6)
    r6["artifact_sha256"] = canonical_payload_sha256(r6)
    cheap, thorough = _routes()
    model_config = ResolvedModelConfiguration(
        profile="v3",
        ledger_s2=cheap,
        classify_s4=cheap,
        residual_s6=cheap,
        gap_fill_s7=cheap,
        scope_r3=cheap,
        cheap_classify_r7=cheap,
        thorough_classify_r7=thorough,
    )
    model_config_sha256 = "m" * 64
    r0 = {
        "policy": policy.model_dump(mode="json"),
        "policy_sha256": policy.policy_sha256,
        "policy_revision": policy.revision,
        "model_config_sha256": model_config_sha256,
        "cheap_classify_r7": classification_v3.route_document(cheap),
        "thorough_classify_r7": classification_v3.route_document(thorough),
    }
    _add_r0_costs(r0, cheap.model, thorough.model)
    r0["r7_classification"] = r7_pin_document(cheap, thorough, str(r0["rate_table_sha256"]))
    job = SimpleNamespace(
        pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V3,
        id="classification-job",
        policy_sha256=policy.policy_sha256,
        model_config_sha256=model_config_sha256,
        resolved_model_config=model_config,
        offline_replay_only=True,
    )
    context = SimpleNamespace(
        job=job,
        prior_payloads={
            CurationStage.V3_R0_PREFLIGHT: r0,
            CurationStage.V3_R3_SCOPE: {
                "scope": scope.model_dump(mode="json"),
                "source_bundle": source_bundle,
                "cost_ledger": [],
                "cost_ledger_sha256": sha256(b"[]").hexdigest(),
            },
            CurationStage.V3_R5_RETRIEVAL: r5,
            CurationStage.V3_R6_CALIBRATION: r6,
        },
    )
    fake = FakeGenerator(
        [
            {
                "rows": [
                    _row_for("bundle:concept-1:fact-1:note:1", "note:1", "keep"),
                    _row_for("bundle:concept-1:fact-2:note:3", "note:3", "exclude"),
                ]
            }
        ]
    )
    runner = object.__new__(CurationServicesRunner)
    runner.structured = StructuredTextService(fake)
    runner.embedder = SimpleNamespace(offline_replay_only=True)
    runner.semantic = SimpleNamespace(embedder=SimpleNamespace(offline_replay_only=True))
    return runner, context, fake


def _row_for(bundle_id: str, candidate_id: str, disposition: str) -> dict[str, object]:
    return {
        "bundle_id": bundle_id,
        "candidate_id": candidate_id,
        "disposition": disposition,
        "confidence_bps": 10_000,
        "supporting_passage_ids": ["passage-1" if "fact-1" in bundle_id else "passage-2"],
        "conflicting_passage_ids": [],
        "redundant_with_candidate_id": None,
        "reason": "supported",
    }


def test_v3_r7_stage_builds_hash_closed_bundle_only_artifact() -> None:
    runner, context, fake = _stage_fixture()
    product = asyncio.run(runner._v3_r7_classification(context))
    payload = product.payload
    assert product.kind == "card_centric_v3_classification"
    assert product.usage is not None and product.usage.input_tokens == 10
    assert payload["policy_sha256"] == context.job.policy_sha256
    assert (
        payload["scope_sha256"]
        == context.prior_payloads[CurationStage.V3_R3_SCOPE]["scope"]["scope_sha256"]
    )
    assert (
        payload["r6_artifact_sha256"]
        == context.prior_payloads[CurationStage.V3_R6_CALIBRATION]["artifact_sha256"]
    )
    assert payload["bundles_sha256"] == canonical_payload_sha256(payload["bundles"])
    assert payload["bundle_sha256s"] == [item["bundle_sha256"] for item in payload["bundles"]]
    assert [item["fact_id"] for item in payload["bundles"]] == ["fact-1", "fact-2"]
    first = payload["bundles"][0]
    assert [fact["fact_id"] for fact in first["concept"]["facts"]] == ["fact-1"]
    assert first["concept"]["source_evidence_ids"] == ["passage-1"]
    assert [passage["passage_id"] for passage in first["selected_passages"]] == ["passage-1"]
    assert first["candidate"] == {
        "candidate_id": "note:1",
        "note_id": 1,
        "text": "front-1",
        "extra": "extra-1",
        "tags": ["tag:a", "Tag:z"],
        "deck": "Deck A\nDeck Z",
    }
    assert first["duplicate_sibling_ids"] == ["note:2"]
    assert first["exact_match_reasons"] == ["exact:a", "exact:z"]
    assert [item["identity"] for item in first["retrieval_scores"]] == sorted(
        item["identity"] for item in first["retrieval_scores"]
    )
    assert "first fact evidence" not in json.dumps(fake.calls[0]["input"]["bundles"][1])
    assert fake.calls[0]["options"].cacheable_source_prefix is None
    assert payload["final_partition"] == [
        payload["cheap_rows"][0],
        payload["cheap_rows"][1],
    ]
    assert payload["calls"][0]["usage"] == {
        "input_tokens": 10,
        "output_tokens": 2,
        "cost_microusd": 3,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    assert payload["artifact_sha256"] == canonical_payload_sha256(
        {key: value for key, value in payload.items() if key != "artifact_sha256"}
    )
    pins = context.prior_payloads[CurationStage.V3_R0_PREFLIGHT]["r7_classification"]
    assert payload["classification_config_sha256"] == pins["classification_config_sha256"]
    assert payload["schema_sha256"] == pins["provider_output_schema_sha256"]
    assert payload["instruction_sha256"] == pins["instruction_sha256"]
    assert payload["options"]["cheap"] == pins["cheap_options"]
    assert payload["estimator_version"] == pins["estimator_version"]
    assert payload["rate_table_sha256"] == pins["rate_table_sha256"]


def test_v3_r7_caps_each_fact_and_exposes_exact_provider_enums() -> None:
    runner, context, _fake = _stage_fixture()
    record = context.prior_payloads[CurationStage.V3_R6_CALIBRATION]["records"][0]
    template = record["all_candidates"][0]
    record["all_candidates"] = []
    record["clusters"] = []
    for note_id, score in ((1, 0.1), (4, 0.4), (5, 0.3), (6, 0.2)):
        candidate = deepcopy(template)
        candidate.update(note_id=note_id, calibrated_score=score)
        record["all_candidates"].append(candidate)
        record["clusters"].append(
            {
                "representative_note_id": note_id,
                "sibling_note_ids": [note_id],
                "missing_vector_note_ids": [],
            }
        )
    _reseal_r6(context)
    fake = FakeGenerator(
        [
            {
                "rows": [
                    _row_for(f"bundle:concept-1:fact-1:note:{note_id}", f"note:{note_id}", "keep")
                    for note_id in (4, 5, 6)
                ]
                + [_row_for("bundle:concept-1:fact-2:note:3", "note:3", "exclude")]
            }
        ]
    )
    runner.structured = StructuredTextService(fake)
    product = asyncio.run(runner._v3_r7_classification(context))
    assert [row["candidate"]["note_id"] for row in product.payload["bundles"]] == [4, 5, 6, 3]
    row_schema = fake.calls[0]["output_schema"]["$defs"]["ProviderClassificationRow"]
    assert row_schema["properties"]["candidate_id"]["enum"] == [
        "note:3",
        "note:4",
        "note:5",
        "note:6",
    ]
    assert row_schema["properties"]["supporting_passage_ids"]["items"]["enum"] == [
        "passage-1",
        "passage-2",
    ]


def _reseal_r6(context: SimpleNamespace) -> None:
    r6 = context.prior_payloads[CurationStage.V3_R6_CALIBRATION]
    for record in r6["records"]:
        record["fact_sha256"] = canonical_payload_sha256(
            {key: value for key, value in record.items() if key != "fact_sha256"}
        )
    r6["artifact_sha256"] = canonical_payload_sha256(
        {key: value for key, value in r6.items() if key != "artifact_sha256"}
    )


def _tamper_r0_policy(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R0_PREFLIGHT]["policy_sha256"] = "f" * 64


def _tamper_r0_model(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R0_PREFLIGHT]["model_config_sha256"] = "f" * 64


def _tamper_r0_route(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R0_PREFLIGHT]["cheap_classify_r7"]["model"] = "wrong"


def _tamper_r0_pin_missing(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R0_PREFLIGHT].pop("r7_classification")


def _tamper_r0_pin_extra(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R0_PREFLIGHT]["r7_classification"]["extra"] = True


def _tamper_r0_instruction(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R0_PREFLIGHT]["r7_classification"][
        "instruction_sha256"
    ]["cheap"] = "f" * 64


def _tamper_r0_schema(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R0_PREFLIGHT]["r7_classification"][
        "provider_output_schema_sha256"
    ] = "f" * 64


def _tamper_r0_options(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R0_PREFLIGHT]["r7_classification"]["cheap_options"][
        "temperature"
    ] = 1.0


def _tamper_r0_config(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R0_PREFLIGHT]["r7_classification"][
        "classification_config"
    ]["cheap_batch_size"] = 1


def _tamper_r0_estimator(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R0_PREFLIGHT]["r7_classification"][
        "estimator_version"
    ] = "wrong"


def _tamper_r0_rate(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R0_PREFLIGHT]["rate_table_sha256"] = "not-a-hash"


def _tamper_r3_scope_hash(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R3_SCOPE]["scope"]["scope_sha256"] = "f" * 64


def _tamper_r3_scope_policy(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R3_SCOPE]["scope"]["policy_sha256"] = "f" * 64


def _tamper_r3_source_bundle(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R3_SCOPE]["source_bundle"]["evidence"][0][
        "normalized_text"
    ] = "tampered"


def _tamper_r3_source_evidence_closure(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R3_SCOPE]["source_bundle"]["evidence"].pop()


def _tamper_r6_artifact(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R6_CALIBRATION]["artifact_sha256"] = "f" * 64


def _tamper_r6_policy(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R6_CALIBRATION]["policy_sha256"] = "f" * 64
    _reseal_r6(context)


def _tamper_r6_scope(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R6_CALIBRATION]["scope_sha256"] = "f" * 64
    _reseal_r6(context)


def _tamper_r6_fact_hash(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R6_CALIBRATION]["records"][0]["fact_sha256"] = "f" * 64
    context.prior_payloads[CurationStage.V3_R6_CALIBRATION]["artifact_sha256"] = (
        canonical_payload_sha256(
            {
                key: value
                for key, value in context.prior_payloads[CurationStage.V3_R6_CALIBRATION].items()
                if key != "artifact_sha256"
            }
        )
    )


def _tamper_r6_duplicate_fact(context: SimpleNamespace) -> None:
    r6 = context.prior_payloads[CurationStage.V3_R6_CALIBRATION]
    r6["records"].append(deepcopy(r6["records"][0]))
    _reseal_r6(context)


def _tamper_r6_missing_fact(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R6_CALIBRATION]["records"].pop()
    _reseal_r6(context)


def _tamper_r6_extra_fact(context: SimpleNamespace) -> None:
    r6 = context.prior_payloads[CurationStage.V3_R6_CALIBRATION]
    extra = deepcopy(r6["records"][0])
    extra["fact_id"] = "fact-extra"
    r6["records"].append(extra)
    _reseal_r6(context)


def _tamper_r6_candidate(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R6_CALIBRATION]["records"][0]["all_candidates"][0][
        "note_id"
    ] = "wrong"
    _reseal_r6(context)


def _tamper_r6_cluster_partition(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R6_CALIBRATION]["records"][0]["clusters"][0][
        "sibling_note_ids"
    ] = [1]
    _reseal_r6(context)


def _tamper_r6_duplicate_sibling(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R6_CALIBRATION]["records"][0]["clusters"][0][
        "sibling_note_ids"
    ] = [1, 1, 2]
    _reseal_r6(context)


def _tamper_r6_representative(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R6_CALIBRATION]["records"][0]["clusters"][0][
        "representative_note_id"
    ] = 999
    _reseal_r6(context)


def _tamper_r6_missing_vector(context: SimpleNamespace) -> None:
    context.prior_payloads[CurationStage.V3_R6_CALIBRATION]["records"][0]["clusters"][0][
        "missing_vector_note_ids"
    ] = [999]
    _reseal_r6(context)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("text", 7),
        ("extra", 7),
        ("tags", ["tag:z", "tag:a"]),
        ("tags", ["tag:a", "tag:a"]),
        ("exact_match_reasons", ["exact:z", "exact:a"]),
        ("exact_match_reasons", ["exact:a", "exact:a"]),
        ("decks", ["Deck", " "]),
        ("note_id", True),
    ],
)
def test_v3_r7_stage_rejects_r6_card_field_normalization_attempts(
    field: str, value: object
) -> None:
    runner, context, fake = _stage_fixture()
    context.prior_payloads[CurationStage.V3_R6_CALIBRATION]["records"][0]["all_candidates"][0][
        field
    ] = value
    _reseal_r6(context)
    product = asyncio.run(runner._v3_r7_classification(context))
    assert product.blocking_error is not None
    assert fake.calls == []


@pytest.mark.parametrize("field", ["sibling_note_ids", "missing_vector_note_ids"])
def test_v3_r7_stage_rejects_bool_cluster_ids(field: str) -> None:
    runner, context, fake = _stage_fixture()
    context.prior_payloads[CurationStage.V3_R6_CALIBRATION]["records"][0]["clusters"][0][field] = [
        True
    ]
    _reseal_r6(context)
    product = asyncio.run(runner._v3_r7_classification(context))
    assert product.blocking_error is not None
    assert fake.calls == []


def test_v3_r7_marks_representative_missing_vector_bundle_degraded() -> None:
    runner, context, _fake = _stage_fixture()
    context.prior_payloads[CurationStage.V3_R6_CALIBRATION]["records"][0]["clusters"][0][
        "missing_vector_note_ids"
    ] = [1]
    _reseal_r6(context)
    product = asyncio.run(runner._v3_r7_classification(context))
    assert product.payload["bundles"][0]["degraded"] is True


@pytest.mark.parametrize(
    "tamper",
    [
        _tamper_r0_policy,
        _tamper_r0_model,
        _tamper_r0_route,
        _tamper_r0_pin_missing,
        _tamper_r0_pin_extra,
        _tamper_r0_instruction,
        _tamper_r0_schema,
        _tamper_r0_options,
        _tamper_r0_config,
        _tamper_r0_estimator,
        _tamper_r0_rate,
        _tamper_r3_scope_hash,
        _tamper_r3_scope_policy,
        _tamper_r3_source_bundle,
        _tamper_r3_source_evidence_closure,
        _tamper_r6_artifact,
        _tamper_r6_policy,
        _tamper_r6_scope,
        _tamper_r6_fact_hash,
        _tamper_r6_missing_fact,
        _tamper_r6_duplicate_fact,
        _tamper_r6_extra_fact,
        _tamper_r6_candidate,
        _tamper_r6_cluster_partition,
        _tamper_r6_duplicate_sibling,
        _tamper_r6_representative,
        _tamper_r6_missing_vector,
    ],
)
def test_v3_r7_stage_rejects_tampered_trust_boundaries_before_provider(
    tamper: object,
) -> None:
    runner, context, fake = _stage_fixture()
    tamper(context)  # type: ignore[operator]
    try:
        product = asyncio.run(runner._v3_r7_classification(context))
    except PinnedInputChanged:
        product = None
    if product is not None:
        assert product.blocking_error is not None
    assert fake.calls == []


def test_v3_r7_oversized_bundle_blocks_without_truncation_or_provider_call() -> None:
    runner, context, fake = _stage_fixture()
    context.prior_payloads[CurationStage.V3_R6_CALIBRATION]["records"][0]["all_candidates"][0][
        "text"
    ] = "x" * 20_000
    _reseal_r6(context)
    product = asyncio.run(runner._v3_r7_classification(context))
    assert product.blocking_error == "R7 bundle exceeds its 16384-byte input bound"
    assert product.payload["policy_sha256"] == context.job.policy_sha256
    assert (
        product.payload["scope_sha256"]
        == context.prior_payloads[CurationStage.V3_R3_SCOPE]["scope"]["scope_sha256"]
    )
    assert (
        product.payload["r6_artifact_sha256"]
        == context.prior_payloads[CurationStage.V3_R6_CALIBRATION]["artifact_sha256"]
    )
    assert product.payload["blocking"] is True
    assert product.payload["partial_diagnostics"] == [
        "R7 bundle exceeds its 16384-byte input bound"
    ]
    for key in (
        "bundles",
        "bundle_sha256s",
        "cheap_rows",
        "escalations",
        "thorough_rows",
        "final_partition",
        "calls",
    ):
        assert product.payload[key] == []
    assert product.payload["bundles_sha256"] == canonical_payload_sha256([])
    assert product.payload["artifact_sha256"] == canonical_payload_sha256(
        {key: value for key, value in product.payload.items() if key != "artifact_sha256"}
    )
    assert fake.calls == []


def test_v3_r7_dispatch_exists_and_r8_retains_its_required_r4_closure() -> None:
    runner, context, _fake = _stage_fixture()
    context.stage = CurationStage.V3_R7_CLASSIFICATION
    product = asyncio.run(runner.run(context))
    assert product.kind == "card_centric_v3_classification"
    context.stage = CurationStage.V3_R8_GAP_CONFIRMATION
    with pytest.raises(KeyError):
        asyncio.run(runner.run(context))
    assert any(
        definition.stage is CurationStage.V3_R8_GAP_CONFIRMATION
        for definition in pipeline_stages(PipelineContractVersion.CARD_CENTRIC_V3)
    )


def test_v3_r7_empty_r6_candidate_partition_is_hash_closed_without_provider_call() -> None:
    runner, context, fake = _stage_fixture()
    for record in context.prior_payloads[CurationStage.V3_R6_CALIBRATION]["records"]:
        record["all_candidates"] = []
        record["clusters"] = []
    _reseal_r6(context)
    product = asyncio.run(runner._v3_r7_classification(context))
    payload = product.payload
    assert product.blocking_error is None and product.usage is None
    assert payload["policy_sha256"] == context.job.policy_sha256
    assert (
        payload["scope_sha256"]
        == context.prior_payloads[CurationStage.V3_R3_SCOPE]["scope"]["scope_sha256"]
    )
    assert (
        payload["r6_artifact_sha256"]
        == context.prior_payloads[CurationStage.V3_R6_CALIBRATION]["artifact_sha256"]
    )
    for key in (
        "bundles",
        "bundle_sha256s",
        "cheap_rows",
        "escalations",
        "thorough_rows",
        "final_partition",
        "calls",
    ):
        assert payload[key] == []
    assert payload["bundles_sha256"] == canonical_payload_sha256([])
    assert payload["blocking"] is False and payload["partial_diagnostics"] == []
    assert payload["artifact_sha256"] == canonical_payload_sha256(
        {key: value for key, value in payload.items() if key != "artifact_sha256"}
    )
    assert fake.calls == []
    context.stage = CurationStage.V3_R8_GAP_CONFIRMATION
    with pytest.raises(KeyError):
        asyncio.run(runner.run(context))


def test_r7_keeps_terminal_cheap_rows_out_of_thorough_batches() -> None:
    first, second = _bundle(1, "fact-1"), _bundle(2, "fact-2")
    fake = FakeGenerator(
        [
            {"rows": [_row(first, "keep", 9000), _row(second, "needs_review", 0)]},
            {"rows": [_row(second, "keep", 9000)]},
        ]
    )
    cheap, thorough = _routes()
    result = R7ClassificationService(StructuredTextService(fake)).classify(
        bundles=(first, second), strictness="strict", cheap_route=cheap, thorough_route=thorough
    )
    assert [call["input"]["tier"] for call in fake.calls] == ["cheap", "thorough"]
    assert [item["candidate"]["note_id"] for item in fake.calls[1]["input"]["bundles"]] == [2]
    assert [row["disposition"] for row in result.payload["final_partition"]] == ["keep", "keep"]


def test_r7_can_defer_partial_cards_without_a_thorough_call() -> None:
    bundle = _bundle(1, "fact-1")
    fake = FakeGenerator([{"rows": [_row(bundle, "needs_review", 0)]}])
    cheap, thorough = _routes()
    result = R7ClassificationService(StructuredTextService(fake)).classify(
        bundles=(bundle,),
        strictness="strict",
        cheap_route=cheap,
        thorough_route=thorough,
        defer_partial=True,
    )
    assert len(fake.calls) == 1
    assert result.payload["final_partition"][0]["diagnostic"] == "deferred_to_set_coverage"


def test_r7_defers_a_repairable_cheap_contract_failure_to_set_coverage() -> None:
    bundle = _bundle(1, "fact-1")
    fake = FakeGenerator([{"rows": []}])
    cheap, thorough = _routes()
    result = R7ClassificationService(StructuredTextService(fake)).classify(
        bundles=(bundle,),
        strictness="strict",
        cheap_route=cheap,
        thorough_route=thorough,
        defer_partial=True,
    )
    assert len(fake.calls) == 1 and result.blocking_error is None
    assert result.payload["blocking"] is False
    assert result.payload["final_partition"][0]["diagnostic"].startswith(
        "deferred_to_set_coverage:"
    )


def test_r8_set_coverage_selects_multiple_cards_from_one_compact_fact_request() -> None:
    statement = "First claim but second claim"
    first, second = _bundle(1, "fact-1", statement), _bundle(2, "fact-1", statement)
    response = {
        "rows": [
            {
                "fact_id": "fact-1",
                "status": "covered",
                "candidate_contributions": [
                    {"candidate_id": "note:2", "target_claim_ids": ["fact-1:claim:02"]},
                    {"candidate_id": "note:1", "target_claim_ids": ["fact-1:claim:01"]},
                ],
                "confidence_bps": 9000,
                "uncovered_claim_ids": [],
            }
        ]
    }
    fake = FakeGenerator([response])
    cheap, _thorough = _routes()
    result = classify_set_coverage(
        StructuredTextService(fake),
        bundles=(first, second),
        strictness="strict",
        route=cheap,
    )
    assert [row["disposition"] for row in result.payload["final_partition"]] == ["keep", "keep"]
    assert result.payload["rows"][0]["selected_candidate_ids"] == ["note:1", "note:2"]
    assert len(fake.calls) == 1 and len(fake.calls[0]["input"]["facts"]) == 1
    fact_input = fake.calls[0]["input"]["facts"][0]
    assert set(fact_input) == {"fact_id", "material_claims", "candidates"}
    assert fact_input["material_claims"] == [
        {"claim_id": "fact-1:claim:01", "statement": "First claim"},
        {"claim_id": "fact-1:claim:02", "statement": "but second claim"},
    ]
    contribution_schema = fake.calls[0]["output_schema"]["$defs"]["ProviderCandidateContribution"]
    assert contribution_schema["properties"]["target_claim_ids"]["items"]["enum"] == [
        "fact-1:claim:01",
        "fact-1:claim:02",
    ]
    assert set(fact_input["candidates"][0]) == {"candidate_id", "note_id", "text", "extra"}
    assert all(
        row["supporting_passage_ids"] == ["passage-1"] for row in result.payload["final_partition"]
    )


@pytest.mark.parametrize(
    ("statement", "expected"),
    (
        (
            "Uroporphyrinogen decarboxylase converts uroporphyrinogen III to "
            "coproporphyrinogen III, and coproporphyrinogen oxidase converts it to "
            "protoporphyrinogen IX.",
            (
                "Uroporphyrinogen decarboxylase converts uroporphyrinogen III to "
                "coproporphyrinogen III",
                "and coproporphyrinogen oxidase converts it to protoporphyrinogen IX.",
            ),
        ),
        (
            "Protoporphyrinogen oxidase converts protoporphyrinogen IX to protoporphyrin IX, "
            "after which ferrochelatase adds ferrous iron to form heme.",
            (
                "Protoporphyrinogen oxidase converts protoporphyrinogen IX to protoporphyrin IX",
                "after which ferrochelatase adds ferrous iron to form heme.",
            ),
        ),
        (
            "Hydroxymethylbilane spontaneously cyclizes mainly to uroporphyrinogen I, while "
            "uroporphyrinogen III cosynthase produces uroporphyrinogen III, the isomer that "
            "proceeds toward heme.",
            (
                "Hydroxymethylbilane spontaneously cyclizes mainly to uroporphyrinogen I",
                "while uroporphyrinogen III cosynthase produces uroporphyrinogen III",
                "the isomer that proceeds toward heme.",
            ),
        ),
        (
            "Decreased temperature, acidity, partial pressure of carbon dioxide, and 2,3-BPG "
            "promote the R form.",
            (
                "Decreased temperature, acidity, partial pressure of carbon dioxide, and "
                "2,3-BPG promote the R form.",
            ),
        ),
    ),
)
def test_r8_material_claim_inventory_splits_observed_compound_mechanisms(
    statement: str, expected: tuple[str, ...]
) -> None:
    bundle = _bundle(1, "fact-1", statement)

    assert tuple(
        claim
        for _claim_id, claim in classification_v3._material_claim_inventory(
            (("fact-1", (bundle,)),), "fact-1"
        )
    ) == expected


def test_r8_set_coverage_fails_closed_when_a_candidate_escapes_its_fact() -> None:
    bundle = _bundle(1, "fact-1")
    fake = FakeGenerator(
        [
            {
                "rows": [
                    {
                        "fact_id": "fact-1",
                        "status": "covered",
                        "candidate_contributions": [
                            {
                                "candidate_id": "note:999",
                                "target_claim_ids": ["fact-1:claim:01"],
                            }
                        ],
                        "confidence_bps": 9000,
                        "uncovered_claim_ids": [],
                    },
                ]
            }
        ]
    )
    cheap, _thorough = _routes()
    result = classify_set_coverage(
        StructuredTextService(fake),
        bundles=(bundle,),
        strictness="strict",
        route=cheap,
    )
    assert result.blocking_error == "R8 set-coverage response escapes requested candidates"
    assert {row["disposition"] for row in result.payload["final_partition"]} == {"unresolved"}


def test_r8_set_coverage_sends_exactly_one_fact_per_call() -> None:
    first, second = _bundle(1, "fact-1"), _bundle(2, "fact-2")
    responses = [
        {
            "rows": [
                {
                    "fact_id": fact_id,
                    "status": "missing",
                    "candidate_contributions": [],
                    "confidence_bps": 9900,
                    "uncovered_claim_ids": [f"{fact_id}:claim:01"],
                }
            ]
        }
        for fact_id in ("fact-1", "fact-2")
    ]
    fake = FakeGenerator(responses)
    cheap, _thorough = _routes()
    result = classify_set_coverage(
        StructuredTextService(fake),
        bundles=(first, second),
        strictness="strict",
        route=cheap,
    )
    assert len(fake.calls) == 2
    assert all(len(call["input"]["facts"]) == 1 for call in fake.calls)
    assert result.payload["facts_per_batch"] == 1


def test_r8_set_coverage_downgrades_claimed_coverage_with_uncovered_claims() -> None:
    bundle = _bundle(1, "fact-1", "First clause but second clause")
    fake = FakeGenerator(
        [
            {
                "rows": [
                    {
                        "fact_id": "fact-1",
                        "status": "covered",
                        "candidate_contributions": [
                            {
                                "candidate_id": "note:1",
                                "target_claim_ids": ["fact-1:claim:01"],
                            }
                        ],
                        "confidence_bps": 9900,
                        "uncovered_claim_ids": ["fact-1:claim:02"],
                    }
                ]
            }
        ]
    )
    cheap, _thorough = _routes()
    result = classify_set_coverage(
        StructuredTextService(fake),
        bundles=(bundle,),
        strictness="strict",
        route=cheap,
    )
    assert result.blocking_error is None
    assert result.payload["rows"][0]["status"] == "unresolved"
    assert result.payload["rows"][0]["uncovered_material_claims"] == [
        "but second clause",
    ]
    assert result.payload["final_partition"][0]["disposition"] == "unresolved"


def test_r8_set_coverage_downgrades_a_covered_response_that_omits_a_target_clause() -> None:
    statement = (
        "AIP causes abdominal and neuropsychiatric symptoms with purple urine but is the only "
        "genetic porphyria described as lacking cutaneous photosensitivity."
    )
    bundle = _bundle(1, "fact-1", statement)
    fake = FakeGenerator(
        [
            {
                "rows": [
                    {
                        "fact_id": "fact-1",
                        "status": "covered",
                        "candidate_contributions": [
                            {
                                "candidate_id": "note:1",
                                "target_claim_ids": ["fact-1:claim:01"],
                            }
                        ],
                        "confidence_bps": 9900,
                        "uncovered_claim_ids": [],
                    }
                ]
            }
        ]
    )
    cheap, _thorough = _routes()
    result = classify_set_coverage(
        StructuredTextService(fake), bundles=(bundle,), strictness="strict", route=cheap
    )
    assert fake.calls[0]["input"]["facts"][0]["material_claims"] == [
        {
            "claim_id": "fact-1:claim:01",
            "statement": "AIP causes abdominal and neuropsychiatric symptoms with purple urine",
        },
        {
            "claim_id": "fact-1:claim:02",
            "statement": (
                "but is the only genetic porphyria described as lacking cutaneous photosensitivity."
            ),
        },
    ]
    assert result.payload["rows"][0]["diagnostic"] == (
        "provider did not partition caller-authored material claims"
    )
    assert result.payload["final_partition"][0]["disposition"] == "unresolved"


@pytest.mark.parametrize(
    ("status", "selected", "uncovered", "diagnostic"),
    (
        ("covered", [], [], "covered result omitted selected candidates"),
        (
            "missing",
            ["note:1"],
            ["partial claim"],
            "missing result selected partial candidates",
        ),
        ("missing", [], [], "missing result omitted uncovered material claims"),
    ),
)
def test_r8_set_coverage_normalizes_semantic_contradictions_per_fact(
    status: str,
    selected: list[str],
    uncovered: list[str],
    diagnostic: str,
) -> None:
    bundle = _bundle(1, "fact-1")
    fake = FakeGenerator(
        [
            {
                "rows": [
                    {
                        "fact_id": "fact-1",
                        "status": status,
                        "candidate_contributions": [
                            {
                                "candidate_id": candidate_id,
                                "target_claim_ids": ["fact-1:claim:01"],
                            }
                            for candidate_id in selected
                        ],
                        "confidence_bps": 9900,
                        "uncovered_claim_ids": (["fact-1:claim:01"] if uncovered else []),
                    }
                ]
            }
        ]
    )
    cheap, _thorough = _routes()
    result = classify_set_coverage(
        StructuredTextService(fake),
        bundles=(bundle,),
        strictness="strict",
        route=cheap,
    )
    assert result.blocking_error is None
    assert result.payload["rows"][0]["status"] == "unresolved"
    assert result.payload["rows"][0]["diagnostic"] == diagnostic
    assert result.payload["final_partition"][0]["disposition"] == "unresolved"


def test_r8_set_coverage_downgrades_duplicate_candidate_contributions() -> None:
    first, second = _bundle(1, "fact-1"), _bundle(2, "fact-1")
    fake = FakeGenerator(
        [
            {
                "rows": [
                    {
                        "fact_id": "fact-1",
                        "status": "covered",
                        "candidate_contributions": [
                            {
                                "candidate_id": "note:1",
                                "target_claim_ids": ["fact-1:claim:01"],
                            },
                            {
                                "candidate_id": "note:1",
                                "target_claim_ids": ["fact-1:claim:01"],
                            },
                        ],
                        "confidence_bps": 9900,
                        "uncovered_claim_ids": [],
                    }
                ]
            }
        ]
    )
    cheap, _thorough = _routes()
    result = classify_set_coverage(
        StructuredTextService(fake),
        bundles=(first, second),
        strictness="strict",
        route=cheap,
    )
    assert result.blocking_error is None
    assert result.payload["rows"][0]["diagnostic"] == (
        "provider duplicated candidate contributions"
    )
    assert {row["disposition"] for row in result.payload["final_partition"]} == {"unresolved"}


def test_r8_set_coverage_keeps_a_well_formed_missing_result_terminal() -> None:
    bundle = _bundle(1, "fact-1")
    fake = FakeGenerator(
        [
            {
                "rows": [
                    {
                        "fact_id": "fact-1",
                        "status": "missing",
                        "candidate_contributions": [],
                        "confidence_bps": 9900,
                        "uncovered_claim_ids": ["fact-1:claim:01"],
                    }
                ]
            }
        ]
    )
    cheap, _thorough = _routes()
    result = classify_set_coverage(
        StructuredTextService(fake),
        bundles=(bundle,),
        strictness="strict",
        route=cheap,
    )
    assert result.blocking_error is None
    assert result.payload["rows"][0]["status"] == "missing"
    assert result.payload["final_partition"][0]["disposition"] == "exclude"


def test_r8_set_coverage_defines_missing_and_unresolved_statuses() -> None:
    boundary = "absence from all supplied cards is missing, not unresolved"

    assert boundary in SET_COVERAGE_INSTRUCTION
    assert boundary in ProviderSetCoverageRow.model_json_schema()["properties"]["status"][
        "description"
    ]


def test_r7_invalid_batch_is_unresolved_when_repair_is_not_authorized() -> None:
    bundle = _bundle(1, "fact-1")
    fake = FakeGenerator([{"rows": []}])
    cheap, thorough = _routes()
    result = R7ClassificationService(StructuredTextService(fake)).classify(
        bundles=(bundle,), strictness="strict", cheap_route=cheap, thorough_route=thorough
    )
    assert len(fake.calls) == 1  # primary cheap only; no repair means partial unresolved
    assert result.payload["blocking"] is True
    assert result.payload["final_partition"][0]["disposition"] == "unresolved"


def test_r7_row_contract_failure_escalates_only_that_row() -> None:
    first, second = _bundle(1, "fact-1"), _bundle(2, "fact-2")
    invalid = _row(first, "keep", 10_000)
    invalid["supporting_passage_ids"] = ["invented"]
    fake = FakeGenerator(
        [
            {"rows": [invalid, _row(second, "exclude", 7500)]},
            {"rows": [_row(first, "keep", 9000)]},
        ]
    )
    cheap, thorough = _routes()
    result = R7ClassificationService(StructuredTextService(fake)).classify(
        bundles=(first, second), strictness="strict", cheap_route=cheap, thorough_route=thorough
    )
    assert [item["candidate"]["note_id"] for item in fake.calls[1]["input"]["bundles"]] == [1]
    assert result.payload["escalations"][0]["reasons"] == ["contract_invalid"]
    assert [row["disposition"] for row in result.payload["final_partition"]] == ["keep", "exclude"]


def test_r7_accumulates_low_and_conflict_reasons_in_fixed_order() -> None:
    bundle = _bundle(1, "fact-1")
    cheap_row = _row(bundle, "needs_review", 0)
    cheap_row["conflicting_passage_ids"] = ["passage-1"]
    cheap_row["supporting_passage_ids"] = []
    fake = FakeGenerator([{"rows": [cheap_row]}, {"rows": [_row(bundle, "keep", 9000)]}])
    cheap, thorough = _routes()
    result = R7ClassificationService(StructuredTextService(fake)).classify(
        bundles=(bundle,), strictness="strict", cheap_route=cheap, thorough_route=thorough
    )
    assert result.payload["escalations"][0]["reasons"] == [
        "cheap_needs_review",
        "low_confidence",
        "conflicting_evidence",
    ]


def test_r7_one_request_bound_repair_aggregates_primary_and_repair_usage() -> None:
    bundle = _bundle(1, "fact-1")
    repair_request = _provider_input(
        "cheap",
        (bundle,),
        repair_error="R7 response does not partition requested bundles",
        invalid_response=[],
    )
    authorization = {
        "policy_sha256": bundle.policy_sha256,
        "rate_table_sha256": "c" * 64,
        "estimator_version": "utf8-byte-upper-bound-v1",
        "repair_request_sha256": canonical_payload_sha256(repair_request),
        "predicted_total_before_repair_microusd": 1,
        "predicted_repair_cost_microusd": 1,
        "predicted_total_after_repair_microusd": 2,
    }
    authorization["authorization_sha256"] = canonical_payload_sha256(authorization)
    fake = FakeGenerator([{"rows": []}, {"rows": [_row(bundle, "keep", 9000)]}])
    cheap, thorough = _routes()
    result = R7ClassificationService(StructuredTextService(fake)).classify(
        bundles=(bundle,),
        strictness="strict",
        cheap_route=cheap,
        thorough_route=thorough,
        repair_authorization=authorization,
        rate_table_sha256="c" * 64,
        ordinary_limit_microusd=2,
        hard_limit_microusd=2,
    )
    assert [call["instruction"] for call in fake.calls] == [
        CHEAP_INSTRUCTION,
        REPAIR_INSTRUCTION,
    ]
    assert fake.calls[1]["input"]["invalid_response"] == []
    assert (
        fake.calls[1]["input"]["repair_error"] == "R7 response does not partition requested bundles"
    )
    assert result.usage is not None and result.usage.input_tokens == 20
    assert result.payload["final_partition"][0]["disposition"] == "keep"


def test_r7_partition_repair_binds_the_full_invalid_provider_rows() -> None:
    bundle = _bundle(1, "fact-1")
    invalid_rows = [_row(bundle, "keep", 9000), _row(bundle, "keep", 9000)]
    repair_request = _provider_input(
        "cheap",
        (bundle,),
        repair_error="R7 response does not partition requested bundles",
        invalid_response=invalid_rows,
    )
    fake = FakeGenerator([{"rows": invalid_rows}, {"rows": [_row(bundle, "keep", 9000)]}])
    cheap, thorough = _routes()
    result = R7ClassificationService(StructuredTextService(fake)).classify(
        bundles=(bundle,),
        strictness="strict",
        cheap_route=cheap,
        thorough_route=thorough,
        repair_authorization=_authorization(bundle, canonical_payload_sha256(repair_request)),
        rate_table_sha256="c" * 64,
        ordinary_limit_microusd=2,
        hard_limit_microusd=2,
    )
    assert fake.calls[1]["input"]["invalid_response"] == invalid_rows
    assert result.payload["final_partition"][0]["disposition"] == "keep"


def test_r7_pins_and_repair_authorization_reject_stale_or_noninteger_costs() -> None:
    cheap, thorough = _routes()
    pin = r7_pin_document(cheap, thorough, "c" * 64)
    assert pin["cheap_options"]["cacheable_source_prefix_sha256"] is None
    assert pin["cheap_options"]["max_tokens"] == 3072
    assert pin["thorough_options"]["max_tokens"] == 3072
    assert pin["classification_config"]["output_max_tokens"] == 3072
    assert pin["classification_config"]["version"] == "classification-r7-v9"
    assert pin["classification_config"]["material_claim_inventory"] == "bounded-clause-v1"
    assert pin["set_coverage"]["provider_schema_strategy"] == ("batch-derived-claim-enums-v2")
    assert pin["set_coverage"]["route"] == classification_v3.route_document(thorough)
    assert pin["cheap_options_sha256"] == canonical_payload_sha256(pin["cheap_options"])
    request = "d" * 64
    authorization = {
        "policy_sha256": "a" * 64,
        "rate_table_sha256": "c" * 64,
        "estimator_version": "utf8-byte-upper-bound-v1",
        "repair_request_sha256": request,
        "predicted_total_before_repair_microusd": 1,
        "predicted_repair_cost_microusd": 1,
        "predicted_total_after_repair_microusd": 2,
    }
    authorization["authorization_sha256"] = canonical_payload_sha256(authorization)
    assert _valid_repair_authorization(authorization, request, "a" * 64, "c" * 64, 2, 2)
    for value in (-1, True):
        changed = dict(authorization, predicted_repair_cost_microusd=value)
        changed["authorization_sha256"] = canonical_payload_sha256(
            {key: item for key, item in changed.items() if key != "authorization_sha256"}
        )
        assert not _valid_repair_authorization(changed, request, "a" * 64, "c" * 64, 2, 2)


def test_r7_reason_rejects_whitespace_and_config_is_deep_copied() -> None:
    bundle = _bundle(1, "fact-1")
    invalid = _row(bundle, "keep", 9000)
    invalid["reason"] = " \t "
    fake = FakeGenerator([{"rows": [invalid]}])
    cheap, thorough = _routes()
    result = R7ClassificationService(StructuredTextService(fake)).classify(
        bundles=(bundle,), strictness="strict", cheap_route=cheap, thorough_route=thorough
    )
    assert result.payload["blocking"] is True
    left, right = classification_v3.r7_config_document(), classification_v3.r7_config_document()
    left["thresholds_bps"]["strict"]["keep"] = 1
    assert right["thresholds_bps"]["strict"]["keep"] == 9000


def test_r7_transport_failure_is_blocking_and_never_repaired() -> None:
    bundle = _bundle(1, "fact-1")
    fake = FakeGenerator([RuntimeError("offline")])
    cheap, thorough = _routes()
    result = R7ClassificationService(StructuredTextService(fake)).classify(
        bundles=(bundle,),
        strictness="strict",
        cheap_route=cheap,
        thorough_route=thorough,
        repair_authorization={},
        rate_table_sha256="c" * 64,
        ordinary_limit_microusd=2,
        hard_limit_microusd=2,
    )
    assert len(fake.calls) == 1
    assert result.payload["blocking"] is True
    assert (
        result.payload["final_partition"][0]["diagnostic"]
        == "R7 provider transport failure: offline"
    )


def _attempt_events() -> tuple[ProviderAttemptBinding, list[str]]:
    events: list[str] = []
    return (
        ProviderAttemptBinding(
            job_id=uuid4(),
            stage=CurationStage.V3_R7_CLASSIFICATION,
            stage_attempt=1,
            mode="canonical",
            recorder=lambda evidence: events.append(evidence.event.event),
        ),
        events,
    )


def _reservation() -> dict[str, object]:
    table = FrozenRateTable(
        (ModelRate("fake", 1, 1, 1, 1, 1),), datetime(2026, 8, 17, tzinfo=UTC), "fixture"
    )
    estimator = CostEstimator(table)
    usage = TokenUsage(input_tokens=1)
    return CostLedgerEntry(
        call_id="a" * 64,
        stage="R7",
        modality="structured",
        model="fake",
        request_sha256="b" * 64,
        rate_table_sha256=table.rate_table_sha256,
        estimator_version=estimator.version,
        predicted=estimator.estimate(CostKind.PREDICTED, model="fake", usage=usage),
        reserved=estimator.estimate(CostKind.RESERVED, model="fake", usage=usage),
    ).document()


def test_r7_bound_attempts_have_one_terminal_per_malformed_primary_and_repair() -> None:
    bundle = _bundle(1, "fact-1")
    error = "structured output failed JSON schema validation: $: invalid JSON at line 1, column 1"
    request = canonical_payload_sha256(
        _provider_input("cheap", (bundle,), repair_error=error, invalid_response="not json")
    )
    fake = FakeGenerator(["not json", {"rows": [_row(bundle, "keep", 9000)]}])
    cheap, thorough = _routes()
    binding, events = _attempt_events()
    with bind_provider_attempts(binding), provider_cost_reservation(_reservation()):
        result = R7ClassificationService(StructuredTextService(fake)).classify(
            bundles=(bundle,),
            strictness="strict",
            cheap_route=cheap,
            thorough_route=thorough,
            repair_authorization=_authorization(bundle, request),
            rate_table_sha256="c" * 64,
            ordinary_limit_microusd=2,
            hard_limit_microusd=2,
        )
    assert [
        event for event in events if event in {"accepted", "validation_failed", "contract_failed"}
    ] == [
        "validation_failed",
        "accepted",
    ]
    assert result.usage is not None and result.usage.input_tokens == 20


def test_r7_bound_malformed_repair_has_no_duplicate_terminal_event() -> None:
    bundle = _bundle(1, "fact-1")
    error = "structured output failed JSON schema validation: $: invalid JSON at line 1, column 1"
    request = canonical_payload_sha256(
        _provider_input("cheap", (bundle,), repair_error=error, invalid_response="not json")
    )
    fake = FakeGenerator(["not json", "not json"])
    cheap, thorough = _routes()
    binding, events = _attempt_events()
    with bind_provider_attempts(binding), provider_cost_reservation(_reservation()):
        result = R7ClassificationService(StructuredTextService(fake)).classify(
            bundles=(bundle,),
            strictness="strict",
            cheap_route=cheap,
            thorough_route=thorough,
            repair_authorization=_authorization(bundle, request),
            rate_table_sha256="c" * 64,
            ordinary_limit_microusd=2,
            hard_limit_microusd=2,
        )
    assert [
        event for event in events if event in {"accepted", "validation_failed", "contract_failed"}
    ] == [
        "validation_failed",
        "validation_failed",
    ]
    assert result.payload["blocking"] is True


def test_r7_bound_local_invalid_batch_contract_fails_but_preserves_valid_terminal() -> None:
    first, second = _bundle(1, "fact-1"), _bundle(2, "fact-2")
    invalid = _row(first, "keep", 9000)
    invalid["candidate_id"] = "note:999"
    fake = FakeGenerator(
        [
            {"rows": [invalid, _row(second, "keep", 9000)]},
            {"rows": [_row(first, "keep", 9000)]},
        ]
    )
    cheap, thorough = _routes()
    binding, events = _attempt_events()
    with bind_provider_attempts(binding), provider_cost_reservation(_reservation()):
        result = R7ClassificationService(StructuredTextService(fake)).classify(
            bundles=(first, second),
            strictness="strict",
            cheap_route=cheap,
            thorough_route=thorough,
        )
    assert [
        event for event in events if event in {"accepted", "validation_failed", "contract_failed"}
    ] == [
        "contract_failed",
        "accepted",
    ]
    assert [item["candidate"]["note_id"] for item in fake.calls[1]["input"]["bundles"]] == [1]
    assert result.payload["cheap_rows"] == [_row(second, "keep", 9000)]


@pytest.mark.parametrize(
    ("strictness", "disposition", "threshold"),
    [
        (strictness, disposition, threshold)
        for strictness, values in {
            "strict": {"keep": 9000, "exclude": 7500, "redundant": 9500},
            "balanced": {"keep": 8500, "exclude": 8500, "redundant": 9500},
            "permissive": {"keep": 7500, "exclude": 9000, "redundant": 9500},
        }.items()
        for disposition, threshold in values.items()
    ],
)
@pytest.mark.parametrize("delta", [0, -1])
def test_r7_threshold_boundaries_and_one_below(
    strictness: str, disposition: str, threshold: int, delta: int
) -> None:
    bundle = _bundle(1, "fact-1")
    if disposition == "redundant":
        bundle = bundle.model_copy(update={"duplicate_sibling_ids": ("note:2",)})
    row = _row(bundle, disposition, threshold + delta)
    if disposition == "redundant":
        row["redundant_with_candidate_id"] = "note:2"
    fake = FakeGenerator(
        [{"rows": [row]}]
        if delta == 0
        else [{"rows": [row]}, {"rows": [_row(bundle, "keep", 10_000)]}]
    )
    cheap, thorough = _routes()
    result = R7ClassificationService(StructuredTextService(fake)).classify(
        bundles=(bundle,), strictness=strictness, cheap_route=cheap, thorough_route=thorough
    )
    assert len(fake.calls) == (1 if delta == 0 else 2)
    assert result.payload["final_partition"][0]["disposition"] == (
        disposition if delta == 0 else "keep"
    )


def test_r7_same_note_different_facts_remain_independent() -> None:
    first, second = _bundle(1, "fact-1"), _bundle(1, "fact-2")
    fake = FakeGenerator(
        [
            {"rows": [_row(first, "keep", 9000), _row(second, "needs_review", 0)]},
            {"rows": [_row(second, "exclude", 7500)]},
        ]
    )
    cheap, thorough = _routes()
    result = R7ClassificationService(StructuredTextService(fake)).classify(
        bundles=(first, second), strictness="strict", cheap_route=cheap, thorough_route=thorough
    )
    assert [row["bundle_id"] for row in result.payload["final_partition"]] == [
        first.bundle_id,
        second.bundle_id,
    ]
    assert [row["disposition"] for row in result.payload["final_partition"]] == ["keep", "exclude"]
    assert fake.calls[1]["input"]["bundles"][0]["fact_id"] == "fact-2"


@pytest.mark.parametrize("fault", ["candidate", "citation", "redundancy"])
def test_r7_invented_row_references_escalate_only_the_affected_row(fault: str) -> None:
    first, second = _bundle(1, "fact-1"), _bundle(2, "fact-2")
    invalid = _row(first, "keep", 10_000)
    if fault == "candidate":
        invalid["candidate_id"] = "note:999"
    elif fault == "citation":
        invalid["supporting_passage_ids"] = ["invented"]
    else:
        invalid["disposition"] = "redundant"
        invalid["redundant_with_candidate_id"] = "note:999"
    fake = FakeGenerator(
        [
            {"rows": [invalid, _row(second, "keep", 9000)]},
            {"rows": [_row(first, "keep", 9000)]},
        ]
    )
    cheap, thorough = _routes()
    result = R7ClassificationService(StructuredTextService(fake)).classify(
        bundles=(first, second), strictness="strict", cheap_route=cheap, thorough_route=thorough
    )
    assert [item["candidate"]["note_id"] for item in fake.calls[1]["input"]["bundles"]] == [1]
    assert result.payload["escalations"][0]["reasons"] == ["contract_invalid"]


@pytest.mark.parametrize("fault", ["low", "conflict", "invalid"])
def test_r7_thorough_nonterminal_results_become_caller_unresolved(fault: str) -> None:
    bundle = _bundle(1, "fact-1")
    thorough = _row(bundle, "keep", 9000)
    if fault == "low":
        thorough["confidence_bps"] = 1
    elif fault == "conflict":
        thorough["supporting_passage_ids"] = []
        thorough["conflicting_passage_ids"] = ["passage-1"]
    else:
        thorough["candidate_id"] = "note:999"
    fake = FakeGenerator([{"rows": [_row(bundle, "needs_review", 0)]}, {"rows": [thorough]}])
    cheap, thorough_route = _routes()
    result = R7ClassificationService(StructuredTextService(fake)).classify(
        bundles=(bundle,),
        strictness="strict",
        cheap_route=cheap,
        thorough_route=thorough_route,
    )
    final = result.payload["final_partition"][0]
    assert final["disposition"] == "unresolved"
    assert final["reason"] == "caller-authored unresolved"


@pytest.mark.parametrize("response", ["not json", {"rows": [{}]}])
def test_r7_malformed_json_or_schema_is_blocking_without_repair(response: object) -> None:
    bundle = _bundle(1, "fact-1")
    fake = FakeGenerator([response])
    cheap, thorough = _routes()
    result = R7ClassificationService(StructuredTextService(fake)).classify(
        bundles=(bundle,), strictness="strict", cheap_route=cheap, thorough_route=thorough
    )
    assert result.payload["blocking"] is True
    assert result.payload["final_partition"][0]["disposition"] == "unresolved"


@pytest.mark.parametrize(
    "rows",
    [
        lambda bundle: [],
        lambda bundle: [_row(bundle, "keep", 9000), _row(bundle, "keep", 9000)],
        lambda bundle: [{**_row(bundle, "keep", 9000), "bundle_id": "extra"}],
    ],
)
def test_r7_nonpartition_batch_is_blocking(rows: object) -> None:
    bundle = _bundle(1, "fact-1")
    fake = FakeGenerator([{"rows": rows(bundle)}])  # type: ignore[operator]
    cheap, thorough = _routes()
    result = R7ClassificationService(StructuredTextService(fake)).classify(
        bundles=(bundle,), strictness="strict", cheap_route=cheap, thorough_route=thorough
    )
    assert result.payload["blocking"] is True


def _authorization(
    bundle: CandidateEvidenceBundle, request: str, **changes: object
) -> dict[str, object]:
    value: dict[str, object] = {
        "policy_sha256": bundle.policy_sha256,
        "rate_table_sha256": "c" * 64,
        "estimator_version": "utf8-byte-upper-bound-v1",
        "repair_request_sha256": request,
        "predicted_total_before_repair_microusd": 1,
        "predicted_repair_cost_microusd": 1,
        "predicted_total_after_repair_microusd": 2,
    }
    value.update(changes)
    value["authorization_sha256"] = canonical_payload_sha256(value)
    return value


@pytest.mark.parametrize(
    "changes",
    [
        {"policy_sha256": "d" * 64},
        {"rate_table_sha256": "d" * 64},
        {"estimator_version": "other"},
        {"repair_request_sha256": "e" * 64},
        {"predicted_repair_cost_microusd": -1},
        {"predicted_repair_cost_microusd": True},
        {"predicted_total_after_repair_microusd": 3},
        {"predicted_total_after_repair_microusd": 3, "predicted_total_before_repair_microusd": 2},
    ],
)
def test_r7_repair_authorization_denials(changes: dict[str, object]) -> None:
    bundle = _bundle(1, "fact-1")
    request = "d" * 64
    authorization = _authorization(bundle, request, **changes)
    assert not _valid_repair_authorization(
        authorization, request, bundle.policy_sha256, "c" * 64, 2, 2
    )


@pytest.mark.parametrize("ordinary,hard", [(2, 3), (3, 2)])
def test_r7_repair_authorization_respects_both_cost_limits(ordinary: int, hard: int) -> None:
    bundle = _bundle(1, "fact-1")
    authorization = _authorization(
        bundle,
        "d" * 64,
        predicted_repair_cost_microusd=2,
        predicted_total_after_repair_microusd=3,
    )
    assert not _valid_repair_authorization(
        authorization, "d" * 64, bundle.policy_sha256, "c" * 64, ordinary, hard
    )


def test_r7_scopes_are_serial_and_repair_reuses_failed_ordinal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle(1, "fact-1")
    request = canonical_payload_sha256(
        _provider_input(
            "cheap",
            (bundle,),
            repair_error="R7 response does not partition requested bundles",
            invalid_response=[],
        )
    )
    scopes: list[dict[str, object]] = []

    @contextmanager
    def scope(**kwargs: object):
        scopes.append(kwargs)
        yield

    monkeypatch.setattr(classification_v3, "provider_call_scope", scope)
    fake = FakeGenerator([{"rows": []}, {"rows": [_row(bundle, "keep", 9000)]}])
    cheap, thorough = _routes()
    R7ClassificationService(StructuredTextService(fake)).classify(
        bundles=(bundle,),
        strictness="strict",
        cheap_route=cheap,
        thorough_route=thorough,
        repair_authorization=_authorization(bundle, request),
        rate_table_sha256="c" * 64,
        ordinary_limit_microusd=2,
        hard_limit_microusd=2,
    )
    assert scopes == [
        {
            "batch_index": 0,
            "batch_note_ids": (1,),
            "kind": "primary",
            "subcall_ordinal": 0,
            "defer_acceptance": True,
        },
        {
            "batch_index": 0,
            "batch_note_ids": (1,),
            "kind": "repair",
            "subcall_ordinal": 1,
            "defer_acceptance": True,
        },
    ]


def test_r7_only_one_actual_repair_across_multiple_cheap_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundles = tuple(_bundle(note_id, f"fact-{note_id:02d}") for note_id in range(1, 18))
    request = canonical_payload_sha256(
        _provider_input(
            "cheap",
            bundles[:16],
            repair_error="R7 response does not partition requested bundles",
            invalid_response=[],
        )
    )
    scopes: list[dict[str, object]] = []

    @contextmanager
    def scope(**kwargs: object):
        scopes.append(kwargs)
        yield

    monkeypatch.setattr(classification_v3, "provider_call_scope", scope)
    fake = FakeGenerator(
        [
            {"rows": []},
            {"rows": [_row(bundle, "keep", 9000) for bundle in bundles[:16]]},
            {"rows": []},
        ]
    )
    cheap, thorough = _routes()
    result = R7ClassificationService(StructuredTextService(fake)).classify(
        bundles=bundles,
        strictness="strict",
        cheap_route=cheap,
        thorough_route=thorough,
        repair_authorization=_authorization(bundles[0], request),
        rate_table_sha256="c" * 64,
        ordinary_limit_microusd=2,
        hard_limit_microusd=2,
    )
    assert [item["kind"] for item in scopes] == ["primary", "repair", "primary"]
    assert [item["batch_index"] for item in scopes] == [0, 0, 1]
    assert scopes[0]["batch_note_ids"] == tuple(range(1, 17))
    assert scopes[2]["batch_note_ids"] == (17,)
    assert result.payload["final_partition"][-1]["disposition"] == "unresolved"


def test_r7_runs_all_cheap_batches_before_monotonic_thorough_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundles = tuple(_bundle(note_id, f"fact-{note_id:02d}") for note_id in range(1, 18))
    scopes: list[dict[str, object]] = []

    @contextmanager
    def scope(**kwargs: object):
        scopes.append(kwargs)
        yield

    monkeypatch.setattr(classification_v3, "provider_call_scope", scope)
    fake = FakeGenerator(
        [
            {"rows": [_row(bundle, "needs_review", 0) for bundle in bundles[:16]]},
            {"rows": [_row(bundles[16], "needs_review", 0)]},
            {"rows": [_row(bundle, "keep", 9000) for bundle in bundles[:8]]},
            {"rows": [_row(bundle, "keep", 9000) for bundle in bundles[8:16]]},
            {"rows": [_row(bundles[16], "keep", 9000)]},
        ]
    )
    cheap, thorough = _routes()
    R7ClassificationService(StructuredTextService(fake)).classify(
        bundles=bundles, strictness="strict", cheap_route=cheap, thorough_route=thorough
    )
    assert [item["batch_index"] for item in scopes] == [0, 1, 2, 3, 4]
    assert [item["kind"] for item in scopes] == ["primary"] * 5
    assert scopes[0]["batch_note_ids"] == tuple(range(1, 17))
    assert scopes[1]["batch_note_ids"] == (17,)
