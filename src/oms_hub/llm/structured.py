import json
import re
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ValidationError

from oms_hub.llm.domain import GeneratedText, ProviderName

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


class StructuredOutputError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        raw_text: str,
        generation: GeneratedText,
    ) -> None:
        super().__init__(message)
        self.raw_text = raw_text
        self.generation = generation


class StructuredTextGenerator(Protocol):
    def generate_text(
        self,
        instruction: str,
        input_text: str,
        *,
        output_schema: dict[str, object],
        provider: ProviderName,
        model: str,
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
    ) -> StructuredJSONResult[StructuredModel]:
        generated = self.generator.generate_text(
            instruction,
            input_text,
            output_schema=output_model.model_json_schema(),
            provider=provider,
            model=model,
        )
        raw_text = sanitize_model_text(generated.text)
        match = _JSON_FENCE.fullmatch(raw_text)
        json_text = match.group("body") if match is not None else raw_text
        try:
            payload = json.loads(json_text)
        except json.JSONDecodeError as exc:
            raise StructuredOutputError(
                "structured output failed JSON schema validation: "
                f"$: invalid JSON at line {exc.lineno}, column {exc.colno}",
                raw_text=raw_text,
                generation=generated,
            ) from exc
        try:
            value = output_model.model_validate(payload)
        except ValidationError as exc:
            raise StructuredOutputError(
                "structured output failed JSON schema validation: "
                + _validation_details(exc),
                raw_text=raw_text,
                generation=generated,
            ) from exc
        except TypeError as exc:
            raise StructuredOutputError(
                "structured output failed JSON schema validation: "
                "$: invalid structured value",
                raw_text=raw_text,
                generation=generated,
            ) from exc
        return StructuredJSONResult(
            value=value,
            raw_text=raw_text,
            provider=generated.provider,
            model=generated.model,
            request_id=generated.request_id,
            input_tokens=generated.input_tokens,
            output_tokens=generated.output_tokens,
            cost_microusd=generated.cost_microusd,
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
