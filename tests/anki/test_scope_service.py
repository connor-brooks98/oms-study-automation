import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from oms_hub.anki.cost_estimator import FrozenRateTable, ModelRate
from oms_hub.anki.course_policy import CourseCurationPolicy, PolicyEmphasisColor
from oms_hub.anki.domain import (
    CurationStage,
    PipelineContractVersion,
    ResolvedStageModel,
    SourceKind,
)
from oms_hub.anki.fidelity_audit import R2FidelityDiagnostic
from oms_hub.anki.pipeline import PinnedInputChanged
from oms_hub.anki.prompt_catalog import AnkiPromptCatalogService
from oms_hub.anki.scope_contracts import LectureScope
from oms_hub.anki.scope_service import (
    PinnedScopePrompt,
    ScopeInputError,
    ScopeReuseArtifact,
    ScopeService,
    _scope_output_model,
)
from oms_hub.anki.sources import SourceEmphasisEvidence, SourcePassage
from oms_hub.anki.stages import (
    CurationServicesRunner,
    _r3_passages,
    _r3_policy,
    _r3_prompt,
)
from oms_hub.llm.domain import GeneratedText, ProviderName
from oms_hub.llm.structured import StructuredOutputError, StructuredTextService

_SOURCE_SHA = "a" * 64
_SIDECAR_SHA = "b" * 64
_MODEL_CONFIG_SHA = "c" * 64
_ROUTE = ResolvedStageModel("openai", "scope-fixture", "disabled")


def test_r3_provider_schema_has_bounded_output_cardinality() -> None:
    schema = _scope_output_model({"evidence"}).model_json_schema()
    concept = schema["$defs"]["SemanticConcept"]
    fact = schema["$defs"]["SemanticFact"]

    assert schema["properties"]["concepts"]["maxItems"] == 24
    assert concept["properties"]["facts"]["maxItems"] == 3
    assert concept["properties"]["retrieval_queries"]["maxItems"] == 4
    assert fact["properties"]["evidence_ids"]["maxItems"] == 12


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


class FakeGenerator:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def generate_text(self, instruction: str, input_text: str, **kwargs: Any) -> GeneratedText:
        self.calls.append({"instruction": instruction, "input_text": input_text, **kwargs})
        response = self.response(input_text) if callable(self.response) else self.response
        text = response if isinstance(response, str) else json.dumps(response)
        return GeneratedText(text, ProviderName.OPENAI, "scope-fixture", "request-1", 7, 9, 11)


def _policy(
    mode: str = "colored_text",
    fallback: str = "block",
) -> CourseCurationPolicy:
    colors = (
        (PolicyEmphasisColor(rgb="FF0000", label="high yield"),)
        if mode
        in {
            "colored_text",
            "combined",
        }
        else ()
    )
    return CourseCurationPolicy(
        policy_id="course-policy",
        revision=3,
        course_id="course-1",
        professor_label="Professor",
        scope_instruction="Use authoritative lecture evidence.",
        emphasis_mode=mode,  # type: ignore[arg-type]
        emphasis_colors=colors,
        missing_emphasis_fallback=fallback,  # type: ignore[arg-type]
        tag_scope_mode="hard_filter",
        classification_strictness="strict",
        generation_style_profile="concise",
        ordinary_cost_limit_microusd=10_000_000,
        hard_stop_cost_limit_microusd=10_000_000,
    )


def _passage(kind: SourceKind, locator: str) -> SourcePassage:
    return SourcePassage.create(
        revision_id=1,
        lecture_id=1,
        artifact_id="artifact",
        source_kind=kind,
        locator=locator,
        text=f"{kind.value} evidence",
        source_id=f"{kind.value}:{locator}",
    )


