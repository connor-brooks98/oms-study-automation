import httpx

from oms_hub.llm.domain import (
    CleanResult,
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


class AnthropicProvider:
    name = ProviderName.ANTHROPIC
    url = "https://api.anthropic.com/v1/messages"

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
        )
        result = self._clean_result(response, model)
        return ProviderConnection(self.name, result.model, result.request_id)

    def _request(
        self,
        api_key: str,
        model: str,
        system: str,
        content: str,
        *,
        max_tokens: int,
    ) -> httpx.Response:
        return post_provider_json(
            self.http,
            self.url,
            provider=self.name,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            payload={
                "model": model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [
                    {
                        "role": "user",
                        "content": content,
                    }
                ],
            },
        )

    def _clean_result(
        self,
        response: httpx.Response,
        requested_model: str,
    ) -> CleanResult:
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
        returned_model = payload.get("model", requested_model)
        if not isinstance(returned_model, str) or not returned_model:
            raise invalid_response(self.name, response)
        request_id = safe_request_id(response)
        if request_id is None:
            message_id = payload.get("id")
            request_id = message_id if isinstance(message_id, str) else ""
        return CleanResult(
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
