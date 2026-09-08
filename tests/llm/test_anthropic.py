import copy
import json

import httpx
import pytest
import respx

from oms_hub.anki.card_centric import s2_generation_parameters
from oms_hub.llm.anthropic import (
    AnthropicProvider,
    anthropic_output_schema,
    resolve_anthropic_model_capabilities,
)
from oms_hub.llm.domain import (
    DiagnosticSource,
    GenerationOptions,
    LLMRequestError,
    ProviderName,
    ThinkingCapability,
    ThinkingMode,
)
from oms_hub.study_generation.practice_contracts import ExtractionPayload
from oms_hub.transcripts.prompt import ApprovedPrompt


@respx.mock
def test_anthropic_provider_sends_messages_request_and_parses_usage():
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            headers={"request-id": "anthropic-request"},
            json={
                "id": "message-123",
                "model": "claude-sonnet-5",
                "content": [
                    {"type": "text", "text": "Cleaned Claude lecture."}
                ],
                "usage": {"input_tokens": 80, "output_tokens": 16},
            },
        )
    )
    provider = AnthropicProvider(
        input_usd_per_million=3.0,
        output_usd_per_million=15.0,
    )

    result = provider.clean(
        "Raw lecture.",
        ApprovedPrompt("Remove filler.", "a" * 64),
        api_key="secret",
        model="claude-sonnet-5",
    )

    request = route.calls.last.request
    assert request.headers["x-api-key"] == "secret"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert "thinking" not in json.loads(request.content)
    assert result.provider is ProviderName.ANTHROPIC
    assert result.text == "Cleaned Claude lecture."
    assert result.model == "claude-sonnet-5"
    assert result.request_id == "anthropic-request"
    assert result.input_tokens == 80
    assert result.output_tokens == 16
    assert result.cost_microusd == 480


@respx.mock
def test_anthropic_structured_generation_sends_output_config():
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            headers={"request-id": "anthropic-json"},
            json={
                "id": "message-json",
                "model": "claude-sonnet-5",
                "content": [
                    {"type": "text", "text": '{"answer":"iron"}'}
                ],
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
        )
    )

    result = AnthropicProvider().generate_text(
        "Return a grounded answer.",
        "Question",
        api_key="secret",
        model="claude-sonnet-5",
        output_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
        options=GenerationOptions(
            cacheable_source_prefix="SUM: iron is essential.",
            thinking=ThinkingMode.DISABLED,
            temperature=0,
            max_tokens=7000,
        ),
    )

    payload = json.loads(route.calls.last.request.content)
    assert "output_config" in payload
    assert "temperature" not in payload
    assert payload["max_tokens"] == 7000
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert result.text == '{"answer":"iron"}'


@respx.mock
def test_anthropic_sends_expanded_extraction_schema_without_mutating_source() -> None:
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "message-extraction-schema",
                "model": "claude-sonnet-5",
                "content": [
                    {"type": "text", "text": '{"questions":[],"answers":[]}'}
                ],
                "usage": {"input_tokens": 12, "output_tokens": 5},
            },
        )
    )
    schema = ExtractionPayload.model_json_schema()
    original = copy.deepcopy(schema)

    AnthropicProvider().generate_text(
        "Return questions.",
        "Question",
        api_key="secret",
        model="claude-sonnet-5",
        output_schema=schema,
    )

    payload = json.loads(route.calls.last.request.content)
    assert payload["output_config"]["format"]["schema"] == anthropic_output_schema(original)
    assert schema == original


