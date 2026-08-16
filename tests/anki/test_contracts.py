import json

import pytest
from pydantic import ValidationError

from oms_hub.anki.contracts import CreateCurationJobRequest, TagPatchContract
from oms_hub.anki.domain import (
    V3_REPLAY_IDENTITY_FIELDS,
    CreateCurationJob,
    ResolvedClassifierExecution,
    ResolvedModelConfiguration,
)


def _job_payload() -> dict[str, object]:
    return {
        "lecture_id": 7,
        "block_id": "heme-block-1",
        "source_revision_ids": [101, 102],
        "deck_allowlist": ["AnKing Step Deck"],
        "tag_allowlist": ["#AK_Step2_v12::Hematology"],
        "target_deck": "OMS-II_Custom_Cards::Heme::Lecture_4",
        "target_tag": "OMS::Heme::Lecture_4",
        "index_snapshot_id": "snapshot-1",
        "instruction_text": "Focus on lecture-emphasized mechanisms.",
        "lcl_prompt_version": "lcl-v4",
        "judgment_rubric_version": "judgment-v4",
        "gap_prompt_version": "gap-v4",
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "summary_outline_id": 91,
        "summary_outline_sha256": "b" * 64,
    }


def test_create_job_rejects_amboss_input() -> None:
    payload = _job_payload()
    payload["amboss_input"] = "legacy text"

    with pytest.raises(ValidationError):
        CreateCurationJobRequest.model_validate(payload)


def test_create_job_normalizes_scope_lists() -> None:
    payload = _job_payload()
    payload["deck_allowlist"] = [
        " AnKing Step Deck ",
        "Sketchy Pepper",
        "anking step deck",
    ]
    payload["tag_allowlist"] = ["#AK_Step2_v12::Hematology", "  "]

    request = CreateCurationJobRequest.model_validate(payload)

    assert request.deck_allowlist == ("AnKing Step Deck", "Sketchy Pepper")
    assert request.tag_allowlist == ("#AK_Step2_v12::Hematology",)
    assert request.source_revision_ids == (101, 102)
    assert request.summary_outline_id == 91
    assert request.summary_outline_sha256 == "b" * 64


def test_create_job_requires_complete_summary_pin() -> None:
    payload = _job_payload()
    payload["summary_outline_sha256"] = None

    with pytest.raises(ValidationError, match="summary outline"):
        CreateCurationJobRequest.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "unsupported"),
        ("source_revision_ids", []),
        ("deck_allowlist", []),
        ("target_tag", "unsafe tag"),
    ],
)
def test_create_job_rejects_invalid_run_scope(
    field: str,
    value: object,
) -> None:
    payload = _job_payload()
    payload[field] = value

    with pytest.raises(ValidationError):
        CreateCurationJobRequest.model_validate(payload)


def test_create_job_accepts_openrouter_provider() -> None:
    payload = _job_payload()
    payload["provider"] = "openrouter"

    request = CreateCurationJobRequest.model_validate(payload)

    assert request.provider == "openrouter"


def test_create_job_allows_omitted_model() -> None:
    payload = _job_payload()
    del payload["model"]

    request = CreateCurationJobRequest.model_validate(payload)

    assert request.model is None


def test_legacy_and_v2_model_documents_omit_v3_tier_fields() -> None:
    legacy = ResolvedModelConfiguration.card_centric_default(
        "anthropic", "claude-sonnet-5"
    ).canonical_document()
    v2 = ResolvedModelConfiguration.card_centric_v2_default(
        "anthropic", "claude-sonnet-5"
    ).canonical_document()
    v3_keys = {"scope_r3", "cheap_classify_r7", "thorough_classify_r7", "generation_r9"}
    assert not v3_keys & set(legacy)
    assert not v3_keys & set(v2)
    assert json.dumps(legacy, sort_keys=True, separators=(",", ":")) == (
        '{"classify_s4":{"fixture_validation_signature":null,"model":"claude-sonnet-5",'
        '"provider":"anthropic","thinking_mode":"disabled"},"gap_fill_s7":'
        '{"fixture_validation_signature":null,"model":"claude-sonnet-5",'
        '"provider":"anthropic","thinking_mode":"default"},"ledger_s2":'
        '{"fixture_validation_signature":null,"model":"claude-sonnet-5",'
        '"provider":"anthropic","thinking_mode":"default"},"profile":"card_centric_default",'
        '"residual_s6":{"fixture_validation_signature":null,"model":"claude-sonnet-5",'
        '"provider":"anthropic","thinking_mode":"disabled"},"residual_unlocked":false}'
    )


