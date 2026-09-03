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
from oms_hub.llm.gemini import GeminiProvider
from oms_hub.study_generation.practice_contracts import ExtractionPayload
from oms_hub.transcripts.prompt import ApprovedPrompt


@respx.mock
def test_gemini_connection_test_uses_model_metadata_instead_of_generation():
    route = respx.get(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash"
    ).mock(
        return_value=httpx.Response(
            200,
            headers={"x-goog-request-id": "gemini-connection"},
            json={
                "name": "models/gemini-3.6-flash",
                "supportedGenerationMethods": ["generateContent"],
            },
        )
    )

    result = GeminiProvider().test_connection("secret", "gemini-3.6-flash")

    assert route.calls.last.request.url.params["key"] == "secret"
    assert result.model == "gemini-3.6-flash"
    assert result.request_id == "gemini-connection"


@respx.mock
def test_gemini_provider_sends_generate_content_request_and_parses_usage():
    route = respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-3.6-flash:generateContent"
    ).mock(
        return_value=httpx.Response(
            200,
            headers={"x-request-id": "gemini-request"},
            json={
                "modelVersion": "gemini-3.6-flash",
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": "Cleaned Gemini lecture."}]
                        }
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 90,
                    "candidatesTokenCount": 18,
                },
            },
        )
    )
    provider = GeminiProvider(
        input_usd_per_million=1.5,
        output_usd_per_million=7.5,
    )

    result = provider.clean(
        "Raw lecture.",
        ApprovedPrompt("Remove filler.", "a" * 64),
        api_key="secret",
        model="gemini-3.6-flash",
    )

    request = route.calls.last.request
    assert request.headers["x-goog-api-key"] == "secret"
    assert result.provider is ProviderName.GEMINI
    assert result.text == "Cleaned Gemini lecture."
    assert result.model == "gemini-3.6-flash"
    assert result.request_id == "gemini-request"
    assert result.input_tokens == 90
    assert result.output_tokens == 18
    assert result.cost_microusd == 270


@respx.mock
def test_gemini_structured_generation_sends_response_format() -> None:
    model = "gemini-any-compatible-model"
    route = respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    ).mock(
        return_value=httpx.Response(
            200,
            headers={"x-request-id": "gemini-json"},
            json={
                "modelVersion": model,
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": '{"answer":"iron"}'}]
                        }
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 4,
                },
            },
        )
    )
    schema: dict[str, object] = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    original_schema = json.loads(json.dumps(schema))

    result = GeminiProvider().generate_text(
        "Return a grounded answer.",
        "Question",
        api_key="secret",
        model=model,
        output_schema=schema,
    )

    payload = json.loads(route.calls.last.request.content)
    assert payload["generationConfig"]["responseFormat"] == {
        "text": {
            "mimeType": "APPLICATION_JSON",
            "schema": schema,
        }
    }
    assert schema == original_schema
    assert result.text == '{"answer":"iron"}'


@respx.mock
def test_gemini_sends_expanded_extraction_schema_without_mutating_source() -> None:
    model = "gemini-schema-model"
    route = respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "modelVersion": model,
                "candidates": [
                    {"content": {"parts": [{"text": '{"questions":[],"answers":[]}'}]}}
                ],
                "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 5},
            },
        )
    )
    schema = ExtractionPayload.model_json_schema()
    original = copy.deepcopy(schema)

    GeminiProvider().generate_text(
        "Return questions.",
        "Question",
        api_key="secret",
        model=model,
        output_schema=schema,
    )

    payload = json.loads(route.calls.last.request.content)
    assert payload["generationConfig"]["responseFormat"] == {
        "text": {"mimeType": "APPLICATION_JSON", "schema": original}
    }
    assert schema == original


@respx.mock
def test_gemini_generation_preserves_prefix_order_without_cache_telemetry() -> None:
    route = respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-3.6-flash:generateContent"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "modelVersion": "gemini-3.6-flash",
                "candidates": [{"content": {"parts": [{"text": '{"ok":true}'}]}}],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 4,
                    "cachedContentTokenCount": 9,
                },
            },
        )
    )

    result = GeminiProvider().generate_text(
        "Return JSON.",
        "Question",
        api_key="secret",
        model="gemini-3.6-flash",
        output_schema={"type": "object"},
        options=GenerationOptions(
            cacheable_source_prefix="SUM: source",
            temperature=0,
            max_tokens=7000,
        ),
    )

    payload = json.loads(route.calls.last.request.content)
    assert GeminiProvider.capabilities.prompt_prefix_caching is False
    assert GeminiProvider.capabilities.thinking is False
    assert payload["contents"][0]["parts"] == [
        {"text": "SUM: source"},
        {"text": "Question"},
    ]
    assert payload["generationConfig"]["temperature"] == 0
    assert payload["generationConfig"]["maxOutputTokens"] == 7000
    assert "thinking" not in payload["generationConfig"]
    assert result.cache_creation_input_tokens == 0
    assert result.cache_read_input_tokens == 0


def test_gemini_rejects_unsupported_thinking() -> None:
    with pytest.raises(LLMRequestError) as raised:
        GeminiProvider().generate_text(
            "Return JSON.",
            "Question",
            api_key="secret",
            model="gemini-3.6-flash",
            output_schema={"type": "object"},
            options=GenerationOptions(thinking=ThinkingMode.ENABLED),
        )

    assert raised.value.source is DiagnosticSource.CONTRACT


@respx.mock
def test_gemini_list_models_filters_to_generate_content_and_sorts():
    route = respx.get(
        "https://generativelanguage.googleapis.com/v1beta/models"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-3-flash",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/gemini-3-pro",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/text-embedding-004",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ]
            },
        )
    )

    models = GeminiProvider().list_models("sentinel-secret")

    assert models == ("gemini-3-flash", "gemini-3-pro")
    request = route.calls.last.request
    assert request.url.params["key"] == "sentinel-secret"


@respx.mock
def test_gemini_list_models_raises_on_unauthorized_without_leaking_key():
    respx.get(
        "https://generativelanguage.googleapis.com/v1beta/models"
    ).mock(
        return_value=httpx.Response(
            401,
            json={
                "error": {
                    "status": "UNAUTHENTICATED",
                    "message": "API key sentinel-secret is invalid",
                }
            },
        )
    )

    with pytest.raises(LLMRequestError) as raised:
        GeminiProvider().list_models("sentinel-secret")

    assert raised.value.source is DiagnosticSource.AUTHENTICATION
    assert "sentinel-secret" not in str(raised.value)


@respx.mock
def test_gemini_list_models_raises_on_network_error_without_leaking_key():
    respx.get(
        "https://generativelanguage.googleapis.com/v1beta/models"
    ).mock(side_effect=httpx.ReadTimeout("timed out"))

    with pytest.raises(LLMRequestError) as raised:
        GeminiProvider().list_models("sentinel-secret")

    assert raised.value.source is DiagnosticSource.NETWORK
    assert "sentinel-secret" not in str(raised.value)
