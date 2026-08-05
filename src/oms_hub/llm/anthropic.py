import httpx

from oms_hub.llm.domain import (
    DEFAULT_GENERATION_OPTIONS,
    CleanResult,
    DiagnosticSource,
    GeneratedText,
    GenerationOptions,
    LLMRequestError,
    ProviderCapabilities,
    ProviderConnection,
    ProviderName,
    ThinkingCapability,
    ThinkingMode,
)
from oms_hub.llm.openai import openai_style_model_ids
from oms_hub.llm.provider import (
    FIXED_TRANSCRIPT_CONSTRAINTS,
    get_provider_json,
    invalid_response,
    optional_token_count,
    post_provider_json,
    response_object,
    safe_request_id,
    token_count,
    transcript_input,
)
from oms_hub.transcripts.prompt import ApprovedPrompt

_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "exclusiveMaximum",
        "exclusiveMinimum",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "multipleOf",
        "uniqueItems",
    }
)

_ADAPTIVE_THINKING_MODEL_PREFIXES = ("claude-sonnet-5",)
_MANUAL_THINKING_MODEL_PREFIXES = (
    "claude-3-7-sonnet",
    "claude-haiku-4",
    "claude-opus-4",
    "claude-sonnet-4",
)
_CACHE_CREATION_INPUT_MULTIPLIER = 1.25
_CACHE_READ_INPUT_MULTIPLIER = 0.1


def anthropic_output_schema(
    schema: dict[str, object],
) -> dict[str, object]:
    normalized = _normalize_schema_value(schema)
    if not isinstance(normalized, dict):
        raise TypeError("Anthropic output schema must be an object")
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
        if key not in _UNSUPPORTED_SCHEMA_KEYS and key != "prefixItems"
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
            else {}
        )
    return normalized


