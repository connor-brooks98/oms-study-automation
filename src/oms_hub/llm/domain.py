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

