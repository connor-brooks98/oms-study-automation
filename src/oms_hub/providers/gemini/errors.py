"""Normalized errors exposed by the Gemini provider adapter."""

from __future__ import annotations


class GeminiProviderError(RuntimeError):
    """Base error with provider-safe diagnostics and retry metadata."""

    category = "provider"
    retryable = False

    def __init__(
        self,
        redacted_message: str,
        *,
        provider_status_code: int | None = None,
        provider_request_id: str | None = None,
        category: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        self.category = type(self).category if category is None else category
        self.retryable = type(self).retryable if retryable is None else retryable
        self.provider_status_code = provider_status_code
        self.provider_request_id = provider_request_id
        self.redacted_message = redacted_message
        super().__init__(redacted_message)


class GeminiAuthenticationError(GeminiProviderError):
    """The provider rejected authentication or authorization."""

    category = "authentication"
    retryable = False


class GeminiQuotaError(GeminiProviderError):
    """The provider applied a quota or rate-limit response."""

    category = "quota"
    retryable = True


class GeminiTransientError(GeminiProviderError):
    """The provider or network failed in a retryable way."""

    category = "transient"
    retryable = True


class GeminiContractError(GeminiProviderError):
    """The SDK/provider response no longer matches the adapter contract."""

    category = "contract"
    retryable = False