def _emphasis(policy: CourseCurationPolicy) -> SourceEmphasisEvidence:
    text = "colored evidence"
    return SourceEmphasisEvidence(
        source_id="slide:1",
        revision_id=1,
        source_kind=SourceKind.SLIDE,
        source_sha256=_SOURCE_SHA,
        sidecar_sha256=_SIDECAR_SHA,
        locator="slide:1:run:1",
        text=text,
        normalized_text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        normalized_color="FF0000",
        policy_sha256=policy.policy_sha256,
    )


def _fidelity(
    policy: CourseCurationPolicy,
    status: str,
    *,
    transcript_count: int = 0,
    outline_count: int = 0,
) -> R2FidelityDiagnostic:
    continuing = status in {"continue", "continue_degraded", "not_applicable"}
    return R2FidelityDiagnostic(
        source_sha256=_SOURCE_SHA,
        sidecar_sha256=_SIDECAR_SHA,
        policy_sha256=policy.policy_sha256,
        matching_colored_count=1 if status == "continue" else 0,
        nonmatching_colored_count=0,
        unresolved_color_count=0,
        transcript_count=transcript_count,
        outline_count=outline_count,
        status=status,  # type: ignore[arg-type]
        may_advance=continuing,
        degraded_mode="transcript_outline" if status == "continue_degraded" else None,
    )


def _prompt() -> PinnedScopePrompt:
    snapshot = AnkiPromptCatalogService().load_card_centric_v3_scope_snapshot()
    return PinnedScopePrompt.from_prompt(snapshot.require("card-centric-scope-v3"))


def _response(input_text: str) -> dict[str, object]:
    evidence_id = json.loads(input_text)["source_bundle"]["evidence"][0]["evidence_id"]
    return {
        "concepts": [
            {
                "canonical_statement": "  Iron deficiency anemia  ",
                "primary_entity": "  iron deficiency  ",
                "aliases": ["IDA", "anemia"],
                "exact_terms": ["IDA"],
                "depth_tier": 1,
                "priority": 90,
                "reason": "  Policy emphasis.  ",
                "facts": [
                    {
                        "statement": "  Iron deficiency can cause microcytosis.  ",
                        "evidence_ids": [evidence_id],
                        "generation_allowed": True,
                        "forbidden_cloze_targets": ["iron"],
                    }
                ],
                "source_evidence_ids": [evidence_id],
                "professor_policy_basis": ["colored text"],
                "retrieval_queries": ["iron deficiency anemia"],
            }
        ]
    }


def _generate(
    policy: CourseCurationPolicy,
    fidelity: R2FidelityDiagnostic,
    passages: tuple[SourcePassage, ...],
    emphasis: tuple[SourceEmphasisEvidence, ...],
    response: object = _response,
    *,
    existing: ScopeReuseArtifact | None = None,
    prompt: PinnedScopePrompt | None = None,
    route: ResolvedStageModel = _ROUTE,
) -> tuple[FakeGenerator, object]:
    generator = FakeGenerator(response)
    result = ScopeService(StructuredTextService(generator)).generate_scope(
        policy=policy,
        fidelity=fidelity,
        source_passages=passages,
        emphasis_evidence=emphasis,
        prompt=prompt or _prompt(),
        route=route,
        model_config_sha256=_MODEL_CONFIG_SHA,
        existing=existing,
    )
    return generator, result


