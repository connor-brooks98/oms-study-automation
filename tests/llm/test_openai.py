import copy
import json

import httpx
import pytest
import respx

from oms_hub.llm.domain import DiagnosticSource, LLMRequestError, ProviderName
from oms_hub.llm.openai import OpenAIProvider
from oms_hub.transcripts.prompt import ApprovedPrompt


@respx.mock
def test_openai_provider_sends_responses_request_and_parses_usage():
    route = respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp-123",
                "status": "completed",
                "model": "gpt-5.2",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "Cleaned lecture."}
                        ],
                    }
                ],
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        )
    )
    provider = OpenAIProvider(
        input_usd_per_million=2.0,
        output_usd_per_million=10.0,
    )

    result = provider.clean(
        "Raw lecture.",
        ApprovedPrompt("Remove filler.", "a" * 64),
        api_key="secret",
        model="gpt-5.2",
    )

    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer secret"
    assert result.provider is ProviderName.OPENAI
    assert result.text == "Cleaned lecture."
    assert result.model == "gpt-5.2"
    assert result.request_id == "resp-123"
    assert result.input_tokens == 100
    assert result.output_tokens == 20
    assert result.cost_microusd == 400


@respx.mock
def test_openai_connection_test_uses_a_real_minimal_generation():
    route = respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp-test",
                "status": "completed",
                "model": "gpt-5.2",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "OK"}],
                    }
                ],
                "usage": {"input_tokens": 5, "output_tokens": 1},
            },
        )
    )

    result = OpenAIProvider().test_connection("secret", "gpt-5.2")

    assert result.request_id == "resp-test"
    assert route.calls.call_count == 1


@respx.mock
def test_openai_structured_generation_sends_json_schema():
    route = respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp-json",
                "status": "completed",
                "model": "gpt-5.2",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"answer":"iron"}',
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
        )
    )

    result = OpenAIProvider().generate_text(
        "Return a grounded answer.",
        "Question",
        api_key="secret",
        model="gpt-5.2",
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
def test_openai_structured_generation_sends_provider_safe_schema_copy():
    route = respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp-schema",
                "status": "completed",
                "model": "gpt-5.6-terra",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"values":["a","b"]}',
                            }
                        ],
                    }
                ],
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
                    {"type": "string", "minLength": 1},
                    {"type": "string", "minLength": 1},
                ],
            }
        },
        "required": ["values"],
        "additionalProperties": False,
    }
    original = copy.deepcopy(schema)

    OpenAIProvider().generate_text(
        "Return two values.",
        "Question",
        api_key="secret",
        model="gpt-5.6-terra",
        output_schema=schema,
    )

    payload = json.loads(route.calls.last.request.content)
    sent = payload["text"]["format"]["schema"]
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
def test_openai_list_models_returns_sorted_ids():
    route = respx.get("https://api.openai.com/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-4.1"},
                    {"id": "gpt-5.2"},
                    {"id": "gpt-5.2-mini"},
                ]
            },
        )
    )

    models = OpenAIProvider().list_models("sentinel-secret")

    assert models == ("gpt-4.1", "gpt-5.2", "gpt-5.2-mini")
    assert route.calls.last.request.headers["authorization"] == (
        "Bearer sentinel-secret"
    )


@respx.mock
def test_openai_list_models_raises_on_unauthorized_without_leaking_key():
    respx.get("https://api.openai.com/v1/models").mock(
        return_value=httpx.Response(
            401,
            json={"error": {"message": "invalid api key: sentinel-secret"}},
        )
    )

    with pytest.raises(LLMRequestError) as raised:
        OpenAIProvider().list_models("sentinel-secret")

    assert raised.value.source is DiagnosticSource.AUTHENTICATION
    assert "sentinel-secret" not in str(raised.value)


@respx.mock
def test_openai_list_models_raises_on_network_error_without_leaking_key():
    respx.get("https://api.openai.com/v1/models").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )

    with pytest.raises(LLMRequestError) as raised:
        OpenAIProvider().list_models("sentinel-secret")

    assert raised.value.source is DiagnosticSource.NETWORK
    assert "sentinel-secret" not in str(raised.value)


@respx.mock
def test_openai_list_models_raises_on_malformed_payload():
    respx.get("https://api.openai.com/v1/models").mock(
        return_value=httpx.Response(200, json={"data": "not-a-list"})
    )

    with pytest.raises(LLMRequestError) as raised:
        OpenAIProvider().list_models("sentinel-secret")

    assert raised.value.source is DiagnosticSource.SERVICE
    assert "sentinel-secret" not in str(raised.value)
