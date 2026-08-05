import copy
import json

import httpx
import pytest
import respx

from oms_hub.llm.anthropic import AnthropicProvider
from oms_hub.llm.domain import (
    DiagnosticSource,
    GenerationOptions,
    LLMRequestError,
    ProviderName,
    ThinkingMode,
)
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
    )

    payload = route.calls.last.request.content.decode()
    assert '"output_config"' in payload
    assert '"json_schema"' in payload
    assert result.text == '{"answer":"iron"}'


@respx.mock
def test_anthropic_generation_caches_prefix_and_enables_thinking() -> None:
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

    result = AnthropicProvider().generate_text(
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
    assert AnthropicProvider.capabilities.thinking is True
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
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert result.cache_creation_input_tokens == 12
    assert result.cache_read_input_tokens == 8


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
