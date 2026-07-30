from urllib.parse import quote

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
    safe_request_id,
    token_count,
    transcript_input,
)
from oms_hub.transcripts.prompt import ApprovedPrompt


class GeminiProvider:
    name = ProviderName.GEMINI
    base_url = "https://generativelanguage.googleapis.com/v1beta/models"

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
            max_output_tokens=None,
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
            max_output_tokens=16,
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
    ) -> GeneratedText:
        response = self._request(
            api_key,
            model,
            instruction,
            input_text,
            max_output_tokens=32768,
            output_schema=output_schema,
        )
        return self._generated_text(response, model)

    def _request(
        self,
        api_key: str,
        model: str,
        instruction: str,
        content: str,
        *,
        max_output_tokens: int | None,
        output_schema: dict[str, object] | None,
    ) -> httpx.Response:
        payload: dict[str, object] = {
            "systemInstruction": {
                "parts": [{"text": instruction}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": content}],
                }
            ],
        }
        generation_config: dict[str, object] = {}
        if max_output_tokens is not None:
            generation_config["maxOutputTokens"] = max_output_tokens
        if output_schema is not None:
            generation_config["responseFormat"] = {
                "text": {
                    "mimeType": "application/json",
                    "schema": output_schema,
                }
            }
        if generation_config:
            payload["generationConfig"] = generation_config
        safe_model = quote(model, safe="-._")
        return post_provider_json(
            self.http,
            f"{self.base_url}/{safe_model}:generateContent",
            provider=self.name,
            headers={"x-goog-api-key": api_key},
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
        candidates = payload.get("candidates")
        usage = payload.get("usageMetadata")
        if not isinstance(candidates, list) or not isinstance(usage, dict):
            raise invalid_response(self.name, response)
        text_parts: list[str] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            if not isinstance(content, dict):
                continue
            parts = content.get("parts")
            if not isinstance(parts, list):
                continue
            for part in parts:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
        cleaned = "".join(text_parts).strip()
        if not cleaned:
            raise invalid_response(self.name, response)
        input_tokens = token_count(
            usage.get("promptTokenCount"),
            self.name,
            response,
        )
        output_tokens = token_count(
            usage.get("candidatesTokenCount"),
            self.name,
            response,
        )
        returned_model = payload.get("modelVersion", requested_model)
        if not isinstance(returned_model, str) or not returned_model:
            raise invalid_response(self.name, response)
        request_id = safe_request_id(response) or ""
        return GeneratedText(
            text=cleaned,
            provider=self.name,
            model=returned_model,
            request_id=request_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microusd=estimated_cost(
                input_tokens,
                output_tokens,
                self.input_usd_per_million,
                self.output_usd_per_million,
            ),
        )
