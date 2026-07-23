import hashlib

import httpx
import pytest
import respx

from oms_hub.panopto.openai_client import (
    OpenAIAuthenticationError,
    OpenAIResponseError,
    OpenAIRateLimitError,
    OpenAITranscriptCleaner,
    OpenAITransientError,
)
from oms_hub.panopto.prompt import (
    ApprovedPrompt,
    PromptInvalid,
    PromptLoader,
    PromptNotApproved,
)


class MemorySecrets:
    def __init__(self, key: str | None = "sk-test-value"):
        self.key = key

    def get(self, key: str) -> str | None:
        return self.key if key == "openai-api-key" else None

    def set(self, key: str, value: str) -> None:
        self.key = value

    def delete(self, key: str) -> None:
        self.key = None


def approved_prompt() -> ApprovedPrompt:
    text = "Remove filler words but preserve every substantive fact."
    return ApprovedPrompt(text, hashlib.sha256(text.encode()).hexdigest())


def response_payload(**overrides):
    payload = {
        "id": "resp_123",
        "status": "completed",
        "model": "gpt-5.6-terra",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Cleaned shoulder transcript.",
                    }
                ],
            }
        ],
        "usage": {"input_tokens": 11000, "output_tokens": 9000},
    }
    payload.update(overrides)
    return payload


def test_prompt_must_match_approved_hash(tmp_path):
    path = tmp_path / "Transcript Cleaning.md"
    path.write_text("Remove filler words but preserve facts.", encoding="utf-8")
    loader = PromptLoader(path, approved_sha256=None)

    with pytest.raises(PromptNotApproved):
        loader.current()


def test_prompt_initializer_does_not_overwrite_and_validates_exact_bytes(tmp_path):
    path = tmp_path / "prompts" / "Transcript Cleaning.md"
    loader = PromptLoader(path, approved_sha256=None)
    assert loader.initialize() == path
    original = path.read_bytes()
    loader.initialize()
    assert path.read_bytes() == original

    approved = hashlib.sha256(original).hexdigest()
    current = PromptLoader(path, approved).current()
    assert current.sha256 == approved
    path.write_text("", encoding="utf-8")
    with pytest.raises(PromptInvalid):
        PromptLoader(path, approved).current()


@respx.mock
def test_responses_request_disables_reasoning_storage_and_records_usage():
    route = respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(200, json=response_payload())
    )
    cleaner = OpenAITranscriptCleaner(
        MemorySecrets(), "gpt-5.6-terra", 2.50, 15.00
    )

    result = cleaner.clean("Raw shoulder transcript.", approved_prompt())
    request = route.calls[0].request

    assert b'"effort":"none"' in request.content
    assert b'"store":false' in request.content
    assert b"<raw_transcript>" in request.content
    assert result.cost_microusd == 162_500
    assert result.text == "Cleaned shoulder transcript."


@respx.mock
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, OpenAIAuthenticationError),
        (403, OpenAIAuthenticationError),
        (429, OpenAIRateLimitError),
        (500, OpenAITransientError),
    ],
)
def test_api_failures_are_typed_and_sanitized(status, error_type):
    respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(status, text="sk-test-value must not leak")
    )
    cleaner = OpenAITranscriptCleaner(
        MemorySecrets(), "gpt-5.6-terra", 2.50, 15.00
    )

    with pytest.raises(error_type) as captured:
        cleaner.clean("raw", approved_prompt())
    assert "sk-test-value" not in str(captured.value)


@respx.mock
def test_incomplete_missing_output_and_missing_usage_are_rejected():
    route = respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json=response_payload(status="incomplete"),
        )
    )
    cleaner = OpenAITranscriptCleaner(
        MemorySecrets(), "gpt-5.6-terra", 2.50, 15.00
    )

    with pytest.raises(OpenAIResponseError):
        cleaner.clean("raw", approved_prompt())
    route.mock(return_value=httpx.Response(200, json=response_payload(output=[])))
    with pytest.raises(OpenAIResponseError):
        cleaner.clean("raw", approved_prompt())
    route.mock(return_value=httpx.Response(200, json=response_payload(usage=None)))
    with pytest.raises(OpenAIResponseError):
        cleaner.clean("raw", approved_prompt())
