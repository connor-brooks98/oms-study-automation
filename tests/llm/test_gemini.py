import httpx
import respx

from oms_hub.llm.domain import ProviderName
from oms_hub.llm.gemini import GeminiProvider
from oms_hub.transcripts.prompt import ApprovedPrompt


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

