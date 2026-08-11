import copy
import json

import httpx
import pytest
import respx

from oms_hub.llm.domain import (
    DiagnosticSource,
    GenerationOptions,
    LLMRequestError,
    ProviderName,
    ThinkingMode,
)
from oms_hub.llm.openrouter import OpenRouterProvider
from oms_hub.transcripts.prompt import ApprovedPrompt


@respx.mock
def test_openrouter_provider_sends_chat_completions_request_and_parses_usage():
    route = respx.post(
        "https://openrouter.ai/api/v1/chat/completions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "gen-123",
                "model": "openai/gpt-4o-mini",
                "choices": [
                    {"message": {"role": "assistant", "content": "Cleaned lecture."}}
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )
    )
    provider = OpenRouterProvider(
        input_usd_per_million=2.0,
        output_usd_per_million=10.0,
    )

    result = provider.clean(
        "Raw lecture.",
        ApprovedPrompt("Remove filler.", "a" * 64),
        api_key="secret",
        model="openai/gpt-4o-mini",
    )

    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer secret"
    assert result.provider is ProviderName.OPENROUTER
    assert result.text == "Cleaned lecture."
    assert result.model == "openai/gpt-4o-mini"
    assert result.request_id == "gen-123"
    assert result.input_tokens == 100
    assert result.output_tokens == 20
    assert result.cost_microusd == 400


@respx.mock
def test_openrouter_connection_test_uses_a_real_minimal_generation():
    route = respx.post(
        "https://openrouter.ai/api/v1/chat/completions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "gen-test",
                "model": "openai/gpt-4o-mini",
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            },
        )
    )

    result = OpenRouterProvider().test_connection("secret", "openai/gpt-4o-mini")

    assert result.request_id == "gen-test"
    assert route.calls.call_count == 1
    payload = json.loads(route.calls.last.request.content)
    assert payload["max_tokens"] == 16


@respx.mock
def test_openrouter_structured_generation_sends_json_schema():
    route = respx.post(
        "https://openrouter.ai/api/v1/chat/completions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "gen-json",
                "model": "openai/gpt-4o-mini",
                "choices": [{"message": {"content": '{"answer":"iron"}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            },
        )
    )

    result = OpenRouterProvider().generate_text(
        "Return a grounded answer.",
        "Question",
        api_key="secret",
        model="openai/gpt-4o-mini",
        output_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    )

    payload = route.calls.last.request.content.decode()
    assert '"json_schema"' in payload
    assert '"structured_output"' in payload
    assert result.text == '{"answer":"iron"}'


@respx.mock
def test_openrouter_structured_generation_uses_the_strict_schema_normalizer():
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "gen-json-strict",
                "model": "openai/gpt-4o-mini",
                "choices": [{"message": {"content": '{"answer":null}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            },
        )
    )

    OpenRouterProvider().generate_text(
        "Return a grounded answer.",
        "Question",
        api_key="secret",
        model="openai/gpt-4o-mini",
        output_schema={
            "type": "object",
            "properties": {"answer": {"type": "string", "default": ""}},
        },
    )

    schema = json.loads(route.calls.last.request.content)["response_format"]["json_schema"][
        "schema"
    ]
    assert schema["required"] == ["answer"]
    assert schema["additionalProperties"] is False
    assert "default" not in schema["properties"]["answer"]


@respx.mock
def test_openrouter_generation_preserves_prefix_order_without_cache_telemetry() -> None:
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "gen-cache",
                "model": "openai/gpt-4o-mini",
                "choices": [{"message": {"content": '{"ok":true}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            },
        )
    )

    result = OpenRouterProvider().generate_text(
        "Return JSON.",
        "Question",
        api_key="secret",
        model="openai/gpt-4o-mini",
        output_schema={"type": "object"},
        options=GenerationOptions(
            cacheable_source_prefix="SUM: source",
            temperature=0,
            max_tokens=7000,
        ),
    )

    payload = json.loads(route.calls.last.request.content)
    assert OpenRouterProvider.capabilities.prompt_prefix_caching is False
    assert OpenRouterProvider.capabilities.thinking is False
    assert payload["messages"] == [
        {"role": "system", "content": "Return JSON."},
        {"role": "user", "content": "SUM: source\n\nQuestion"},
    ]
    assert payload["temperature"] == 0
    assert payload["max_tokens"] == 7000
    assert "thinking" not in payload
    assert result.cache_creation_input_tokens == 0
    assert result.cache_read_input_tokens == 0


def test_openrouter_rejects_unsupported_thinking() -> None:
    with pytest.raises(LLMRequestError) as raised:
        OpenRouterProvider().generate_text(
            "Return JSON.",
            "Question",
            api_key="secret",
            model="openai/gpt-4o-mini",
            output_schema={"type": "object"},
            options=GenerationOptions(thinking=ThinkingMode.ENABLED),
        )

    assert raised.value.source is DiagnosticSource.CONTRACT


@respx.mock
def test_openrouter_structured_generation_sends_provider_safe_schema_copy():
    route = respx.post(
        "https://openrouter.ai/api/v1/chat/completions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "gen-schema",
                "model": "openai/gpt-4o-mini",
                "choices": [{"message": {"content": '{"values":["a","b"]}'}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5},
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
                    {"type": "string", "minLength": 1},
                    {"type": "string", "minLength": 1},
                ],
            }
        },
        "required": ["values"],
        "additionalProperties": False,
    }
    original = copy.deepcopy(schema)

    OpenRouterProvider().generate_text(
        "Return two values.",
        "Question",
        api_key="secret",
        model="openai/gpt-4o-mini",
        output_schema=schema,
    )

    payload = json.loads(route.calls.last.request.content)
    sent = payload["response_format"]["json_schema"]["schema"]
    assert sent == {
        "type": "object",
        "properties": {
            "values": {
                "type": "array",
                "minItems": 2,
                "maxItems": 2,
                "items": {"type": "string", "minLength": 1},
            }
        },
        "required": ["values"],
        "additionalProperties": False,
    }
    assert schema == original


@respx.mock
def test_openrouter_list_models_returns_sorted_ids():
    route = respx.get("https://openrouter.ai/api/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "openrouter/free"},
                    {"id": "anthropic/claude-3.5-sonnet"},
                    {"id": "openai/gpt-4o-mini"},
                ]
            },
        )
    )

    models = OpenRouterProvider().list_models("sentinel-secret")

    assert models == (
        "anthropic/claude-3.5-sonnet",
        "openai/gpt-4o-mini",
        "openrouter/free",
    )
    assert route.calls.last.request.headers["authorization"] == (
        "Bearer sentinel-secret"
    )


