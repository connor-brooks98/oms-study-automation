import asyncio
import sys
from collections.abc import Coroutine
from typing import Any

import pytest
from pydantic import SecretStr

from oms_hub.providers.gemini.client import GeminiClientFactory, translate_gemini_error
from oms_hub.providers.gemini.errors import (
    GeminiAuthenticationError,
    GeminiContractError,
    GeminiProviderError,
    GeminiQuotaError,
    GeminiTransientError,
)
from oms_hub.providers.gemini.models import GeminiConfig


class FakeAioClient:
    def __init__(self) -> None:
        self.closed = False
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        self.closed = True


class FakeSdkClient:
    def __init__(self) -> None:
        self.aio = FakeAioClient()


class FakeSdkFactory:
    def __init__(self) -> None:
        self.clients: list[FakeSdkClient] = []
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> FakeSdkClient:
        self.calls.append(kwargs)
        client = FakeSdkClient()
        self.clients.append(client)
        return client


class SdkError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.headers = headers or {}


def gemini_config() -> GeminiConfig:
    return GeminiConfig(api_key=SecretStr("raw-secret"))


def run(coroutine: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coroutine)


def test_importing_client_does_not_import_google_genai() -> None:
    assert "google.genai" not in sys.modules


def test_async_client_is_closed_after_context() -> None:
    sdk = FakeSdkFactory()
    factory = GeminiClientFactory(config=gemini_config(), sdk_factory=sdk)

    async def exercise() -> None:
        async with factory.client() as client:
            assert client is sdk.clients[0].aio
            assert not sdk.clients[0].aio.closed

    run(exercise())

    assert sdk.clients[0].aio.closed
    assert sdk.clients[0].aio.close_calls == 1


def test_injected_sdk_receives_raw_key_and_version_without_secret_wrapper() -> None:
    sdk = FakeSdkFactory()
    factory = GeminiClientFactory(config=gemini_config(), sdk_factory=sdk)

    async def exercise() -> None:
        async with factory.client():
            pass

    run(exercise())

    assert sdk.calls == [{"api_key": "raw-secret", "http_options": {"api_version": "v1beta"}}]


def test_one_top_level_sdk_client_is_created_per_context() -> None:
    sdk = FakeSdkFactory()
    factory = GeminiClientFactory(config=gemini_config(), sdk_factory=sdk)

    async def exercise() -> None:
        async with factory.client() as first:
            async with factory.client() as second:
                assert first is sdk.clients[0].aio
                assert second is sdk.clients[1].aio
                assert first is not second

    run(exercise())

    assert len(sdk.clients) == 2
    assert [client.aio.close_calls for client in sdk.clients] == [1, 1]


def test_async_client_is_closed_when_context_body_raises() -> None:
    sdk = FakeSdkFactory()
    factory = GeminiClientFactory(config=gemini_config(), sdk_factory=sdk)

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="body failure"):
            async with factory.client():
                raise RuntimeError("body failure")

    run(exercise())

    assert sdk.clients[0].aio.closed
    assert sdk.clients[0].aio.close_calls == 1


@pytest.mark.parametrize(
    ("status", "error_type", "category", "retryable"),
    (
        (401, GeminiAuthenticationError, "authentication", False),
        (403, GeminiAuthenticationError, "authentication", False),
        (408, GeminiQuotaError, "quota", True),
        (429, GeminiQuotaError, "quota", True),
        (500, GeminiTransientError, "transient", True),
        (502, GeminiTransientError, "transient", True),
        (503, GeminiTransientError, "transient", True),
        (504, GeminiTransientError, "transient", True),
    ),
)
def test_status_errors_are_normalized_without_provider_payload(
    status: int,
    error_type: type[GeminiProviderError],
    category: str,
    retryable: bool,
) -> None:
    translated = translate_gemini_error(
        SdkError(
            "raw-secret and private provider payload",
            status_code=status,
            headers={"x-request-id": "request-123"},
        )
    )

    assert type(translated) is error_type
    assert translated.category == category
    assert translated.retryable is retryable
    assert translated.provider_status_code == status
    assert translated.provider_request_id == "request-123"
    assert "raw-secret" not in translated.redacted_message
    assert "private provider payload" not in translated.redacted_message


def test_timeout_error_is_retryable_transient() -> None:
    translated = translate_gemini_error(TimeoutError("raw-secret timeout payload"))

    assert isinstance(translated, GeminiTransientError)
    assert translated.retryable
    assert translated.redacted_message == (
        "Gemini operation timed out; resume from the persisted phase."
    )
    assert "raw-secret" not in str(translated)


@pytest.mark.parametrize(
    "error",
    (
        AttributeError("private payload"),
        TypeError("private payload"),
        KeyError("private payload"),
    ),
)
def test_sdk_shape_errors_are_non_retryable_contract_errors(error: Exception) -> None:
    translated = translate_gemini_error(error)

    assert isinstance(translated, GeminiContractError)
    assert not translated.retryable
    assert translated.redacted_message == (
        "Gemini SDK response did not match the expected contract."
    )
    assert "private payload" not in str(translated)


def test_existing_provider_error_is_returned_unchanged() -> None:
    original = GeminiQuotaError(
        "safe provider message",
        provider_status_code=429,
        provider_request_id="request-123",
    )

    assert translate_gemini_error(original) is original


def test_unknown_error_uses_fixed_redacted_message() -> None:
    translated = translate_gemini_error(
        ValueError("raw-secret and private response body")
    )

    assert type(translated) is GeminiProviderError
    assert translated.redacted_message == "Gemini provider request failed."
    assert translated.provider_status_code is None
    assert translated.provider_request_id is None
    assert "raw-secret" not in str(translated)
    assert "private response body" not in str(translated)


def test_default_sdk_seam_fails_closed_when_google_genai_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "google.genai", None)
    factory = GeminiClientFactory(config=gemini_config())

    async def exercise() -> None:
        with pytest.raises(GeminiContractError) as raised:
            async with factory.client():
                pass
        assert raised.value.redacted_message == (
            "Gemini SDK is unavailable; install the approved provider dependency."
        )
        assert "raw-secret" not in str(raised.value)

    run(exercise())
