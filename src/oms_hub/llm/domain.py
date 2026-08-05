from dataclasses import dataclass
from enum import StrEnum


class ProviderName(StrEnum):
    OPENAI = "openai"
    GEMINI = "gemini"
    ANTHROPIC = "anthropic"
    OPENROUTER = "openrouter"


class LLMTask(StrEnum):
    TRANSCRIPTS = "transcripts"
    ANKI_CURATION = "anki_curation"
    ACCURACY_REVIEW = "accuracy_review"


class ThinkingMode(StrEnum):
    """Whether a generation may spend output budget on extended thinking."""

    DISABLED = "disabled"
    ENABLED = "enabled"


@dataclass(frozen=True, slots=True)
class GenerationOptions:
    """Immutable, provider-neutral controls for one text generation call.

    ``cacheable_source_prefix`` is ordered before the per-call prompt. Providers
    that cannot explicitly cache it still send it as ordinary prompt context.
    ``thinking_budget_tokens`` is only used when ``thinking`` is enabled.
    """

    cacheable_source_prefix: str | None = None
    thinking: ThinkingMode = ThinkingMode.DISABLED
    thinking_budget_tokens: int = 1024

    def __post_init__(self) -> None:
        if not isinstance(self.thinking, ThinkingMode):
            raise TypeError("thinking must be a ThinkingMode")
        if self.cacheable_source_prefix is not None:
            if not isinstance(self.cacheable_source_prefix, str):
                raise TypeError("cacheable_source_prefix must be a string or None")
            if not self.cacheable_source_prefix.strip():
                raise ValueError("cacheable_source_prefix cannot be blank")
        if (
            isinstance(self.thinking_budget_tokens, bool)
            or not isinstance(self.thinking_budget_tokens, int)
            or self.thinking_budget_tokens < 1024
        ):
            raise ValueError("thinking_budget_tokens must be at least 1024")

    @property
    def thinking_mode(self) -> ThinkingMode:
        """Alias that reads naturally at stage-configuration call sites."""
        return self.thinking


DEFAULT_GENERATION_OPTIONS = GenerationOptions()


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Provider-wide guarantees; model-specific support must not be assumed."""

    prompt_prefix_caching: bool = False
    thinking: bool = False

    @property
    def supports_prompt_prefix_caching(self) -> bool:
        return self.prompt_prefix_caching

    @property
    def supports_thinking(self) -> bool:
        return self.thinking


class DiagnosticSource(StrEnum):
    STUDY_HUB = "study_hub"
    NETWORK = "network"
    AUTHENTICATION = "provider_authentication"
    MODEL = "provider_model"
    REQUEST = "provider_request"
    QUOTA = "provider_quota"
    SERVICE = "provider_service"
    SOURCE_PROCESSING = "source_processing"
    VALIDATION = "validation"
    CONTRACT = "contract"


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
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


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


@dataclass(frozen=True, slots=True)
class TaskAssignment:
    task: LLMTask
    provider: ProviderName
    model: str