def test_r3_colored_runs_bind_exact_source_locator_and_reject_ambiguous_provenance() -> None:
    policy = _policy()
    fidelity = _fidelity(policy, "continue")
    fidelity = fidelity.model_copy(update={"matching_colored_count": 2, "diagnostic_sha256": ""})
    first = _emphasis(policy)
    second = SourceEmphasisEvidence.model_validate(
        {
            **first.canonical_payload(),
            "locator": "slide:1:run:2",
            "text": "second colored run",
            "normalized_text_sha256": hashlib.sha256(b"second colored run").hexdigest(),
        }
    )
    passages = (
        SourcePassage.create(
            revision_id=1,
            lecture_id=1,
            artifact_id="artifact",
            source_kind=SourceKind.SLIDE,
            locator=first.locator,
            text=first.text,
            source_id=first.source_id,
        ),
        SourcePassage.create(
            revision_id=1,
            lecture_id=1,
            artifact_id="artifact",
            source_kind=SourceKind.SLIDE,
            locator=second.locator,
            text=second.text,
            source_id=second.source_id,
        ),
    )
    generator = FakeGenerator(_response)
    result = ScopeService(StructuredTextService(generator)).generate_scope(
        policy=policy,
        fidelity=fidelity,
        source_passages=passages,
        emphasis_evidence=(first, second),
        prompt=_prompt(),
        route=_ROUTE,
        model_config_sha256=_MODEL_CONFIG_SHA,
        require_v3_provenance=True,
    )
    assert [item["locator"] for item in result.source_bundle["evidence"]] == [
        first.locator,
        second.locator,
    ] and len(generator.calls) == 1
    for bad_passages in (
        (*passages, passages[0]),
        (
            SourcePassage.create(
                revision_id=1,
                lecture_id=1,
                artifact_id="artifact",
                source_kind=SourceKind.TRANSCRIPT,
                locator=first.locator,
                text=first.text,
                source_id=first.source_id,
            ),
            passages[1],
        ),
        (
            SourcePassage.create(
                revision_id=2,
                lecture_id=1,
                artifact_id="artifact",
                source_kind=SourceKind.SLIDE,
                locator=first.locator,
                text=first.text,
                source_id=first.source_id,
            ),
            passages[1],
        ),
        (
            SourcePassage.create(
                revision_id=1,
                lecture_id=1,
                artifact_id="artifact",
                source_kind=SourceKind.SLIDE,
                locator=first.locator,
                text="wrong",
                source_id=first.source_id,
            ),
            passages[1],
        ),
    ):
        blocked = FakeGenerator(_response)
        with pytest.raises(ScopeInputError):
            ScopeService(StructuredTextService(blocked)).generate_scope(
                policy=policy,
                fidelity=fidelity,
                source_passages=bad_passages,
                emphasis_evidence=(first, second),
                prompt=_prompt(),
                route=_ROUTE,
                model_config_sha256=_MODEL_CONFIG_SHA,
                require_v3_provenance=True,
            )
        assert blocked.calls == []


@pytest.mark.parametrize(
    ("mode", "status", "expected_types"),
    [
        ("colored_text", "continue", {"colored_text"}),
        ("combined", "continue", {"colored_text", "transcript", "outline"}),
        ("transcript_emphasis", "not_applicable", {"transcript"}),
        ("outline_depth", "not_applicable", {"outline"}),
    ],
)
def test_scope_selects_only_policy_authorized_evidence(
    mode: str,
    status: str,
    expected_types: set[str],
) -> None:
    policy = _policy(mode)
    emphasis = (_emphasis(policy),) if mode in {"colored_text", "combined"} else ()
    passages = (
        _passage(SourceKind.SLIDE, "slide:1"),
        _passage(SourceKind.SPEAKER_NOTES, "slide:1:notes"),
        _passage(SourceKind.VISION, "slide:1:image"),
        _passage(SourceKind.TRANSCRIPT, "transcript:1"),
        _passage(SourceKind.SUMMARY, "summary:core:1"),
    )
    generator, result = _generate(
        policy,
        _fidelity(policy, status, transcript_count=1, outline_count=1),
        passages,
        emphasis,
    )

    assert len(generator.calls) == 1
    evidence = result.provider_input["source_bundle"]["evidence"]  # type: ignore[index]
    assert {item["evidence_type"] for item in evidence} == expected_types
    ids = {item["evidence_id"] for item in evidence}
    if mode == "combined":
        assert ids == {emphasis[0].evidence_id, passages[3].passage_id, passages[4].passage_id}
    elif mode == "transcript_emphasis":
        assert ids == {passages[3].passage_id}
    elif mode == "outline_depth":
        assert ids == {passages[4].passage_id}
    else:
        assert ids == {emphasis[0].evidence_id}


