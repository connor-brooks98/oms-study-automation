import re

from oms_hub.llm.domain import LLMTask, ProviderName
from tests.v2.test_llm_settings_routes import prepared_client


def test_settings_renders_four_secret_safe_provider_cards(tmp_path):
    client, _, secrets = prepared_client(tmp_path)
    secrets.set("openai-api-key", "sentinel-secret")

    response = client.get("/settings")
    password_fields = re.findall(
        r"<input\b[^>]*data-credential-input[^>]*>",
        response.text,
    )

    assert response.status_code == 200
    assert response.text.count("data-provider-card") == 4
    assert 'data-provider="openai"' in response.text
    assert 'data-provider="gemini"' in response.text
    assert 'data-provider="anthropic"' in response.text
    assert 'data-provider="openrouter"' in response.text
    assert "sentinel-secret" not in response.text
    assert len(password_fields) == 4
    assert all('type="password"' in field for field in password_fields)
    assert all(" value=" not in field for field in password_fields)
    assert response.text.count("data-toggle-password") == 4
    assert response.text.count("data-test-connection") == 4
    assert response.text.count('data-diagnostic aria-live="polite"') == 4


def test_settings_provider_kicker_reflects_task_assignment_counts(tmp_path):
    client, app, _ = prepared_client(tmp_path)
    # Bind two existing assignments to Gemini and the third to Anthropic.
    # The two quiz defaults remain on OpenAI, so task usage is derived from
    # all five assignments rather than the retired "active" column.
    app.state.llm_settings.set_assignment(
        LLMTask.TRANSCRIPTS,
        ProviderName.GEMINI,
        "gemini-3.6-flash",
    )
    app.state.llm_settings.set_assignment(
        LLMTask.ANKI_CURATION,
        ProviderName.GEMINI,
        "gemini-3.6-flash",
    )
    app.state.llm_settings.set_assignment(
        LLMTask.ACCURACY_REVIEW,
        ProviderName.ANTHROPIC,
        "claude-sonnet-5",
    )

    response = client.get("/settings")

    assert response.status_code == 200
    assert "Active" not in response.text
    assert "Available" not in response.text
    assert response.text.count("Used by 2 tasks") == 2
    assert response.text.count("Used by 1 task") == 1
    assert response.text.count("Not assigned") == 1


def test_settings_renders_external_script(tmp_path):
    client, _, _ = prepared_client(tmp_path)

    response = client.get("/settings")

    assert re.search(
        r'<script src="/static/settings\.js(?:\?v=[^"]+)?" defer></script>',
        response.text,
    )


def test_settings_renders_five_secret_safe_task_assignment_rows(tmp_path):
    client, _, secrets = prepared_client(tmp_path)
    secrets.set("openai-api-key", "sentinel-secret")

    response = client.get("/settings")

    assert response.status_code == 200
    assert response.text.count("data-assignment-row") == 5
    assert 'data-task="quiz_extraction"' in response.text
    assert 'data-task="quiz_answer_generation"' in response.text
    assert "Quiz question extraction" in response.text
    assert "Missing-answer generation" in response.text
    assert "sentinel-secret" not in response.text
