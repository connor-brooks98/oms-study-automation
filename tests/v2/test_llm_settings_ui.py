import re

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


def test_settings_renders_external_script(tmp_path):
    client, _, _ = prepared_client(tmp_path)

    response = client.get("/settings")

    assert re.search(
        r'<script src="/static/settings\.js(?:\?v=[^"]+)?" defer></script>',
        response.text,
    )