def test_degraded_scope_excludes_colored_evidence() -> None:
    policy = _policy("combined", "transcript_outline")
    emphasis = ()
    passages = (
        _passage(SourceKind.TRANSCRIPT, "transcript:1"),
        _passage(SourceKind.SUMMARY, "summary:core:1"),
    )
    _, result = _generate(
        policy,
        _fidelity(policy, "continue_degraded", transcript_count=1, outline_count=1),
        passages,
        emphasis,
    )

    evidence = result.provider_input["source_bundle"]["evidence"]  # type: ignore[index]
    assert result.scope.degraded_mode == "transcript_outline"
    assert {item["evidence_id"] for item in evidence} == {item.passage_id for item in passages}


@pytest.mark.parametrize(
    "status",
    ["blocked", "confirmation_required", "blocked_fallback_unavailable"],
)
def test_blocked_scope_inputs_do_not_call_provider(status: str) -> None:
    policy = _policy()
    generator = FakeGenerator(_response)
    with pytest.raises(ScopeInputError, match="fidelity blocks"):
        ScopeService(StructuredTextService(generator)).generate_scope(
            policy=policy,
            fidelity=_fidelity(
                policy,
                status,
                transcript_count=0,
                outline_count=0,
            ),
            source_passages=(),
            emphasis_evidence=(),
            prompt=_prompt(),
            route=_ROUTE,
            model_config_sha256=_MODEL_CONFIG_SHA,
        )
    assert generator.calls == []


def test_empty_authorized_evidence_and_identity_mismatch_do_not_call_provider() -> None:
    policy = _policy("transcript_emphasis")
    generator = FakeGenerator(_response)
    service = ScopeService(StructuredTextService(generator))
    with pytest.raises(ScopeInputError, match="no policy-authorized"):
        service.generate_scope(
            policy=policy,
            fidelity=_fidelity(policy, "not_applicable"),
            source_passages=(),
            emphasis_evidence=(),
            prompt=_prompt(),
            route=_ROUTE,
            model_config_sha256=_MODEL_CONFIG_SHA,
        )
    other = _policy("combined")
    with pytest.raises(ScopeInputError, match="policy identity"):
        service.generate_scope(
            policy=policy,
            fidelity=_fidelity(other, "continue"),
            source_passages=(),
            emphasis_evidence=(),
            prompt=_prompt(),
            route=_ROUTE,
            model_config_sha256=_MODEL_CONFIG_SHA,
        )
    assert generator.calls == []


def test_contract_rejects_escaped_citations_and_normalized_duplicates_before_acceptance() -> None:
    policy = _policy()
    emphasis = (_emphasis(policy),)
    escaped = _response(json.dumps({"source_bundle": {"evidence": [{"evidence_id": "ok"}]}}))
    escaped["concepts"][0]["facts"][0]["evidence_ids"] = ["outside"]  # type: ignore[index]
    generator = FakeGenerator(escaped)
    with pytest.raises(StructuredOutputError, match="outside the authorized"):
        ScopeService(StructuredTextService(generator)).generate_scope(
            policy=policy,
            fidelity=_fidelity(policy, "continue"),
            source_passages=(),
            emphasis_evidence=emphasis,
            prompt=_prompt(),
            route=_ROUTE,
            model_config_sha256=_MODEL_CONFIG_SHA,
        )
    assert len(generator.calls) == 1

    def duplicate_concepts(input_text: str) -> dict[str, object]:
        payload = _response(input_text)
        duplicate_concept = deepcopy(payload["concepts"][0])  # type: ignore[index]
        duplicate_concept["canonical_statement"] = " iron  deficiency anemia "
        payload["concepts"].append(duplicate_concept)  # type: ignore[index]
        return payload

    generator = FakeGenerator(duplicate_concepts)
    with pytest.raises(StructuredOutputError, match="concept statements"):
        ScopeService(StructuredTextService(generator)).generate_scope(
            policy=policy,
            fidelity=_fidelity(policy, "continue"),
            source_passages=(),
            emphasis_evidence=emphasis,
            prompt=_prompt(),
            route=_ROUTE,
            model_config_sha256=_MODEL_CONFIG_SHA,
        )
    assert len(generator.calls) == 1


