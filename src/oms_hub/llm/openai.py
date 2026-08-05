from typing import Any

import httpx

from oms_hub.llm.domain import (
    DEFAULT_GENERATION_OPTIONS,
    CleanResult,
    GeneratedText,
    GenerationOptions,
    ProviderCapabilities,
    ProviderConnection,
    ProviderName,
)
from oms_hub.llm.provider import (
    FIXED_TRANSCRIPT_CONSTRAINTS,
    estimated_cost,
    get_provider_json,
    invalid_response,
    post_provider_json,
    prompt_with_cacheable_prefix,
    require_supported_generation_options,
    response_object,
    token_count,
    transcript_input,
)
from oms_hub.transcripts.prompt import ApprovedPrompt


def openai_output_schema(
    schema: dict[str, object],
) -> dict[str, object]:
    normalized = _normalize_schema_value(schema)
    if not isinstance(normalized, dict):
        raise TypeError("OpenAI output schema must be an object")
    return normalized


def _normalize_schema_value(value: object) -> object:
    if isinstance(value, list):
        return [_normalize_schema_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    prefix_items = value.get("prefixItems")
    normalized = {
        str(key): _normalize_schema_value(item)
        for key, item in value.items()
        if key != "prefixItems"
    }
    if (
        "items" not in normalized
        and isinstance(prefix_items, list)
        and prefix_items
    ):
        candidates = [
            _normalize_schema_value(item) for item in prefix_items
        ]
        normalized["items"] = (
            candidates[0]
            if all(item == candidates[0] for item in candidates[1:])
            else {"anyOf": candidates}
        )
    return normalized


def openai_style_model_ids(
    payload: dict[str, Any],
    provider: ProviderName,
    response: httpx.Response,
) -> tuple[str, ...]:
    """Extract sorted model ids from an OpenAI-format ``{"data": [...]}`` list.

    Shared by any provider whose model-listing endpoint follows the OpenAI
    convention of a top-level ``data`` array of ``{"id": ...}`` objects
    (OpenAI itself, Anthropic, and OpenRouter).
    """
    data = payload.get("data")
    if not isinstance(data, list):
        raise invalid_response(provider, response)
    ids = [
        item["id"]
        for item in data
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and item["id"]
    ]
    return tuple(sorted(ids))


class OpenAIProvider:
    name = ProviderName.OPENAI
    capabilities = ProviderCapabilities()
    url = "https://api.openai.com/v1/responses"
    models_url = "https://api.openai.com/v1/models"

    def __init__(
        self,
        *,
        input_usd_per_million: float = 0,
        output_usd_per_million: float = 0,
        http: httpx.Client | None = None,
    ) -> None:
        self.input_usd_per_million = input_usd_per_million
        self.output_usd_per_million = output_usd_per_million
        self.http = http or httpx.Client(timeout=300.0)

    def clean(
        self,
        raw_text: str,
        prompt: ApprovedPrompt,
        *,
        api_key: str,
        model: str,
    ) -> CleanResult:
        response = self._request(
            api_key,
            {
                "model": model,
                "store": False,
                "instructions": FIXED_TRANSCRIPT_CONSTRAINTS,
                "input": transcript_input(raw_text, prompt),
            },
        )
        return self._clean_result(response, model)

    def generate_text(
        self,
        instruction: str,
        input_text: str,
        *,
        api_key: str,
        model: str,
        output_schema: dict[str, object],
        options: GenerationOptions = DEFAULT_GENERATION_OPTIONS,
    ) -> GeneratedText:
        require_supported_generation_options(self.name, self.capabilities, options)
        response = self._request(
            api_key,
            {
                "model": model,
                "store": False,
                "instructions": instruction,
                "input": prompt_with_cacheable_prefix(input_text, options),
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "structured_output",
                        "schema": openai_output_schema(output_schema),
                        "strict": True,
                    }
                },
            },
        )
        return self._generated_text(response, model)

    def test_connection(
        self,
        api_key: str,
        model: str,
    ) -> ProviderConnection:
        response = self._request(
            api_key,
            {
                "model": model,
                "store": False,
                "max_output_tokens": 16,
                "input": "Reply with exactly OK.",
            },
        )
        result = self._clean_result(response, model)
        return ProviderConnection(self.name, result.model, result.request_id)

    def list_models(self, api_key: str) -> tuple[str, ...]:
        response = get_provider_json(
            self.http,
            self.models_url,
            provider=self.name,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        payload = response_object(response, self.name)
        return openai_style_model_ids(payload, self.name, response)

    def _request(
        self,
        api_key: str,
        payload: dict[str, Any],
    ) -> httpx.Response:
        return post_provider_json(
            self.http,
            self.url,
            provider=self.name,
            headers={"Authorization": f"Bearer {api_key}"},
            payload=payload,
        )

    def _clean_result(
        self,
        response: httpx.Response,
        requested_model: str,
    ) -> CleanResult:
        generated = self._generated_text(response, requested_model)
        return CleanResult(
            text=generated.text,
            provider=generated.provider,
            model=generated.model,
            request_id=generated.request_id,
            input_tokens=generated.input_tokens,
            output_tokens=generated.output_tokens,
            cost_microusd=generated.cost_microusd,
        )

    def _generated_text(
        self,
        response: httpx.Response,
        requested_model: str,
    ) -> GeneratedText:
        payload = response_object(response, self.name)
        if payload.get("status") != "completed":
            raise invalid_response(self.name, response)
        output = payload.get("output")
        if not isinstance(output, list):
            raise invalid_response(self.name, response)
        text_parts: list[str] = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    value = part.get("text")
                    if isinstance(value, str):
                        text_parts.append(value)
        cleaned = "".join(text_parts).strip()
        usage = payload.get("usage")
        if not cleaned or not isinstance(usage, dict):
            raise invalid_response(self.name, response)
        input_tokens = token_count(
            usage.get("input_tokens"),
            self.name,
            response,
        )
        output_tokens = token_count(
            usage.get("output_tokens"),
            self.name,
            response,
        )
        request_id = payload.get("id")
        if not isinstance(request_id, str) or not request_id:
            raise invalid_response(self.name, response)
        returned_model = payload.get("model", requested_model)
        if not isinstance(returned_model, str) or not returned_model:
            raise invalid_response(self.name, response)
        return GeneratedText(
            text=cleaned,
            provider=self.name,
            model=returned_model,
            request_id=request_id[:200],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microusd=estimated_cost(
                input_tokens,
                output_tokens,
                self.input_usd_per_million,
                self.output_usd_per_million,
            ),
        )