@pytest.mark.parametrize(
    "model",
    (
        "claude-opus-4-7suffix",
        "claude-opus-4-8suffix",
        "claude-sonnet-5suffix",
    ),
)
@respx.mock
def test_anthropic_direct_adaptive_models_omit_temperature(model: str) -> None:
    def reject_explicit_temperature(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if "temperature" in payload:
            return httpx.Response(
                400,
                request=request,
                json={"error": {"message": "temperature is unsupported"}},
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "message-no-temperature",
                "model": model,
                "content": [{"type": "text", "text": '{"answer":"iron"}'}],
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
        )

    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        side_effect=reject_explicit_temperature
    )

    AnthropicProvider().generate_text(
        "Return JSON.",
        "Question",
        api_key="secret",
        model=model,
        output_schema={"type": "object"},
        options=GenerationOptions(
            cacheable_source_prefix="SUM: iron.",
            thinking=ThinkingMode.DISABLED,
            temperature=0,
            max_tokens=7000,
        ),
    )

    payload = json.loads(route.calls.last.request.content)
    assert "temperature" not in payload
    audit = s2_generation_parameters(ProviderName.ANTHROPIC, model)
    assert resolve_anthropic_model_capabilities(model).temperature is False
    assert audit["temperature"] == {
        "requested": 0,
        "transmission": "not_transmitted",
        "provider_default": "unknown_provider_default",
    }
    assert payload["max_tokens"] == 7000
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["messages"][0]["content"][0]["cache_control"] == {
        "type": "ephemeral"
    }


@respx.mock
def test_anthropic_compatible_model_keeps_temperature_and_minimal_connection_test() -> None:
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "message-compatible",
                "model": "claude-3-7-sonnet-latest",
                "content": [{"type": "text", "text": "OK"}],
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
        )
    )
    provider = AnthropicProvider()

    provider.generate_text(
        "Return JSON.",
        "Question",
        api_key="secret",
        model="claude-3-7-sonnet-latest",
        output_schema={"type": "object"},
        options=GenerationOptions(temperature=0, max_tokens=7000),
    )
    payload = json.loads(route.calls.last.request.content)
    assert payload["temperature"] == 0
    assert resolve_anthropic_model_capabilities("claude-3-7-sonnet-latest").temperature
    assert s2_generation_parameters(
        ProviderName.ANTHROPIC, "claude-3-7-sonnet-latest"
    )["temperature"] == {"value": 0, "transmission": "transmitted"}

    provider.test_connection("secret", "claude-sonnet-5")
    connection_payload = json.loads(route.calls.last.request.content)
    assert set(connection_payload) == {"model", "max_tokens", "system", "messages"}
    assert "temperature" not in connection_payload


@respx.mock
def test_anthropic_sonnet_5_explicit_disabled_thinking_is_encoded() -> None:
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "message-disabled",
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": '{"answer":"iron"}'}],
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
        )
    )

    AnthropicProvider().generate_text(
        "Return JSON.",
        "Question",
        api_key="secret",
        model="claude-sonnet-5",
        output_schema={"type": "object"},
        options=GenerationOptions(thinking=ThinkingMode.DISABLED),
    )

    assert json.loads(route.calls.last.request.content)["thinking"] == {
        "type": "disabled"
    }


@respx.mock
def test_anthropic_connection_test_omits_sonnet_5_thinking_control() -> None:
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "message-connection",
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": "OK"}],
                "usage": {"input_tokens": 5, "output_tokens": 1},
            },
        )
    )

    AnthropicProvider().test_connection("secret", "claude-sonnet-5")

    assert "thinking" not in json.loads(route.calls.last.request.content)


