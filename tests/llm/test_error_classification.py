import httpx
import pytest
import respx

from oms_hub.llm.domain import DiagnosticSource, LLMRequestError
from oms_hub.llm.openai import OpenAIProvider
from oms_hub.transcripts.prompt import ApprovedPrompt


@pytest.mark.parametrize(
    ("status", "source"),
    [
        (401, DiagnosticSource.AUTHENTICATION),
        (403, DiagnosticSource.AUTHENTICATION),
        (400, DiagnosticSource.MODEL),
        (404, DiagnosticSource.MODEL),
        (429, DiagnosticSource.QUOTA),
        (503, DiagnosticSource.SERVICE),
    ],
)
@respx.mock
def test_provider_http_errors_are_safely_classified(status, source):
    respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(
            status,
            headers={"x-request-id": "safe-request"},
            json={"error": {"message": "secret provider body"}},
        )
    )

    with pytest.raises(LLMRequestError) as raised:
        OpenAIProvider().clean(
            "Raw",
            ApprovedPrompt("Prompt", "a" * 64),
            api_key="sentinel-secret",
            model="gpt-5.2",
        )

    assert raised.value.source is source
    assert raised.value.http_status == status
    assert raised.value.provider_request_id == "safe-request"
    assert "sentinel-secret" not in str(raised.value)
    assert "secret provider body" not in str(raised.value)


@respx.mock
def test_network_timeout_is_distinguished_from_provider_failure():
    respx.post("https://api.openai.com/v1/responses").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )

    with pytest.raises(LLMRequestError) as raised:
        OpenAIProvider().clean(
            "Raw",
            ApprovedPrompt("Prompt", "a" * 64),
            api_key="sentinel-secret",
            model="gpt-5.2",
        )

    assert raised.value.source is DiagnosticSource.NETWORK
    assert raised.value.http_status is None
    assert "sentinel-secret" not in str(raised.value)


@respx.mock
def test_malformed_success_is_a_provider_service_issue():
    respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(200, json={"status": "completed"})
    )

    with pytest.raises(LLMRequestError) as raised:
        OpenAIProvider().clean(
            "Raw",
            ApprovedPrompt("Prompt", "a" * 64),
            api_key="sentinel-secret",
            model="gpt-5.2",
        )

    assert raised.value.source is DiagnosticSource.SERVICE
    assert "sentinel-secret" not in str(raised.value)