def test_scope_ids_and_hashes_are_stable_for_reordered_evidence_and_follow_provider_order() -> None:
    policy = _policy("combined")
    passages = (
        _passage(SourceKind.TRANSCRIPT, "transcript:1"),
        _passage(SourceKind.SUMMARY, "summary:core:1"),
    )
    emphasis = (_emphasis(policy),)
    _, first = _generate(
        policy,
        _fidelity(policy, "continue", transcript_count=1, outline_count=1),
        passages,
        emphasis,
    )
    _, reordered = _generate(
        policy,
        _fidelity(policy, "continue", transcript_count=1, outline_count=1),
        tuple(reversed(passages)),
        emphasis,
    )
    assert first.scope.scope_id == reordered.scope.scope_id
    assert first.scope.scope_sha256 == reordered.scope.scope_sha256

    def two_concepts(input_text: str) -> dict[str, object]:
        base = _response(input_text)["concepts"][0]
        second = dict(base)
        second["canonical_statement"] = "Second concept"
        second["facts"] = [dict(base["facts"][0], statement="Second fact")]
        return {"concepts": [second, base]}

    _, changed = _generate(
        policy,
        _fidelity(policy, "continue", transcript_count=1, outline_count=1),
        passages,
        emphasis,
        two_concepts,
    )
    assert [item.concept_id for item in changed.scope.concepts] == [
        "concept-00000001",
        "concept-00000002",
    ]
    assert changed.scope.concepts[0].canonical_statement == "Second concept"
    assert changed.scope.scope_sha256 != first.scope.scope_sha256


def test_exact_reuse_calls_no_provider_and_mismatch_regenerates() -> None:
    policy = _policy()
    emphasis = (_emphasis(policy),)
    _, original = _generate(policy, _fidelity(policy, "continue"), (), emphasis)
    reuse = ScopeReuseArtifact(original.scope, original.scope_request_sha256)
    generator, reused = _generate(
        policy,
        _fidelity(policy, "continue"),
        (),
        emphasis,
        existing=reuse,
    )
    assert generator.calls == []
    assert reused.reused
    generator, regenerated = _generate(
        policy,
        _fidelity(policy, "continue"),
        (),
        emphasis,
        existing=ScopeReuseArtifact(original.scope, "d" * 64),
    )
    assert len(generator.calls) == 1
    assert not regenerated.reused
    different_identity = original.scope.model_dump(mode="json")
    different_identity["scope_id"] = "scope-other"
    different_identity["scope_sha256"] = ""
    generator, regenerated = _generate(
        policy,
        _fidelity(policy, "continue"),
        (),
        emphasis,
        existing=ScopeReuseArtifact(
            LectureScope.model_validate(different_identity),
            original.scope_request_sha256,
        ),
    )
    assert len(generator.calls) == 1
    assert not regenerated.reused


def test_invalid_json_is_one_call_without_repair() -> None:
    policy = _policy()
    generator = FakeGenerator("{")
    with pytest.raises(StructuredOutputError, match="invalid JSON"):
        ScopeService(StructuredTextService(generator)).generate_scope(
            policy=policy,
            fidelity=_fidelity(policy, "continue"),
            source_passages=(),
            emphasis_evidence=(_emphasis(policy),),
            prompt=_prompt(),
            route=_ROUTE,
            model_config_sha256=_MODEL_CONFIG_SHA,
        )
    assert len(generator.calls) == 1