@respx.mock
def test_openrouter_list_models_raises_on_unauthorized_without_leaking_key():
    respx.get("https://openrouter.ai/api/v1/models").mock(
        return_value=httpx.Response(
            401,
            json={"error": {"message": "invalid api key: sentinel-secret"}},
        )
    )

    with pytest.raises(LLMRequestError) as raised:
        OpenRouterProvider().list_models("sentinel-secret")

    assert raised.value.source is DiagnosticSource.AUTHENTICATION
    assert "sentinel-secret" not in str(raised.value)


@respx.mock
def test_openrouter_list_models_raises_on_network_error_without_leaking_key():
    respx.get("https://openrouter.ai/api/v1/models").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )

    with pytest.raises(LLMRequestError) as raised:
        OpenRouterProvider().list_models("sentinel-secret")

    assert raised.value.source is DiagnosticSource.NETWORK
    assert "sentinel-secret" not in str(raised.value)


@respx.mock
def test_openrouter_list_models_raises_on_malformed_payload():
    respx.get("https://openrouter.ai/api/v1/models").mock(
        return_value=httpx.Response(200, json={"data": "not-a-list"})
    )

    with pytest.raises(LLMRequestError) as raised:
        OpenRouterProvider().list_models("sentinel-secret")

    assert raised.value.source is DiagnosticSource.SERVICE
    assert "sentinel-secret" not in str(raised.value)


@respx.mock
def test_openrouter_provider_http_errors_are_safely_classified_and_key_free():
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            401,
            headers={"x-request-id": "safe-request"},
            json={"error": {"message": "secret provider body sentinel-secret"}},
        )
    )

    with pytest.raises(LLMRequestError) as raised:
        OpenRouterProvider().clean(
            "Raw",
            ApprovedPrompt("Prompt", "a" * 64),
            api_key="sentinel-secret",
            model="openai/gpt-4o-mini",
        )

    assert raised.value.source is DiagnosticSource.AUTHENTICATION
    assert raised.value.http_status == 401
    assert raised.value.provider_request_id == "safe-request"
    assert "sentinel-secret" not in str(raised.value)
    assert "secret provider body" not in str(raised.value)


@respx.mock
def test_openrouter_network_timeout_is_distinguished_from_provider_failure():
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )

    with pytest.raises(LLMRequestError) as raised:
        OpenRouterProvider().clean(
            "Raw",
            ApprovedPrompt("Prompt", "a" * 64),
            api_key="sentinel-secret",
            model="openai/gpt-4o-mini",
        )

    assert raised.value.source is DiagnosticSource.NETWORK
    assert raised.value.http_status is None
    assert "sentinel-secret" not in str(raised.value)
