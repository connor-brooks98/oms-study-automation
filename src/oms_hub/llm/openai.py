from typing import Any

import httpx

from oms_hub.llm.domain import (
    CleanResult,
    GeneratedText,
    ProviderConnection,
    ProviderName,
)
from oms_hub.llm.provider import (
    FIXED_TRANSCRIPT_CONSTRAINTS,
    estimated_cost,
    invalid_response,
    post_provider_json,
    response_object,
    token_count,
    transcript_input,
)
from oms_hub.transcripts.prompt import ApprovedPrompt


class OpenAIProvider:
    name = ProviderName.OPENAI
    url = "https://api.openai.com/v1/responses"

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
    ) -> GeneratedText:
        response = self._request(
            api_key,
            {
                "model": model,
                "store": False,
                "instructions": instruction,
                "input": input_text,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "structured_output",
                        "schema": output_schema,
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