def test_fidelity_count_and_namespace_collisions_stop_before_provider() -> None:
    policy = _policy("combined")
    passage = _passage(SourceKind.TRANSCRIPT, "transcript:1")
    emphasis = _emphasis(policy)
    generator = FakeGenerator(_response)
    service = ScopeService(StructuredTextService(generator))
    with pytest.raises(ScopeInputError, match="fidelity diagnostic"):
        service.generate_scope(
            policy=policy,
            fidelity=_fidelity(policy, "continue", transcript_count=0),
            source_passages=(passage,),
            emphasis_evidence=(emphasis,),
            prompt=_prompt(),
            route=_ROUTE,
            model_config_sha256=_MODEL_CONFIG_SHA,
        )
    collision = emphasis.model_copy(update={"evidence_id": passage.passage_id})
    with pytest.raises(ScopeInputError, match="conflicting scope evidence ID"):
        service.generate_scope(
            policy=policy,
            fidelity=_fidelity(policy, "continue", transcript_count=1),
            source_passages=(passage,),
            emphasis_evidence=(collision,),
            prompt=_prompt(),
            route=_ROUTE,
            model_config_sha256=_MODEL_CONFIG_SHA,
        )
    assert generator.calls == []


def test_normalized_duplicate_fact_and_validation_event_never_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()

    def duplicate_facts(input_text: str) -> dict[str, object]:
        payload = _response(input_text)
        fact = deepcopy(payload["concepts"][0]["facts"][0])  # type: ignore[index]
        fact["statement"] = " iron deficiency can cause   microcytosis. "
        payload["concepts"][0]["facts"].append(fact)  # type: ignore[index]
        return payload

    events: list[str] = []

    def record_event(_handle: object, event: str, **_kwargs: object) -> None:
        events.append(event)

    monkeypatch.setattr("oms_hub.llm.structured.emit_provider_event", record_event)
    generator = FakeGenerator(duplicate_facts)
    with pytest.raises(StructuredOutputError, match="fact statements"):
        ScopeService(StructuredTextService(generator)).generate_scope(
            policy=policy,
            fidelity=_fidelity(policy, "continue"),
            source_passages=(),
            emphasis_evidence=(_emphasis(policy),),
            prompt=_prompt(),
            route=_ROUTE,
            model_config_sha256=_MODEL_CONFIG_SHA,
        )
    assert events[-1] == "validation_failed"
    assert "accepted" not in events


def test_default_thinking_and_reuse_prompt_or_route_changes_regenerate() -> None:
    policy = _policy()
    emphasis = (_emphasis(policy),)
    _, original = _generate(policy, _fidelity(policy, "continue"), (), emphasis)
    reuse = ScopeReuseArtifact(original.scope, original.scope_request_sha256)
    generator, regenerated = _generate(
        policy,
        _fidelity(policy, "continue"),
        (),
        emphasis,
        existing=reuse,
        route=ResolvedStageModel("openai", "scope-fixture", "enabled"),
    )
    assert len(generator.calls) == 1
    assert not regenerated.reused
    changed_prompt = _prompt()
    changed_content = changed_prompt.content + "\nUse exact citations."
    changed_prompt = PinnedScopePrompt(
        id=changed_prompt.id,
        version=changed_prompt.version,
        content=changed_content,
        content_sha256=hashlib.sha256(changed_content.encode()).hexdigest(),
        metadata=changed_prompt.metadata,
    )
    generator, regenerated = _generate(
        policy,
        _fidelity(policy, "continue"),
        (),
        emphasis,
        existing=reuse,
        prompt=changed_prompt,
    )
    assert len(generator.calls) == 1
    assert not regenerated.reused
    generator = FakeGenerator(_response)
    with pytest.raises(ScopeInputError, match="enabled or disabled"):
        ScopeService(StructuredTextService(generator)).generate_scope(
            policy=policy,
            fidelity=_fidelity(policy, "continue"),
            source_passages=(),
            emphasis_evidence=emphasis,
            prompt=_prompt(),
            route=ResolvedStageModel("openai", "scope-fixture"),
            model_config_sha256=_MODEL_CONFIG_SHA,
        )
    assert generator.calls == []


