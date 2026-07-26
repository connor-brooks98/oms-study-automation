from collections.abc import Mapping

from oms_hub.llm.domain import (
    CleanResult,
    DiagnosticSource,
    LLMRequestError,
    ProviderConnection,
    ProviderName,
)
from oms_hub.llm.provider import LLMProvider
from oms_hub.llm.repository import LLMSettingsRepository
from oms_hub.security.secret_store import SecretStore
from oms_hub.transcripts.prompt import ApprovedPrompt

SECRET_KEYS = {
    ProviderName.OPENAI: "openai-api-key",
    ProviderName.GEMINI: "gemini-api-key",
    ProviderName.ANTHROPIC: "anthropic-api-key",
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
        preference = self.settings.active()
        api_key = self._credential(preference.provider)
        provider = self.providers[preference.provider]
        return provider.clean(
            raw_text,
            prompt,
            api_key=api_key,
            model=preference.model,
        )

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

    def credential_configured(self, provider: ProviderName) -> bool:
        value = self.secrets.get(SECRET_KEYS[provider])
        return bool(value and value.strip())

    def _credential(self, provider: ProviderName) -> str:
        value = self.secrets.get(SECRET_KEYS[provider])
        if not value or not value.strip():
            raise LLMRequestError(
                f"{provider.value.title()} credential is not configured",
                source=DiagnosticSource.AUTHENTICATION,
            )
        return value

