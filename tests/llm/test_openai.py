import httpx
import respx

from oms_hub.llm.domain import ProviderName
from oms_hub.llm.openai import OpenAIProvider
from oms_hub.transcripts.prompt import ApprovedPrompt


@respx.mock
def test_openai_provider_sends_responses_request_and_parses_usage():
    route = respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp-123",
                "status": "completed",
                "model": "gpt-5.2",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "Cleaned lecture."}
                        ],
                    }
                ],
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        )
    )
    provider = OpenAIProvider(
        input_usd_per_million=2.0,
        output_usd_per_million=10.0,
    )

    result = provider.clean(
        "Raw lecture.",
        ApprovedPrompt("Remove filler.", "a" * 64),
        api_key="secret",
        model="gpt-5.2",
    )

    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer secret"
    assert result.provider is ProviderName.OPENAI
    assert result.text == "Cleaned lecture."
    assert result.model == "gpt-5.2"
    assert result.request_id == "resp-123"
    assert result.input_tokens == 100
    assert result.output_tokens == 20
    assert result.cost_microusd == 400


@respx.mock
def test_openai_connection_test_uses_a_real_minimal_generation():
    route = respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp-test",
                "status": "completed",
                "model": "gpt-5.2",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "OK"}],
                    }
                ],
                "usage": {"input_tokens": 5, "output_tokens": 1},
            },
        )
    )

    result = OpenAIProvider().test_connection("secret", "gpt-5.2")

    assert result.request_id == "resp-test"
    assert route.calls.call_count == 1