def test_r3_payload_validators_require_full_pinned_identities() -> None:
    policy = _policy()
    prompt = _prompt()
    context = SimpleNamespace(
        job=SimpleNamespace(
            policy_sha256=policy.policy_sha256,
            model_config_sha256=_MODEL_CONFIG_SHA,
        )
    )
    r0 = {
        "policy": policy.model_dump(mode="json"),
        "policy_sha256": policy.policy_sha256,
        "policy_revision": policy.revision,
        "model_config_sha256": _MODEL_CONFIG_SHA,
        "prompt_snapshot": [
            {
                "id": prompt.id,
                "version": prompt.version,
                "content": prompt.content,
                "content_sha256": prompt.content_sha256,
                "metadata": prompt.metadata.model_dump(mode="json", by_alias=True),
            }
        ],
    }
    assert _r3_policy(context, r0) == policy
    assert _r3_prompt(r0) == prompt
    with pytest.raises(ScopeInputError, match="pinned scope prompt"):
        PinnedScopePrompt(
            id=prompt.id,
            version=prompt.version,
            content=prompt.content,
            content_sha256=prompt.content_sha256,
            metadata=prompt.metadata.model_copy(update={"model": "not-allowed"}),
        )
    with pytest.raises(PinnedInputChanged, match="policy identity"):
        _r3_policy(context, dict(r0, policy_revision=policy.revision + 1))
    with pytest.raises(PinnedInputChanged, match="scope prompt"):
        _r3_prompt(dict(r0, prompt_snapshot=[]))

    passage = _passage(SourceKind.TRANSCRIPT, "transcript:1")
    raw = {
        "passage_id": passage.passage_id,
        "source_id": passage.source_id,
        "revision_id": passage.revision_id,
        "lecture_id": passage.lecture_id,
        "artifact_id": passage.artifact_id,
        "source_kind": passage.source_kind.value,
        "locator": passage.locator,
        "text": passage.text,
        "content_hash": passage.content_hash,
        "extraction_status": passage.extraction_status,
        "slide_number": passage.slide_number,
        "start_seconds": passage.start_seconds,
        "end_seconds": passage.end_seconds,
        "summary_backrefs": list(passage.summary_backrefs),
        "summary_section": passage.summary_section,
    }
    assert _r3_passages({"passages": [raw]}) == (passage,)
    with pytest.raises(PinnedInputChanged, match="malformed"):
        _r3_passages({"passages": [dict(raw, content_hash="d" * 64)]})