def test_v3_only_fields_are_rejected_for_legacy_requests_and_domain_calls() -> None:
    payload = _job_payload()
    payload["resolved_model_config"] = {
        **ResolvedModelConfiguration.card_centric_default(
            "anthropic", "claude-sonnet-5"
        ).canonical_document(),
        "scope_r3": {"provider": "anthropic", "model": "claude-sonnet-5"},
    }
    with pytest.raises(ValidationError, match="v3 model-tier"):
        CreateCurationJobRequest.model_validate(payload)
    with pytest.raises(ValueError, match="v3-only"):
        CreateCurationJob(
            lecture_id=1, block_id=None, source_revision_ids=(1,), deck_allowlist=("Deck",),
            tag_allowlist=("tag",), instruction_text="", target_deck="Deck", target_tag="tag",
            index_snapshot_id="index", lcl_prompt_version="lcl", judgment_rubric_version="judge",
            gap_prompt_version="gap", provider="anthropic", model="claude-sonnet-5",
            policy_sha256="a" * 64,
        )


def test_phase_h_replay_binding_fields_are_frozen_in_order() -> None:
    assert V3_REPLAY_IDENTITY_FIELDS == (
        "policy_sha256", "policy_revision", "style_fidelity_sha256", "scope_sha256",
        "lexical_generation", "semantic_generation", "retrieval_calibration_sha256",
        "evidence_bundle_sha256", "model_tier_escalation_identity", "cost_policy_rate_table_sha256",
    )


def test_create_job_rejects_blank_model() -> None:
    payload = _job_payload()
    payload["model"] = "   "

    with pytest.raises(ValidationError):
        CreateCurationJobRequest.model_validate(payload)


def test_create_job_rejects_oversized_model() -> None:
    payload = _job_payload()
    payload["model"] = "x" * 201

    with pytest.raises(ValidationError):
        CreateCurationJobRequest.model_validate(payload)


def test_to_domain_uses_the_resolved_model_override() -> None:
    payload = _job_payload()
    payload["model"] = "claude-sonnet-5"

    request = CreateCurationJobRequest.model_validate(payload)
    domain = request.to_domain(model="resolved-default-model")

    assert domain.model == "resolved-default-model"


@pytest.mark.parametrize(
    ("provider", "model"),
    (
        ("openai", "gpt-5.2"),
        ("anthropic", "claude-sonnet-5"),
        ("gemini", "gemini-3-flash"),
    ),
)
def test_v2_job_accepts_an_approved_persisted_fast_classifier_route(
    provider: str, model: str
) -> None:
    payload = _job_payload()
    payload["pipeline_contract_version"] = "card_centric_v2"
    config = ResolvedModelConfiguration.card_centric_v2_default(
        "anthropic", "claude-sonnet-5"
    ).canonical_document()
    config["fast_classify_s4b"] = {
        **config["fast_classify_s4b"],
        "provider": provider,
        "model": model,
    }
    payload["resolved_model_config"] = config

    request = CreateCurationJobRequest.model_validate(payload)

    domain = request.to_domain(model="claude-sonnet-5")

    assert domain.resolved_model_config is not None
    assert domain.resolved_model_config.fast_classify_s4b is not None
    assert domain.resolved_model_config.fast_classify_s4b.provider == provider
    assert domain.resolved_model_config.fast_classify_s4b.model == model
    assert domain.resolved_model_config.classifier_execution == ResolvedClassifierExecution()


