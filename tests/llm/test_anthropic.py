import httpx
import respx

from oms_hub.llm.anthropic import AnthropicProvider
from oms_hub.llm.domain import ProviderName
from oms_hub.transcripts.prompt import ApprovedPrompt


@respx.mock
def test_anthropic_provider_sends_messages_request_and_parses_usage():
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            headers={"request-id": "anthropic-request"},
            json={
                "id": "message-123",
                "model": "claude-sonnet-5",
                "content": [
                    {"type": "text", "text": "Cleaned Claude lecture."}
                ],
                "usage": {"input_tokens": 80, "output_tokens": 16},
            },
        )
    )
    provider = AnthropicProvider(
        input_usd_per_million=3.0,
        output_usd_per_million=15.0,
    )

    result = provider.clean(
        "Raw lecture.",
        ApprovedPrompt("Remove filler.", "a" * 64),
        api_key="secret",
        model="claude-sonnet-5",
    )

    request = route.calls.last.request
    assert request.headers["x-api-key"] == "secret"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert result.provider is ProviderName.ANTHROPIC
    assert result.text == "Cleaned Claude lecture."
    assert result.model == "claude-sonnet-5"
    assert result.request_id == "anthropic-request"
    assert result.input_tokens == 80
    assert result.output_tokens == 16
    assert result.cost_microusd == 480

