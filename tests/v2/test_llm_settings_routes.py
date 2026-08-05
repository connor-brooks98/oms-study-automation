from dataclasses import dataclass

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.llm.domain import (
    DiagnosticSource,
    LLMRequestError,
    LLMTask,
    ProviderConnection,
    ProviderName,
)
from oms_hub.llm.openai import OpenAIProvider
from oms_hub.web import settings_routes


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


@pytest.fixture(autouse=True)
def _clear_model_cache():
    settings_routes._model_cache.clear()
    yield
    settings_routes._model_cache.clear()


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


def test_models_endpoint_returns_fallback_when_key_missing(tmp_path):
    client, _, _ = prepared_client(tmp_path)

    response = client.get("/api/settings/providers/openai/models")
    payload = response.json()

    assert response.status_code == 200
    assert payload["source"] == "fallback"
    assert payload["models"]
    assert response.headers["cache-control"] == "no-store"


def test_models_endpoint_unknown_provider_returns_404(tmp_path):
    client, _, _ = prepared_client(tmp_path)

    response = client.get("/api/settings/providers/not-a-provider/models")

    assert response.status_code == 404


@respx.mock
def test_models_endpoint_returns_live_models_and_caches_second_call(tmp_path):
    client, app, secrets = prepared_client(tmp_path)
    app.state.llm_service.providers[ProviderName.OPENAI] = OpenAIProvider()
    secrets.set("openai-api-key", "sentinel-secret")
    route = respx.get("https://api.openai.com/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "gpt-4.1"}, {"id": "gpt-5.2"}]},
        )
    )

    first = client.get("/api/settings/providers/openai/models")
    second = client.get("/api/settings/providers/openai/models")

    assert first.status_code == 200
    assert first.json() == {
        "models": ["gpt-4.1", "gpt-5.2"],
        "source": "live",
    }
    assert second.json() == first.json()
    assert route.calls.call_count == 1
    assert "sentinel-secret" not in first.text
    assert "sentinel-secret" not in second.text


@respx.mock
def test_models_endpoint_falls_back_when_provider_request_fails(tmp_path):
    client, app, secrets = prepared_client(tmp_path)
    app.state.llm_service.providers[ProviderName.OPENAI] = OpenAIProvider()
    secrets.set("openai-api-key", "sentinel-secret")
    respx.get("https://api.openai.com/v1/models").mock(
        return_value=httpx.Response(
            401,
            json={"error": {"message": "invalid api key: sentinel-secret"}},
        )
    )

    response = client.get("/api/settings/providers/openai/models")
    payload = response.json()

    assert response.status_code == 200
    assert payload["source"] == "fallback"
    assert "sentinel-secret" not in response.text


def test_task_assignment_put_updates_provider_and_model(tmp_path):
    client, app, secrets = prepared_client(tmp_path)
    secrets.set("gemini-api-key", "secret")

    response = client.put(
        "/api/settings/task-assignments/transcripts",
        json={"provider": "gemini", "model": "gemini-3.6-flash"},
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload == {
        "task": "transcripts",
        "provider": "gemini",
        "model": "gemini-3.6-flash",
        "key_configured": True,
    }
    assert app.state.llm_settings.assignment(LLMTask.TRANSCRIPTS).model == (
        "gemini-3.6-flash"
    )
    assert response.headers["cache-control"] == "no-store"


def test_task_assignment_put_reports_unconfigured_credential(tmp_path):
    client, _, _ = prepared_client(tmp_path)

    response = client.put(
        "/api/settings/task-assignments/anki_curation",
        json={"provider": "anthropic", "model": "claude-sonnet-5"},
    )

    assert response.status_code == 200
    assert response.json()["key_configured"] is False


def test_quiz_task_assignments_are_updated_independently_without_credentials(tmp_path):
    client, app, secrets = prepared_client(tmp_path)

    extraction = client.put(
        "/api/settings/task-assignments/quiz_extraction",
        json={"provider": "openrouter", "model": "deepseek/model"},
    )
    answer = client.put(
        "/api/settings/task-assignments/quiz_answer_generation",
        json={"provider": "openai", "model": "gpt-answer"},
    )

    assert extraction.status_code == 200
    assert extraction.json()["key_configured"] is False
    assert answer.status_code == 200
    assert answer.json()["key_configured"] is False
    assert app.state.llm_settings.assignment(LLMTask.QUIZ_EXTRACTION).model == (
        "deepseek/model"
    )
    assert app.state.llm_settings.assignment(LLMTask.QUIZ_ANSWER_GENERATION).model == (
        "gpt-answer"
    )
    assert secrets.values == {}


def test_task_assignment_put_unknown_task_returns_404(tmp_path):
    client, _, _ = prepared_client(tmp_path)

    response = client.put(
        "/api/settings/task-assignments/not-a-task",
        json={"provider": "openai", "model": "gpt-5.2"},
    )

    assert response.status_code == 404


def test_task_assignment_put_unknown_provider_returns_422(tmp_path):
    client, _, _ = prepared_client(tmp_path)

    response = client.put(
        "/api/settings/task-assignments/transcripts",
        json={"provider": "not-a-provider", "model": "gpt-5.2"},
    )

    assert response.status_code == 422


def test_task_assignment_put_blank_model_returns_422(tmp_path):
    client, _, _ = prepared_client(tmp_path)

    response = client.put(
        "/api/settings/task-assignments/transcripts",
        json={"provider": "openai", "model": "   "},
    )

    assert response.status_code == 422
