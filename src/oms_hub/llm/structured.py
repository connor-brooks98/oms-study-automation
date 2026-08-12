import json
import re
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ValidationError

from oms_hub.anki.provider_attempts import (
    ProviderCallHandle,
    begin_provider_call,
    emit_provider_event,
)
from oms_hub.llm.domain import (
    DEFAULT_GENERATION_OPTIONS,
    GeneratedText,
    GenerationOptions,
    LLMRequestError,
    ProviderName,
)

_JSON_FENCE = re.compile(
    r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$",
    re.IGNORECASE | re.DOTALL,
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class StructuredJSONResult[StructuredModel: BaseModel]:
    value: StructuredModel
    raw_text: str
    provider: ProviderName
    model: str
    request_id: str
    input_tokens: int
    output_tokens: int
    cost_microusd: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    attempt_handle: ProviderCallHandle | None = None


class StructuredOutputError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        raw_text: str,
        generation: GeneratedText,
        attempt_handle: ProviderCallHandle | None = None,
    ) -> None:
        super().__init__(message)
        self.raw_text = raw_text
        self.generation = generation
        self.attempt_handle = attempt_handle


class StructuredTextGenerator(Protocol):
    def generate_text(
        self,
        instruction: str,
        input_text: str,
        *,
        output_schema: dict[str, object],
        provider: ProviderName,
        model: str,
        options: GenerationOptions = DEFAULT_GENERATION_OPTIONS,
    ) -> GeneratedText: ...


class StructuredTextService:
    def __init__(self, generator: StructuredTextGenerator) -> None:
        self.generator = generator

    def generate_json[StructuredModel: BaseModel](
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[StructuredModel],
        provider: ProviderName,
        model: str,
        options: GenerationOptions = DEFAULT_GENERATION_OPTIONS,
    ) -> StructuredJSONResult[StructuredModel]:
        output_schema = output_model.model_json_schema()
        generation_parameters: dict[str, object] = {
            "thinking": options.thinking.value,
            "thinking_budget_tokens": options.thinking_budget_tokens,
            "temperature": options.temperature,
            "max_tokens": options.max_tokens,
        }
        attempt_handle = begin_provider_call(
            provider=provider.value,
            model=model,
            instruction=instruction,
            input_text=input_text,
            output_schema=output_schema,
            generation_parameters=generation_parameters,
            cacheable_source_prefix=options.cacheable_source_prefix,
        )
        arguments: dict[str, object] = {
            "output_schema": output_schema,
            "provider": provider,
            "model": model,
        }
        if options is not DEFAULT_GENERATION_OPTIONS:
            arguments["options"] = options
        emit_provider_event(attempt_handle, "dispatched")
        try:
            generated = self.generator.generate_text(
                instruction,
                input_text,
                **arguments,  # type: ignore[arg-type]
            )
        except Exception as exc:
            request_error = exc if isinstance(exc, LLMRequestError) else None
            emit_provider_event(
                attempt_handle,
                "transport_failed",
                request_id=(request_error.provider_request_id if request_error else None),
                error=str(exc),
                diagnostic_source=(request_error.source.value if request_error else None),
                http_status=(request_error.http_status if request_error else None),
            )
            raise
        raw_text = sanitize_model_text(generated.text)
        emit_provider_event(
            attempt_handle,
            "response_received",
            response_text=raw_text,
            request_id=generated.request_id,
            input_tokens=generated.input_tokens,
            output_tokens=generated.output_tokens,
            cost_microusd=generated.cost_microusd,
            cache_creation_input_tokens=generated.cache_creation_input_tokens,
            cache_read_input_tokens=generated.cache_read_input_tokens,
        )
        match = _JSON_FENCE.fullmatch(raw_text)
        json_text = match.group("body") if match is not None else raw_text
        try:
            payload = json.loads(json_text)
        except json.JSONDecodeError as exc:
            message = (
                "structured output failed JSON schema validation: "
                f"$: invalid JSON at line {exc.lineno}, column {exc.colno}"
            )
            emit_provider_event(attempt_handle, "validation_failed", error=message)
            raise StructuredOutputError(
                message,
                raw_text=raw_text,
                generation=generated,
                attempt_handle=attempt_handle,
            ) from exc
        try:
            value = output_model.model_validate(payload)
        except ValidationError as exc:
            message = (
                "structured output failed JSON schema validation: "
                + _validation_details(exc)
            )
            emit_provider_event(attempt_handle, "validation_failed", error=message)
            raise StructuredOutputError(
                message,
                raw_text=raw_text,
                generation=generated,
                attempt_handle=attempt_handle,
            ) from exc
        except TypeError as exc:
            message = (
                "structured output failed JSON schema validation: "
                "$: invalid structured value"
            )
            emit_provider_event(attempt_handle, "validation_failed", error=message)
            raise StructuredOutputError(
                message,
                raw_text=raw_text,
                generation=generated,
                attempt_handle=attempt_handle,
            ) from exc
        emit_provider_event(
            attempt_handle,
            "accepted",
            request_id=generated.request_id,
            input_tokens=generated.input_tokens,
            output_tokens=generated.output_tokens,
            cost_microusd=generated.cost_microusd,
            cache_creation_input_tokens=generated.cache_creation_input_tokens,
            cache_read_input_tokens=generated.cache_read_input_tokens,
        )
        return StructuredJSONResult(
            value=value,
            raw_text=raw_text,
            provider=generated.provider,
            model=generated.model,
            request_id=generated.request_id,
            input_tokens=generated.input_tokens,
            output_tokens=generated.output_tokens,
            cost_microusd=generated.cost_microusd,
            cache_creation_input_tokens=generated.cache_creation_input_tokens,
            cache_read_input_tokens=generated.cache_read_input_tokens,
            attempt_handle=attempt_handle,
        )


def sanitize_model_text(value: str, *, max_characters: int = 200_000) -> str:
    sanitized = _CONTROL.sub("", value).strip()
    return sanitized[:max_characters]


def _validation_details(error: ValidationError) -> str:
    details: list[str] = []
    for item in error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )[:6]:
        location = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in item["loc"]
        )
        message = sanitize_model_text(str(item["msg"]), max_characters=240)
        details.append(f"{location}: {message}")
    return "; ".join(details) or "$: value does not match the schema"