@pytest.mark.parametrize(
    ("provider", "model", "thinking_mode", "error"),
    (
        ("openrouter", "openai/gpt-4o-mini", "disabled", "S4b requires an approved provider"),
        ("unsupported", "model", "disabled", "provider is unsupported"),
        ("   ", "model", "disabled", "values cannot be blank"),
        ("openai", "   ", "disabled", "values cannot be blank"),
        ("gemini", "gemini-3-flash", "default", "S4b requires an approved provider"),
        ("anthropic", "claude-sonnet-5", "enabled", "S4b requires an approved provider"),
    ),
)
def test_v2_job_rejects_an_unapproved_fast_classifier_route(
    provider: str, model: str, thinking_mode: str, error: str
) -> None:
    payload = _job_payload()
    payload["pipeline_contract_version"] = "card_centric_v2"
    config = ResolvedModelConfiguration.card_centric_v2_default(
        "anthropic", "claude-sonnet-5"
    ).canonical_document()
    config["fast_classify_s4b"] = {
        **config["fast_classify_s4b"],
        "provider": provider,
        "model": model,
        "thinking_mode": thinking_mode,
    }
    payload["resolved_model_config"] = config

    request = CreateCurationJobRequest.model_validate(payload)

    with pytest.raises(ValueError, match=error):
        request.to_domain(model="claude-sonnet-5")


def test_v2_job_keeps_the_legacy_openai_fast_classifier_route_as_the_default() -> None:
    payload = _job_payload()
    payload["pipeline_contract_version"] = "card_centric_v2"

    request = CreateCurationJobRequest.model_validate(payload)
    domain = request.to_domain(model="claude-sonnet-5")

    assert domain.resolved_model_config is not None
    assert domain.resolved_model_config.fast_classify_s4b is not None
    assert domain.resolved_model_config.fast_classify_s4b.provider == "openai"
    assert domain.resolved_model_config.fast_classify_s4b.model == "gpt-4o-mini"
    assert domain.resolved_model_config.classifier_execution == ResolvedClassifierExecution()


def test_v2_request_normalizes_omitted_classifier_execution_to_frozen_defaults() -> None:
    payload = _job_payload()
    payload["pipeline_contract_version"] = "card_centric_v2"
    config = ResolvedModelConfiguration.card_centric_v2_default(
        "anthropic", "claude-sonnet-5"
    ).canonical_document()
    config.pop("classifier_execution")
    payload["resolved_model_config"] = config

    domain = CreateCurationJobRequest.model_validate(payload).to_domain(
        model="claude-sonnet-5"
    )

    assert domain.resolved_model_config is not None
    assert domain.resolved_model_config.classifier_execution == ResolvedClassifierExecution()


def test_v2_request_rejects_explicit_null_classifier_execution() -> None:
    payload = _job_payload()
    payload["pipeline_contract_version"] = "card_centric_v2"
    config = ResolvedModelConfiguration.card_centric_v2_default(
        "anthropic", "claude-sonnet-5"
    ).canonical_document()
    config["classifier_execution"] = None
    payload["resolved_model_config"] = config

    request = CreateCurationJobRequest.model_validate(payload)
    with pytest.raises(ValueError, match="classifier execution configuration must be an object"):
        request.to_domain(model="claude-sonnet-5")


def test_tag_patch_round_trips_exact_diff() -> None:
    patch = TagPatchContract(
        note_id=42,
        before=("lecture::03", "source::ankihub"),
        after=("lecture::03", "review::high_yield", "source::ankihub"),
        add_tags=("review::high_yield",),
        remove_tags=(),
        expected_tag_hash="a" * 64,
        tag_policy_version="tag-policy-v1",
    )

    assert TagPatchContract.model_validate(patch.model_dump()) == patch


def test_tag_patch_rejects_inexact_or_conflicting_diff() -> None:
    with pytest.raises(ValidationError):
        TagPatchContract(
            note_id=42,
            before=("lecture::03",),
            after=("lecture::03",),
            add_tags=("review::high_yield",),
            remove_tags=("review::high_yield",),
            expected_tag_hash="a" * 64,
            tag_policy_version="tag-policy-v1",
        )


def test_tag_patch_contract_normalizes_surrounding_whitespace() -> None:
    patch = TagPatchContract(
        note_id=42,
        before=(" OMS::Old ",),
        after=("OMS::New",),
        add_tags=(" OMS::New ",),
        remove_tags=("OMS::Old",),
        expected_tag_hash="a" * 64,
        tag_policy_version="tags-v1",
    )

    assert patch.before == ("OMS::Old",)
    assert patch.add_tags == ("OMS::New",)