class AnthropicProvider:
    name = ProviderName.ANTHROPIC
    capabilities = ProviderCapabilities(prompt_prefix_caching=True)
    url = "https://api.anthropic.com/v1/messages"
    models_url = "https://api.anthropic.com/v1/models"

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
            model,
            FIXED_TRANSCRIPT_CONSTRAINTS,
            transcript_input(raw_text, prompt),
            max_tokens=32768,
            output_schema=None,
        )
        return self._clean_result(response, model)

    def test_connection(
        self,
        api_key: str,
        model: str,
    ) -> ProviderConnection:
        response = self._request(
            api_key,
            model,
            "Return only the requested text.",
            "Reply with exactly OK.",
            max_tokens=16,
            output_schema=None,
        )
        result = self._clean_result(response, model)
        return ProviderConnection(self.name, result.model, result.request_id)

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
        response = self._request(
            api_key,
            model,
            instruction,
            input_text,
            max_tokens=32768,
            output_schema=output_schema,
            options=options,
        )
        return self._generated_text(response, model)

    def list_models(self, api_key: str) -> tuple[str, ...]:
        response = get_provider_json(
            self.http,
            self.models_url,
            provider=self.name,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        payload = response_object(response, self.name)
        return openai_style_model_ids(payload, self.name, response)

    def capabilities_for_model(self, model: str) -> ProviderCapabilities:
        thinking_capability = _thinking_capability(model)
        return ProviderCapabilities(
            prompt_prefix_caching=True,
            thinking=thinking_capability is not ThinkingCapability.UNSUPPORTED,
            thinking_capability=thinking_capability,
        )

    def _request(
        self,
        api_key: str,
        model: str,
        system: str,
        content: str,
        *,
        max_tokens: int,
        output_schema: dict[str, object] | None,
        options: GenerationOptions = DEFAULT_GENERATION_OPTIONS,
    ) -> httpx.Response:
        thinking = self._thinking_request(model, max_tokens, options)
        message_content: str | list[dict[str, object]] = content
        if options.cacheable_source_prefix is not None:
            message_content = [
                {
                    "type": "text",
                    "text": options.cacheable_source_prefix,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": content},
            ]
        payload: dict[str, object] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [
                {
                    "role": "user",
                    "content": message_content,
                }
            ],
        }
        if thinking is not None:
            payload["thinking"] = thinking
        if output_schema is not None:
            payload["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": anthropic_output_schema(output_schema),
                }
            }
        return post_provider_json(
            self.http,
            self.url,
            provider=self.name,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            payload=payload,
        )

    def _thinking_request(
        self,
        model: str,
        max_tokens: int,
        options: GenerationOptions,
    ) -> dict[str, object] | None:
        if options.thinking is ThinkingMode.DISABLED:
            return None
        capability = self.capabilities_for_model(model).thinking_capability
        if capability is ThinkingCapability.ADAPTIVE:
            return {"type": "adaptive"}
        if capability is ThinkingCapability.MANUAL:
            if options.thinking_budget_tokens >= max_tokens:
                raise LLMRequestError(
                    "Anthropic thinking budget must be less than max_tokens",
                    source=DiagnosticSource.CONTRACT,
                )
            return {
                "type": "enabled",
                "budget_tokens": options.thinking_budget_tokens,
            }
        raise LLMRequestError(
            "Anthropic thinking mode is not supported by the selected model",
            source=DiagnosticSource.CONTRACT,
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
        content = payload.get("content")
        usage = payload.get("usage")
        if not isinstance(content, list) or not isinstance(usage, dict):
            raise invalid_response(self.name, response)
        text_parts = [
            part["text"]
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ]
        cleaned = "".join(text_parts).strip()
        if not cleaned:
            raise invalid_response(self.name, response)
        raw_input_tokens = token_count(
            usage.get("input_tokens"),
            self.name,
            response,
        )
        output_tokens = token_count(
            usage.get("output_tokens"),
            self.name,
            response,
        )
        cache_creation_input_tokens = optional_token_count(
            usage.get("cache_creation_input_tokens"), self.name, response
        )
        cache_read_input_tokens = optional_token_count(
            usage.get("cache_read_input_tokens"), self.name, response
        )
        input_tokens = (
            raw_input_tokens
            + cache_creation_input_tokens
            + cache_read_input_tokens
        )
        returned_model = payload.get("model", requested_model)
        if not isinstance(returned_model, str) or not returned_model:
            raise invalid_response(self.name, response)
        request_id = safe_request_id(response)
        if request_id is None:
            message_id = payload.get("id")
            request_id = message_id if isinstance(message_id, str) else ""
        return GeneratedText(
            text=cleaned,
            provider=self.name,
            model=returned_model,
            request_id=request_id[:200],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microusd=_anthropic_estimated_cost(
                raw_input_tokens,
                cache_creation_input_tokens,
                cache_read_input_tokens,
                output_tokens,
                self.input_usd_per_million,
                self.output_usd_per_million,
            ),
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
        )


def _thinking_capability(model: str) -> ThinkingCapability:
    normalized = model.casefold()
    if normalized.startswith(_ADAPTIVE_THINKING_MODEL_PREFIXES):
        return ThinkingCapability.ADAPTIVE
    if normalized.startswith(_MANUAL_THINKING_MODEL_PREFIXES):
        return ThinkingCapability.MANUAL
    return ThinkingCapability.UNSUPPORTED


def _anthropic_estimated_cost(
    raw_input_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
    output_tokens: int,
    input_usd_per_million: float,
    output_usd_per_million: float,
) -> int:
    """Return micro-USD without double-counting cache token categories."""
    return round(
        raw_input_tokens * input_usd_per_million
        + cache_creation_input_tokens
        * input_usd_per_million
        * _CACHE_CREATION_INPUT_MULTIPLIER
        + cache_read_input_tokens * input_usd_per_million * _CACHE_READ_INPUT_MULTIPLIER
        + output_tokens * output_usd_per_million
    )
