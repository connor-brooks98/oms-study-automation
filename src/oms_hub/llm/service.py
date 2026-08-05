from collections.abc import Mapping

from oms_hub.llm.domain import (
    DEFAULT_GENERATION_OPTIONS,
    CleanResult,
    DiagnosticSource,
    GeneratedText,
    GenerationOptions,
    LLMRequestError,
    LLMTask,
    ProviderCapabilities,
    ProviderConnection,
    ProviderName,
)
from oms_hub.llm.openrouter import OPENROUTER_API_KEY_SECRET
from oms_hub.llm.provider import LLMProvider
from oms_hub.llm.repository import LLMSettingsRepository
from oms_hub.security.secret_store import SecretStore
from oms_hub.transcripts.prompt import ApprovedPrompt

SECRET_KEYS = {
    ProviderName.OPENAI: "openai-api-key",
    ProviderName.GEMINI: "gemini-api-key",
    ProviderName.ANTHROPIC: "anthropic-api-key",
    ProviderName.OPENROUTER: OPENROUTER_API_KEY_SECRET,
}


class LLMService:
    def __init__(
        self,
        settings: LLMSettingsRepository,
        secrets: SecretStore,
        providers: Mapping[ProviderName, LLMProvider],
    ) -> None:
        missing = set(ProviderName) - set(providers)
        if missing:
            names = ", ".join(sorted(provider.value for provider in missing))
            raise ValueError(f"language model providers are missing: {names}")
        self.settings = settings
        self.secrets = secrets
        self.providers = dict(providers)

    def clean(
        self,
        raw_text: str,
        prompt: ApprovedPrompt,
    ) -> CleanResult:
        provider, model, api_key = self.for_task(LLMTask.TRANSCRIPTS)
        return provider.clean(
            raw_text,
            prompt,
            api_key=api_key,
            model=model,
        )

    def for_task(self, task: LLMTask) -> tuple[LLMProvider, str, str]:
        assignment = self.settings.assignment(task)
        api_key = self._credential(assignment.provider)
        provider = self.providers[assignment.provider]
        return provider, assignment.model, api_key

    def test_connection(
        self,
        provider_name: ProviderName,
    ) -> ProviderConnection:
        preference = self.settings.get(provider_name)
        api_key = self._credential(provider_name)
        return self.providers[provider_name].test_connection(
            api_key,
            preference.model,
        )

    def generate_text(
        self,
        instruction: str,
        input_text: str,
        *,
        output_schema: dict[str, object],
        provider: ProviderName,
        model: str,
        options: GenerationOptions = DEFAULT_GENERATION_OPTIONS,
    ) -> GeneratedText:
        api_key = self._credential(provider)
        arguments: dict[str, object] = {
            "api_key": api_key,
            "model": model,
            "output_schema": output_schema,
        }
        if options is not DEFAULT_GENERATION_OPTIONS:
            arguments["options"] = options
        return self.providers[provider].generate_text(
            instruction,
            input_text,
            **arguments,  # type: ignore[arg-type]
        )

    def capabilities_for(
        self,
        provider: ProviderName,
        model: str | None = None,
    ) -> ProviderCapabilities:
        """Expose conservative provider or selected-model guarantees locally."""
        adapter = self.providers[provider]
        if model is None:
            return adapter.capabilities
        return adapter.capabilities_for_model(model)

    def credential_configured(self, provider: ProviderName) -> bool:
        try:
            value = self.secrets.get(SECRET_KEYS[provider])
        except Exception:  # noqa: BLE001 - read-only status fails closed
            return False
        return bool(value and value.strip())

    def _credential(self, provider: ProviderName) -> str:
        value = self.secrets.get(SECRET_KEYS[provider])
        if not value or not value.strip():
            raise LLMRequestError(
                f"{provider.value.title()} credential is not configured",
                source=DiagnosticSource.AUTHENTICATION,
            )
        return value