def test_r3_scope_stage_validates_version_route_style_and_counts_before_call() -> None:
    policy = _policy()
    prompt = _prompt()
    emphasis = _emphasis(policy)
    fidelity = _fidelity(policy, "continue")
    route = {
        "provider": _ROUTE.provider,
        "model": _ROUTE.model,
        "thinking_mode": _ROUTE.thinking_mode,
        "fixture_validation_signature": _ROUTE.fixture_validation_signature,
    }
    r0 = {
        "policy": policy.model_dump(mode="json"),
        "policy_sha256": policy.policy_sha256,
        "policy_revision": policy.revision,
        "model_config_sha256": _MODEL_CONFIG_SHA,
        "scope_r3": route,
        "prompt_snapshot": [
            {
                "id": prompt.id,
                "version": prompt.version,
                "content": prompt.content,
                "content_sha256": prompt.content_sha256,
                "metadata": prompt.metadata.model_dump(mode="json", by_alias=True),
            }
        ],
    }
    _add_r0_costs(r0, _ROUTE.model)
    colored_passage = SourcePassage.create(
        revision_id=1,
        lecture_id=1,
        artifact_id="artifact",
        source_kind=SourceKind.SLIDE,
        locator=emphasis.locator,
        text=emphasis.text,
        source_id=emphasis.source_id,
    )
    r1 = {
        "passages": [
            {
                "passage_id": colored_passage.passage_id,
                "source_id": colored_passage.source_id,
                "revision_id": colored_passage.revision_id,
                "lecture_id": colored_passage.lecture_id,
                "artifact_id": colored_passage.artifact_id,
                "source_kind": colored_passage.source_kind.value,
                "locator": colored_passage.locator,
                "text": colored_passage.text,
                "content_hash": colored_passage.content_hash,
                "extraction_status": colored_passage.extraction_status,
                "slide_number": colored_passage.slide_number,
                "start_seconds": colored_passage.start_seconds,
                "end_seconds": colored_passage.end_seconds,
                "summary_backrefs": list(colored_passage.summary_backrefs),
                "summary_section": colored_passage.summary_section,
            }
        ],
        "emphasis_evidence": [emphasis.model_dump(mode="json")],
        "style_source_sha256": _SOURCE_SHA,
        "style_sidecar_sha256": _SIDECAR_SHA,
    }
    prior = {
        CurationStage.V3_R0_PREFLIGHT: r0,
        CurationStage.V3_R1_SOURCE_INDEX: r1,
        CurationStage.V3_R2_FIDELITY: {"fidelity_diagnostic": fidelity.model_dump(mode="json")},
    }

    def context(
        version: PipelineContractVersion,
        payloads: dict[object, object],
    ) -> SimpleNamespace:
        return SimpleNamespace(
            job=SimpleNamespace(
                pipeline_contract_version=version,
                id="scope-job",
                policy_sha256=policy.policy_sha256,
                model_config_sha256=_MODEL_CONFIG_SHA,
                resolved_model_config=SimpleNamespace(scope_r3=_ROUTE),
            ),
            prior_payloads=payloads,
            replay_inputs={},
        )

    runner = object.__new__(CurationServicesRunner)
    generator = FakeGenerator(_response)
    runner.structured = StructuredTextService(generator)
    with pytest.raises(PinnedInputChanged, match="requires the card_centric_v3"):
        asyncio.run(runner._v3_r3_scope(context(PipelineContractVersion.CARD_CENTRIC_V2, {})))
    assert generator.calls == []

    for changed in (
        {**r0, "scope_r3": {**route, "model": "wrong"}},
        r0,
    ):
        payloads = deepcopy(prior)
        payloads[CurationStage.V3_R0_PREFLIGHT] = changed
        if changed is r0:
            payloads[CurationStage.V3_R1_SOURCE_INDEX]["style_source_sha256"] = "d" * 64
        with pytest.raises(PinnedInputChanged):
            asyncio.run(
                runner._v3_r3_scope(context(PipelineContractVersion.CARD_CENTRIC_V3, payloads))
            )
    assert generator.calls == []

    passage = _passage(SourceKind.TRANSCRIPT, "transcript:count")
    count_payloads = deepcopy(prior)
    count_payloads[CurationStage.V3_R1_SOURCE_INDEX]["passages"] = [
        {
            "passage_id": passage.passage_id,
            "source_id": passage.source_id,
            "revision_id": passage.revision_id,
            "lecture_id": passage.lecture_id,
            "artifact_id": passage.artifact_id,
            "source_kind": passage.source_kind.value,
            "locator": passage.locator,
            "text": passage.text,
            "content_hash": passage.content_hash,
            "extraction_status": passage.extraction_status,
            "slide_number": passage.slide_number,
            "start_seconds": passage.start_seconds,
            "end_seconds": passage.end_seconds,
            "summary_backrefs": list(passage.summary_backrefs),
            "summary_section": passage.summary_section,
        }
    ]
    with pytest.raises(ScopeInputError, match="fidelity diagnostic"):
        asyncio.run(
            runner._v3_r3_scope(context(PipelineContractVersion.CARD_CENTRIC_V3, count_payloads))
        )
    assert generator.calls == []

    product = asyncio.run(
        runner._v3_r3_scope(context(PipelineContractVersion.CARD_CENTRIC_V3, prior))
    )
    assert product.kind == "card_centric_v3_scope"
    assert {"scope", "source_bundle", "scope_request_sha256", "route"} <= set(product.payload)
    assert len(generator.calls) == 1
