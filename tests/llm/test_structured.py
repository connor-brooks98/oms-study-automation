from collections.abc import Sequence

import pytest
from pydantic import BaseModel

from oms_hub.llm.domain import (
    GeneratedText,
    ProviderName,
)
from oms_hub.llm.structured import (
    StructuredOutputError,
    StructuredTextService,
)


class Answer(BaseModel):
    value: int
    labels: tuple[str, ...]


class FakeTextGenerator:
    def __init__(self, responses: Sequence[GeneratedText]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def generate_text(
        self,
        instruction: str,
        input_text: str,
        *,
        output_schema: dict[str, object],
        provider: ProviderName,
        model: str,
    ) -> GeneratedText:
        self.calls.append(
            {
                "instruction": instruction,
                "input_text": input_text,
                "output_schema": output_schema,
                "provider": provider,
                "model": model,
            }
        )
        return self.responses.pop(0)


def _generated(text: str) -> GeneratedText:
    return GeneratedText(
        text=text,
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        request_id="request-1",
        input_tokens=11,
        output_tokens=7,
        cost_microusd=3,
    )


def test_generate_json_returns_validated_value_and_metadata() -> None:
    generator = FakeTextGenerator(
        [_generated('{"value": 7, "labels": ["heme", "iron"]}')]
    )
    service = StructuredTextService(generator)

    result = service.generate_json(
        "Return the answer.",
        "Question",
        output_model=Answer,
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
    )

    assert result.value == Answer(value=7, labels=("heme", "iron"))
    assert result.raw_text.startswith("{")
    assert result.provider is ProviderName.OPENAI
    assert generator.calls[0]["output_schema"]["type"] == "object"  # type: ignore[index]


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        '{"value": "wrong", "labels": []}',
        '```json\n{"value": 1}\n```',
    ],
)
def test_generate_json_rejects_invalid_or_schema_mismatched_output(
    text: str,
) -> None:
    service = StructuredTextService(FakeTextGenerator([_generated(text)]))

    with pytest.raises(StructuredOutputError) as raised:
        service.generate_json(
            "Return the answer.",
            "Question",
            output_model=Answer,
            provider=ProviderName.OPENAI,
            model="gpt-5.2",
        )

    assert raised.value.raw_text
    assert "validation" in str(raised.value).casefold()


def test_schema_validation_error_identifies_the_invalid_field_without_echoing_input() -> None:
    private_value = "do-not-echo-this-response-value"
    service = StructuredTextService(
        FakeTextGenerator(
            [_generated(f'{{"value": "{private_value}", "labels": []}}')]
        )
    )

    with pytest.raises(StructuredOutputError) as raised:
        service.generate_json(
            "Return the answer.",
            "Question",
            output_model=Answer,
            provider=ProviderName.OPENAI,
            model="gpt-5.2",
        )

    message = str(raised.value)
    assert "$.value" in message
    assert "valid integer" in message
    assert private_value not in message


def test_generate_json_accepts_a_complete_json_fence() -> None:
    service = StructuredTextService(
        FakeTextGenerator(
            [
                _generated(
                    '```json\n{"value": 2, "labels": ["safe"]}\n```'
                )
            ]
        )
    )

    result = service.generate_json(
        "Return the answer.",
        "Question",
        output_model=Answer,
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
    )

    assert result.value.value == 2
