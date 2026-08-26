from dataclasses import dataclass
from pathlib import Path

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


def test_openrouter_uses_only_generic_provider_routes_and_clears_diagnostics(tmp_path):
    client, app, secrets = prepared_client(tmp_path)
    app.state.llm_settings.record_test(
        ProviderName.OPENROUTER,
        state="failed",
        tested_at="2026-08-09T00:00:00+00:00",
        diagnostic_source="network",
        diagnostic_message="stale diagnostic",
    )

    saved = client.post(
        "/settings/ai/openrouter/credential",
        json={"credential": "router-secret"},
    )

    source = Path(settings_routes.__file__).read_text(encoding="utf-8")
    preference = app.state.llm_settings.get(ProviderName.OPENROUTER)
    assert saved.json() == {"provider": "openrouter", "configured": True}
    assert secrets.values["openrouter-api-key"] == "router-secret"
    assert preference.last_test_state is None
    assert preference.diagnostic_message is None
    assert '@router.post("/ai/openrouter/credential")' not in source
    assert '@router.post("/ai/openrouter/model")' not in source
    assert '@router.post("/ai/openrouter/test")' not in source
    assert '@router.post("/accuracy-gate")' in source


def test_openrouter_generic_test_uses_its_saved_card_model_not_accuracy_assignment(tmp_path):
    client, app, secrets = prepared_client(tmp_path)
    secrets.set("openrouter-api-key", "router-secret")
    app.state.llm_settings.set_model(ProviderName.OPENROUTER, "router/card-model")
    app.state.llm_settings.set_assignment(
        LLMTask.ACCURACY_REVIEW,
        ProviderName.GEMINI,
        "gemini/assigned-model",
    )

    response = client.post("/settings/ai/openrouter/test")

    assert response.status_code == 200
    assert response.json()["provider"] == "openrouter"
    assert response.json()["state"] == "connected"
    assert app.state.llm_settings.assignment(LLMTask.ACCURACY_REVIEW).model == (
        "gemini/assigned-model"
    )


def test_openrouter_generic_model_change_preserves_accuracy_assignment(tmp_path):
    client, app, _ = prepared_client(tmp_path)
    app.state.llm_settings.set_assignment(
        LLMTask.ACCURACY_REVIEW,
        ProviderName.GEMINI,
        "gemini/assigned-model",
    )
    app.state.llm_settings.record_test(
        ProviderName.OPENROUTER,
        state="failed",
        tested_at="2026-08-09T00:00:00+00:00",
        diagnostic_source="provider_model",
        diagnostic_message="stale model diagnostic",
    )

    response = client.post(
        "/settings/ai/openrouter/model",
        json={"model": "openrouter/card-model"},
    )

    assert response.status_code == 200
    preference = app.state.llm_settings.get(ProviderName.OPENROUTER)
    assert preference.model == "openrouter/card-model"
    assert preference.last_test_state is None
    assert preference.diagnostic_message is None
    assignment = app.state.llm_settings.assignment(LLMTask.ACCURACY_REVIEW)
    assert assignment.provider is ProviderName.GEMINI
    assert assignment.model == "gemini/assigned-model"


def test_accuracy_gate_has_a_distinct_non_provider_endpoint(tmp_path):
    client, _, _ = prepared_client(tmp_path)

    response = client.post("/settings/accuracy-gate", json={"enabled": True})

    assert response.status_code == 200
    assert response.json() == {"enabled": True}


def test_voyage_credential_is_saved_separately_and_blank_retains_existing(tmp_path):
    client, app, secrets = prepared_client(tmp_path)
    client.get("/settings")
    csrf = client.cookies.get(app.state.csrf.cookie_name)

    saved = client.post(
        "/settings/anki/voyage/credential",
        json={"credential": " voyage-secret "},
        headers={app.state.csrf.header_name: csrf},
    )
    retained = client.post(
        "/settings/anki/voyage/credential",
        json={"credential": "   "},
        headers={app.state.csrf.header_name: csrf},
    )

    assert saved.json() == {"configured": True}
    assert retained.json() == {"configured": True}
    assert saved.headers["cache-control"] == "no-store"
    assert secrets.values == {"voyage-api-key": "voyage-secret"}


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


def test_model_mutation_clears_prior_provider_diagnostics(tmp_path):
    client, app, _ = prepared_client(tmp_path)
    app.state.llm_settings.record_test(
        ProviderName.GEMINI,
        state="failed",
        tested_at="2026-08-09T00:00:00+00:00",
        diagnostic_source="provider_model",
        diagnostic_message="old model failure",
    )

    response = client.post(
        "/settings/ai/gemini/model",
        json={"model": "gemini-3.6-flash"},
    )

    assert response.status_code == 200
    preference = app.state.llm_settings.get(ProviderName.GEMINI)
    assert preference.last_test_state is None
    assert preference.diagnostic_message is None


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
