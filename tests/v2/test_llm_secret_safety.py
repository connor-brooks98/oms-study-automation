import logging

from oms_hub.llm.domain import (
    DiagnosticSource,
    LLMRequestError,
    ProviderName,
)
from tests.v2.test_llm_settings_routes import (
    ConnectionProvider,
    prepared_client,
)


def test_credential_never_returns_in_html_json_or_logs(tmp_path, caplog):
    client, app, _ = prepared_client(tmp_path)
    sentinel = "sentinel-private-credential"

    saved = client.post(
        "/settings/ai/anthropic/credential",
        json={"credential": sentinel},
    )
    page = client.get("/settings")
    app.state.llm_service.providers[ProviderName.ANTHROPIC] = (
        ConnectionProvider(
            ProviderName.ANTHROPIC,
            LLMRequestError(
                "Anthropic rejected the credential",
                source=DiagnosticSource.AUTHENTICATION,
                http_status=401,
            ),
        )
    )
    with caplog.at_level(logging.INFO):
        tested = client.post("/settings/ai/anthropic/test")

    assert sentinel not in saved.text
    assert sentinel not in page.text
    assert sentinel not in tested.text
    assert sentinel not in caplog.text
    assert "location" not in saved.headers