@respx.mock
def test_anthropic_sonnet_5_uses_adaptive_thinking_and_cache_aware_totals() -> None:
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "message-cache",
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": '{"answer":"iron"}'}],
                "usage": {
                    "input_tokens": 16,
                    "output_tokens": 4,
                    "cache_creation_input_tokens": 12,
                    "cache_read_input_tokens": 8,
                },
            },
        )
    )

    provider = AnthropicProvider(
        input_usd_per_million=3.0,
        output_usd_per_million=15.0,
    )
    result = provider.generate_text(
        "Return JSON.",
        "Question",
        api_key="secret",
        model="claude-sonnet-5",
        output_schema={"type": "object"},
        options=GenerationOptions(
            cacheable_source_prefix="SUM: iron is essential.",
            thinking=ThinkingMode.ENABLED,
            thinking_budget_tokens=2048,
        ),
    )

    payload = json.loads(route.calls.last.request.content)
    assert AnthropicProvider.capabilities.prompt_prefix_caching is True
    assert AnthropicProvider.capabilities.thinking is False
    assert provider.capabilities_for_model("claude-sonnet-5").thinking is True
    assert (
        provider.capabilities_for_model("claude-sonnet-5").thinking_capability
        is ThinkingCapability.ADAPTIVE
    )
    assert payload["messages"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "SUM: iron is essential.",
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": "Question"},
            ],
        }
    ]
    assert payload["thinking"] == {"type": "adaptive"}
    assert result.input_tokens == 36
    assert result.cache_creation_input_tokens == 12
    assert result.cache_read_input_tokens == 8
    assert result.cost_microusd == 155


@respx.mock
def test_anthropic_compatible_model_uses_manual_thinking_budget() -> None:
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "message-manual-thinking",
                "model": "claude-3-7-sonnet-latest",
                "content": [{"type": "text", "text": '{"answer":"iron"}'}],
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
        )
    )
    provider = AnthropicProvider()

    provider.generate_text(
        "Return JSON.",
        "Question",
        api_key="secret",
        model="claude-3-7-sonnet-latest",
        output_schema={"type": "object"},
        options=GenerationOptions(
            thinking=ThinkingMode.ENABLED,
            thinking_budget_tokens=2048,
        ),
    )

    payload = json.loads(route.calls.last.request.content)
    assert (
        provider.capabilities_for_model("claude-3-7-sonnet-latest").thinking_capability
        is ThinkingCapability.MANUAL
    )
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 2048}


@respx.mock
def test_anthropic_manual_thinking_model_omits_default_disabled_control() -> None:
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "message-manual-default",
                "model": "claude-3-7-sonnet-latest",
                "content": [{"type": "text", "text": '{"answer":"iron"}'}],
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
        )
    )

    AnthropicProvider().generate_text(
        "Return JSON.",
        "Question",
        api_key="secret",
        model="claude-3-7-sonnet-latest",
        output_schema={"type": "object"},
    )

    assert "thinking" not in json.loads(route.calls.last.request.content)


@pytest.mark.parametrize("model", ("claude-opus-4-7", "claude-opus-4-8"))
@respx.mock
def test_anthropic_adaptive_only_opus_release_uses_adaptive_thinking(
    model: str,
) -> None:
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "message-opus-adaptive",
                "model": model,
                "content": [{"type": "text", "text": '{"answer":"iron"}'}],
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
        )
    )

    AnthropicProvider().generate_text(
        "Return JSON.",
        "Question",
        api_key="secret",
        model=model,
        output_schema={"type": "object"},
        options=GenerationOptions(thinking=ThinkingMode.ENABLED),
    )

    assert json.loads(route.calls.last.request.content)["thinking"] == {
        "type": "adaptive"
    }


def test_anthropic_rejects_unknown_model_thinking_before_network() -> None:
    with pytest.raises(LLMRequestError) as raised:
        AnthropicProvider().generate_text(
            "Return JSON.",
            "Question",
            api_key="secret",
            model="claude-unknown-9",
            output_schema={"type": "object"},
            options=GenerationOptions(thinking=ThinkingMode.ENABLED),
        )

    assert raised.value.source is DiagnosticSource.CONTRACT


def test_anthropic_rejects_manual_budget_at_request_limit_before_network() -> None:
    with pytest.raises(LLMRequestError) as raised:
        AnthropicProvider().generate_text(
            "Return JSON.",
            "Question",
            api_key="secret",
            model="claude-3-7-sonnet-latest",
            output_schema={"type": "object"},
            options=GenerationOptions(
                thinking=ThinkingMode.ENABLED,
                thinking_budget_tokens=32768,
            ),
        )

    assert raised.value.source is DiagnosticSource.CONTRACT


