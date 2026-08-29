"""Managed async lifecycle and safe error translation for the Gemini SDK."""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any, NoReturn

import httpx

from oms_hub.providers.gemini.errors import (
    GeminiAuthenticationError,
    GeminiContractError,
    GeminiProviderError,
    GeminiQuotaError,
    GeminiTransientError,
)
from oms_hub.providers.gemini.models import GeminiConfig

SdkFactory = Callable[..., Any]

_REQUEST_ID_HEADERS = frozenset(
    {
        "request-id",
        "x-request-id",
        "x-goog-request-id",
        "x-generation-id",
    }
)
_INVALID_ARGUMENT = "INVALID_ARGUMENT"
_UNSUPPORTED_MIME_PREFIX = "Unsupported MIME type:"


def _official_sdk_factory(
    *,
    api_key: str,
    http_options: dict[str, object],
) -> Any:
    """Construct the official client only when the provider is actually used."""

    try:
        genai = importlib.import_module("google.genai")
    except (ImportError, ModuleNotFoundError):
        raise GeminiContractError(
            "Gemini SDK is unavailable; install the approved provider dependency."
        ) from None
    except Exception as error:
        _raise_translated(error)
    try:
        return genai.Client(api_key=api_key, http_options=http_options)
    except Exception as error:
        _raise_translated(error)


class GeminiClientFactory:
    """Create one top-level SDK client for each application context or batch."""

    def __init__(
        self,
        config: GeminiConfig,
        sdk_factory: SdkFactory | None = None,
    ) -> None:
        self.config = config
        self.sdk_factory = sdk_factory if sdk_factory is not None else _official_sdk_factory

    def _build_sdk_client(self) -> Any:
        try:
            return self.sdk_factory(
                api_key=self.config.api_key.get_secret_value(),
                http_options={
                    "api_version": self.config.api_version,
                    "timeout": self.config.request_timeout_seconds * 1_000,
                },
            )
        except GeminiProviderError as error:
            raise error from None
        except Exception as error:
            _raise_translated(error)

    @asynccontextmanager
    async def client(self) -> AsyncIterator[Any]:
        """Yield the SDK async facade and close it exactly once on exit."""

        sdk_client = self._build_sdk_client()
        try:
            aio = sdk_client.aio
        except Exception as error:
            _raise_translated(error)
        try:
            close = getattr(aio, "aclose", None)
        except Exception as error:
            _raise_translated(error)
        if not callable(close):
            raise GeminiContractError(
                "Gemini SDK async client does not expose the required close method."
            )
        body_failed = False
        try:
            try:
                yield aio
            except BaseException:
                body_failed = True
                raise
        finally:
            try:
                await close()
            except BaseException as error:
                if not isinstance(error, Exception):
                    raise
                if body_failed:
                    pass
                else:
                    _raise_translated(error)


def translate_gemini_error(exc: Exception) -> GeminiProviderError:
    """Normalize SDK failures without copying provider payloads into diagnostics."""

    if isinstance(exc, GeminiProviderError):
        return exc

    status_code = _safe_status_code(exc)
    request_id = _safe_request_id(exc)

    if status_code in {401, 403}:
        return GeminiAuthenticationError(
            "Gemini authentication failed.",
            provider_status_code=status_code,
            provider_request_id=request_id,
        )
    if status_code in {408, 429}:
        return GeminiQuotaError(
            "Gemini quota or rate limit reached; retry with provider delay.",
            provider_status_code=status_code,
            provider_request_id=request_id,
        )
    if status_code in {500, 502, 503, 504}:
        return GeminiTransientError(
            "Gemini service is temporarily unavailable; retry the persisted phase.",
            provider_status_code=status_code,
            provider_request_id=request_id,
        )
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return GeminiTransientError(
            "Gemini operation timed out; resume from the persisted phase.",
            provider_status_code=status_code,
            provider_request_id=request_id,
            diagnostic_code="timeout",
        )
    if isinstance(exc, httpx.TransportError):
        return GeminiTransientError(
            "Gemini transport failed; retry the persisted phase.",
            provider_status_code=status_code,
            provider_request_id=request_id,
            diagnostic_code="transport_error",
        )
    if isinstance(exc, (AttributeError, TypeError, KeyError, ValueError)):
        return GeminiContractError(
            "Gemini SDK response did not match the expected contract.",
            provider_status_code=status_code,
            provider_request_id=request_id,
            diagnostic_code="sdk_contract",
        )
    return GeminiProviderError(
        "Gemini provider request failed.",
        provider_status_code=status_code,
        provider_request_id=request_id,
        diagnostic_code=_safe_provider_diagnostic(exc),
    )


def _raise_translated(error: Exception) -> NoReturn:
    raise translate_gemini_error(error) from None


def _safe_status_code(exc: Exception) -> int | None:
    for source in (exc, _safe_attr(exc, "response")):
        if source is None:
            continue
        for name in ("status_code", "http_status", "status"):
            value = _safe_attr(source, name)
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and 100 <= value <= 599:
                return value
            if isinstance(value, str) and len(value) <= 3 and value.isdecimal():
                parsed = int(value)
                if 100 <= parsed <= 599:
                    return parsed
    return None


def _safe_provider_diagnostic(exc: Exception) -> str:
    if _safe_attr(exc, "status") != _INVALID_ARGUMENT:
        return "unknown_provider"
    message = _safe_attr(exc, "message")
    if type(message) is str and message.startswith(_UNSUPPORTED_MIME_PREFIX):
        return "unsupported_mime_type"
    return "invalid_argument"


def _safe_request_id(exc: Exception) -> str | None:
    for source in (exc, _safe_attr(exc, "response")):
        if source is None:
            continue
        for name in ("provider_request_id", "request_id", "requestId"):
            request_id = _safe_identifier(_safe_attr(source, name))
            if request_id is not None:
                return request_id
        headers = _safe_attr(source, "headers")
        request_id = _safe_header_request_id(headers)
        if request_id is not None:
            return request_id
    return None


def _safe_attr(value: object, name: str) -> object | None:
    try:
        return getattr(value, name, None)
    except Exception:
        return None


def _safe_identifier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value[:200]
    if not value.isprintable():
        return None
    value = value.strip()
    return value or None


def _safe_header_request_id(headers: object) -> str | None:
    if not isinstance(headers, Mapping):
        return None
    try:
        for index, (key, value) in enumerate(headers.items()):
            if index >= 32:
                break
            if (
                isinstance(key, str)
                and len(key) <= 64
                and key.casefold() in _REQUEST_ID_HEADERS
            ):
                request_id = _safe_identifier(value)
                if request_id is not None:
                    return request_id
    except Exception:
        return None
    return None


__all__ = ["GeminiClientFactory", "translate_gemini_error"]
