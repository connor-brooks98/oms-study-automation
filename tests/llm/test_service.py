from dataclasses import dataclass

import pytest

from oms_hub.db import Database
from oms_hub.llm.domain import (
    CleanResult,
    DiagnosticSource,
    LLMRequestError,
    ProviderConnection,
    ProviderName,
)
from oms_hub.llm.repository import LLMSettingsRepository
from oms_hub.llm.service import LLMService
from oms_hub.transcripts.prompt import ApprovedPrompt


class MemorySecrets:
    def __init__(self, values):
        self.values = values

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


@dataclass
class StubProvider:
    name: ProviderName
    settings: LLMSettingsRepository | None = None
    switch_to: ProviderName | None = None

    def clean(self, raw_text, prompt, *, api_key, model):
        if self.settings is not None and self.switch_to is not None:
            self.settings.set_active(self.switch_to)
        return CleanResult(
            text=f"{self.name.value}:{raw_text}",
            provider=self.name,
            model=model,
            request_id=f"{self.name.value}-request",
            input_tokens=10,
            output_tokens=5,
            cost_microusd=0,
        )

    def test_connection(self, api_key, model):
        return ProviderConnection(self.name, model, f"{self.name.value}-test")


def prepared_service(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    settings = LLMSettingsRepository(database, default_openai_model="gpt-5.2")
    providers = {name: StubProvider(name) for name in ProviderName}
    secrets = MemorySecrets(
        {
            "openai-api-key": "openai-secret",
            "gemini-api-key": "gemini-secret",
            "anthropic-api-key": "anthropic-secret",
        }
    )
    return settings, LLMService(settings, secrets, providers)


def test_clean_resolves_active_provider_for_each_new_call(tmp_path):
    settings, service = prepared_service(tmp_path)
    prompt = ApprovedPrompt("Prompt", "a" * 64)

    settings.set_active(ProviderName.GEMINI)
    first = service.clean("first", prompt)
    settings.set_active(ProviderName.ANTHROPIC)
    second = service.clean("second", prompt)

    assert first.provider is ProviderName.GEMINI
    assert second.provider is ProviderName.ANTHROPIC


def test_clean_captures_provider_and_model_before_request_starts(tmp_path):
    settings, service = prepared_service(tmp_path)
    provider = StubProvider(
        ProviderName.GEMINI,
        settings=settings,
        switch_to=ProviderName.ANTHROPIC,
    )
    service.providers[ProviderName.GEMINI] = provider
    settings.set_active(ProviderName.GEMINI)

    result = service.clean("raw", ApprovedPrompt("Prompt", "a" * 64))

    assert result.provider is ProviderName.GEMINI
    assert result.model == "gemini-3.6-flash"
    assert settings.active().provider is ProviderName.ANTHROPIC


def test_missing_credential_is_an_authentication_diagnostic(tmp_path):
    settings, service = prepared_service(tmp_path)
    settings.set_active(ProviderName.GEMINI)
    service.secrets.delete("gemini-api-key")

    with pytest.raises(LLMRequestError) as raised:
        service.clean("raw", ApprovedPrompt("Prompt", "a" * 64))

    assert raised.value.source is DiagnosticSource.AUTHENTICATION
    assert raised.value.http_status is None


def test_configured_status_fails_closed_when_credential_store_is_unavailable(
    tmp_path,
):
    _, service = prepared_service(tmp_path)

    class FailingSecrets:
        def get(self, key):
            raise RuntimeError("credential backend unavailable")

        def set(self, key, value):
            raise RuntimeError

        def delete(self, key):
            raise RuntimeError

    service.secrets = FailingSecrets()

    assert service.credential_configured(ProviderName.OPENAI) is False
