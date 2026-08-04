from dataclasses import dataclass

from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.llm.domain import (
    DiagnosticSource,
    LLMRequestError,
    ProviderConnection,
    ProviderName,
)


class MemorySecrets:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


@dataclass
class ConnectionProvider:
    name: ProviderName
    error: LLMRequestError | None = None

    def clean(self, raw_text, prompt, *, api_key, model):
        raise NotImplementedError

    def test_connection(self, api_key, model):
        if self.error is not None:
            raise self.error
        return ProviderConnection(self.name, model, "provider-request")


def prepared_client(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        allow_local_access=True,
    )
    app = create_app(settings)
    secrets = MemorySecrets()
    app.state.secrets = secrets
    app.state.llm_service.secrets = secrets
    for provider in ProviderName:
        app.state.llm_service.providers[provider] = ConnectionProvider(provider)
    return TestClient(app), app, secrets


def test_credentials_are_saved_independently_and_blank_retains_existing(tmp_path):
    client, _, secrets = prepared_client(tmp_path)

    openai = client.post(
        "/settings/ai/openai/credential",
        json={"credential": "openai-secret"},
    )
    gemini = client.post(
        "/settings/ai/gemini/credential",
        json={"credential": "gemini-secret"},
    )
    retained = client.post(
        "/settings/ai/openai/credential",
        json={"credential": "   "},
    )

    assert openai.json() == {"provider": "openai", "configured": True}
    assert gemini.json() == {"provider": "gemini", "configured": True}
    assert retained.json() == {"provider": "openai", "configured": True}
    assert secrets.values == {
        "openai-api-key": "openai-secret",
        "gemini-api-key": "gemini-secret",
    }
    assert openai.headers["cache-control"] == "no-store"


def test_model_can_change_without_restart(tmp_path):
    client, app, secrets = prepared_client(tmp_path)
    secrets.set("gemini-api-key", "secret")

    model = client.post(
        "/settings/ai/gemini/model",
        json={"model": "gemini-3.6-flash"},
    )

    assert model.json()["model"] == "gemini-3.6-flash"
    assert app.state.llm_settings.get(ProviderName.GEMINI).model == (
        "gemini-3.6-flash"
    )


def test_connection_test_returns_connected_state_and_safe_metadata(tmp_path):
    client, _, secrets = prepared_client(tmp_path)
    secrets.set("openai-api-key", "secret")

    response = client.post("/settings/ai/openai/test")
    payload = response.json()

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert payload["provider"] == "openai"
    assert payload["state"] == "connected"
    assert payload["diagnostic"] is None
    assert payload["provider_request_id"] == "provider-request"
    assert payload["correlation_id"]
    assert payload["tested_at"]


def test_connection_test_returns_sanitized_provider_failure(tmp_path):
    client, app, secrets = prepared_client(tmp_path)
    secrets.set("gemini-api-key", "sentinel-secret")
    app.state.llm_service.providers[ProviderName.GEMINI] = ConnectionProvider(
        ProviderName.GEMINI,
        LLMRequestError(
            "Gemini rejected the credential",
            source=DiagnosticSource.AUTHENTICATION,
            http_status=401,
            provider_request_id="provider-request",
        ),
    )

    response = client.post("/settings/ai/gemini/test")
    payload = response.json()

    assert response.status_code == 200
    assert payload["state"] == "failed"
    assert payload["diagnostic"]["source"] == "provider_authentication"
    assert payload["diagnostic"]["http_status"] == 401
    assert "sentinel-secret" not in response.text


def test_unknown_provider_is_rejected(tmp_path):
    client, _, _ = prepared_client(tmp_path)

    response = client.post(
        "/settings/ai/not-a-provider/credential",
        json={"credential": "secret"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "AI provider was not found"