@pytest.mark.parametrize(
    ("usage", "expected_input_tokens", "expected_cost"),
    [
        (
            {
                "input_tokens": 20,
                "output_tokens": 3,
                "cache_creation_input_tokens": 100,
            },
            120,
            320,
        ),
        (
            {
                "input_tokens": 20,
                "output_tokens": 3,
                "cache_read_input_tokens": 100,
            },
            120,
            90,
        ),
    ],
)
def test_anthropic_cache_usage_contributes_to_total_input_and_cost(
    usage: dict[str, int],
    expected_input_tokens: int,
    expected_cost: int,
) -> None:
    response = httpx.Response(
        200,
        json={
            "id": "message-cache-usage",
            "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": "answer"}],
            "usage": usage,
        },
    )
    provider = AnthropicProvider(
        input_usd_per_million=2.0,
        output_usd_per_million=10.0,
    )

    result = provider._generated_text(response, "claude-sonnet-5")

    assert result.input_tokens == expected_input_tokens
    assert result.cost_microusd == expected_cost


@respx.mock
def test_anthropic_structured_generation_sends_provider_safe_schema_copy():
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            headers={"request-id": "anthropic-schema"},
            json={
                "id": "message-schema",
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": '{"values":["a","b"]}'}],
                "usage": {"input_tokens": 12, "output_tokens": 5},
            },
        )
    )
    schema = {
        "type": "object",
        "properties": {
            "values": {
                "type": "array",
                "minItems": 2,
                "maxItems": 2,
                "prefixItems": [
                    {"type": "string", "minLength": 1, "maxLength": 20},
                    {"type": "string", "minLength": 1, "maxLength": 20},
                ],
            }
        },
        "required": ["values"],
        "additionalProperties": False,
    }
    original = copy.deepcopy(schema)

    AnthropicProvider().generate_text(
        "Return two values.",
        "Question",
        api_key="secret",
        model="claude-sonnet-5",
        output_schema=schema,
    )

    payload = json.loads(route.calls.last.request.content)
    sent = payload["output_config"]["format"]["schema"]
    assert sent == {
        "type": "object",
        "properties": {
            "values": {
                "type": "array",
                "items": {"type": "string"},
            }
        },
        "required": ["values"],
        "additionalProperties": False,
    }
    assert schema == original


@respx.mock
def test_anthropic_list_models_returns_sorted_ids():
    route = respx.get("https://api.anthropic.com/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "claude-sonnet-5"},
                    {"id": "claude-fable-5"},
                ]
            },
        )
    )

    models = AnthropicProvider().list_models("sentinel-secret")

    assert models == ("claude-fable-5", "claude-sonnet-5")
    request = route.calls.last.request
    assert request.headers["x-api-key"] == "sentinel-secret"
    assert request.headers["anthropic-version"] == "2023-06-01"


@respx.mock
def test_anthropic_list_models_raises_on_unauthorized_without_leaking_key():
    respx.get("https://api.anthropic.com/v1/models").mock(
        return_value=httpx.Response(
            401,
            json={"error": {"message": "invalid x-api-key sentinel-secret"}},
        )
    )

    with pytest.raises(LLMRequestError) as raised:
        AnthropicProvider().list_models("sentinel-secret")

    assert raised.value.source is DiagnosticSource.AUTHENTICATION
    assert "sentinel-secret" not in str(raised.value)


@respx.mock
def test_anthropic_list_models_raises_on_network_error_without_leaking_key():
    respx.get("https://api.anthropic.com/v1/models").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )

    with pytest.raises(LLMRequestError) as raised:
        AnthropicProvider().list_models("sentinel-secret")

    assert raised.value.source is DiagnosticSource.NETWORK
    assert "sentinel-secret" not in str(raised.value)
