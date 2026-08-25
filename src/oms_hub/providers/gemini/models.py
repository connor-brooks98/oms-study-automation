"""Configuration for the pinned Gemini File Search adapter."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class GeminiConfig(BaseModel):
    """Validated, provider-local Gemini settings.

    The API key is accepted for adapter use but excluded from every normal
    Pydantic serialization path. Use :meth:`to_redacted_dict` for diagnostics.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    _maximum_provider_document_bytes: ClassVar[int] = 100 * 1024 * 1024
    _maximum_internal_store_input_bytes: ClassVar[int] = 6_442_450_944

    api_key: SecretStr = Field(..., exclude=True, repr=False)
    api_key_secret_name: str = "gemini_api_key"
    sdk_version: str = "2.14.0"
    file_search_model: str = "gemini-3.7-flash"
    embedding_model: str = "models/gemini-embedding-2"
    api_version: str = "v1beta"
    request_timeout_seconds: int = Field(default=120, gt=0)
    operation_poll_seconds: int = Field(default=2, gt=0)
    operation_timeout_seconds: int = Field(default=900, gt=0)
    maximum_document_bytes: int = Field(
        default=_maximum_provider_document_bytes,
        gt=0,
        le=_maximum_provider_document_bytes,
    )
    maximum_store_input_bytes: int = Field(
        default=_maximum_internal_store_input_bytes,
        gt=0,
        le=_maximum_internal_store_input_bytes,
    )

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        normalized = value.get_secret_value().strip()
        if not normalized:
            raise ValueError("api key must not be empty")
        return SecretStr(normalized)

    @field_validator(
        "api_key_secret_name",
        "sdk_version",
        "file_search_model",
        "embedding_model",
        "api_version",
    )
    @classmethod
    def validate_nonblank_string(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("configuration value must not be blank")
        return normalized

    def to_redacted_dict(self) -> dict[str, object]:
        """Return serializable configuration metadata without the API key."""

        redacted = self.model_dump(mode="json")
        redacted["api_key_configured"] = bool(self.api_key.get_secret_value())
        return redacted
