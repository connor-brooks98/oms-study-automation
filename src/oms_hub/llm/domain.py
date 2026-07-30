from dataclasses import dataclass
from enum import StrEnum


class ProviderName(StrEnum):
    OPENAI = "openai"
    GEMINI = "gemini"
    ANTHROPIC = "anthropic"


class DiagnosticSource(StrEnum):
    STUDY_HUB = "study_hub"
    NETWORK = "network"
    AUTHENTICATION = "provider_authentication"
    MODEL = "provider_model"
    QUOTA = "provider_quota"
    SERVICE = "provider_service"


class LLMRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        source: DiagnosticSource,
        http_status: int | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.source = source
        self.http_status = http_status
        self.provider_request_id = provider_request_id


@dataclass(frozen=True, slots=True)
class CleanResult:
    text: str
    provider: ProviderName
    model: str
    request_id: str
    input_tokens: int
    output_tokens: int
    cost_microusd: int


@dataclass(frozen=True, slots=True)
class GeneratedText:
    text: str
    provider: ProviderName
    model: str
    request_id: str
    input_tokens: int
    output_tokens: int
    cost_microusd: int


@dataclass(frozen=True, slots=True)
class ProviderConnection:
    provider: ProviderName
    model: str
    request_id: str | None


@dataclass(frozen=True, slots=True)
class ProviderPreference:
    provider: ProviderName
    model: str
    active: bool
    last_test_state: str | None
    last_tested_at: str | None
    diagnostic_source: str | None
    diagnostic_message: str | None
    http_status: int | None
    provider_request_id: str | None
