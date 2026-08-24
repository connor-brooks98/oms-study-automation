import pytest
from pydantic import SecretStr

from oms_hub.providers.gemini.errors import (
    GeminiAuthenticationError,
    GeminiContractError,
    GeminiProviderError,
    GeminiQuotaError,
    GeminiTransientError,
)
from oms_hub.providers.gemini.models import GeminiConfig


def test_api_key_is_never_serialized() -> None:
    config = GeminiConfig(api_key=SecretStr("secret-value"))

    assert "secret-value" not in repr(config)
    assert "secret-value" not in str(config.model_dump())
    assert "secret-value" not in config.model_dump_json()
    assert "secret-value" not in config.to_redacted_dict().values()
    assert config.to_redacted_dict()["api_key_configured"] is True


def test_default_configuration_matches_pinned_provider_contract() -> None:
    config = GeminiConfig(api_key=SecretStr("x"))

    assert config.api_key_secret_name == "gemini_api_key"
    assert config.sdk_version == "2.14.0"
    assert config.file_search_model == "gemini-3.7-flash"
    assert config.embedding_model == "models/gemini-embedding-2"
    assert config.api_version == "v1beta"
    assert config.request_timeout_seconds == 120
    assert config.operation_poll_seconds == 2
    assert config.operation_timeout_seconds == 900
    assert config.maximum_document_bytes == 100 * 1024 * 1024
    assert config.maximum_store_input_bytes == 6_442_450_944


def test_maximum_document_size_matches_provider_limit() -> None:
    assert GeminiConfig(api_key=SecretStr("x")).maximum_document_bytes == 100 * 1024 * 1024


def test_empty_key_disables_provider() -> None:
    with pytest.raises(ValueError, match="api key"):
        GeminiConfig(api_key=SecretStr(""))


def test_whitespace_only_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="api key"):
        GeminiConfig(api_key=SecretStr("  \t\n"))


@pytest.mark.parametrize(
    ("error_type", "category", "retryable"),
    (
        (GeminiAuthenticationError, "authentication", False),
        (GeminiQuotaError, "quota", True),
        (GeminiTransientError, "transient", True),
        (GeminiContractError, "contract", False),
    ),
)
def test_provider_errors_record_normalized_metadata(
    error_type: type[GeminiProviderError],
    category: str,
    retryable: bool,
) -> None:
    error = error_type(
        "safe provider failure",
        provider_status_code=503,
        provider_request_id="request-123",
    )

    assert error.category == category
    assert error.retryable is retryable
    assert error.provider_status_code == 503
    assert error.provider_request_id == "request-123"
    assert error.redacted_message == "safe provider failure"
    assert str(error) == "safe provider failure"
