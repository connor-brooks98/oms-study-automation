import pytest
from notebooklm import AuthError, ConfigurationError, NetworkError

from oms_hub.llm.domain import DiagnosticSource
from oms_hub.study_generation.notebook_errors import (
    NotebookAuthenticationError,
    translate_notebook_error,
)


@pytest.mark.parametrize("error_type", [NetworkError, TimeoutError, ConnectionError])
@pytest.mark.parametrize(
    "message",
    [
        "Could not reach https://accounts.google.com/. Run 'notebooklm login'.",
        "Authentication expired or invalid. Redirected to: https://accounts.google.com/",
    ],
)
def test_network_errors_remain_retryable_despite_login_text(error_type, message):
    translated = translate_notebook_error(error_type(message))

    assert translated is not None
    assert translated.source is DiagnosticSource.NETWORK
    assert translated.retryable is True
    assert not isinstance(translated, NotebookAuthenticationError)


def test_configuration_failure_does_not_require_reconnect():
    translated = translate_notebook_error(
        ConfigurationError("Missing optional dependency for notebooklm login")
    )

    assert translated is not None
    assert translated.source is DiagnosticSource.VALIDATION
    assert translated.retryable is False
    assert not isinstance(translated, NotebookAuthenticationError)
    assert "configuration" in str(translated).lower()
    assert "reconnect" not in str(translated).lower()


@pytest.mark.parametrize(
    "error",
    [
        AuthError("Expired session"),
        RuntimeError(
            "Authentication expired or invalid. Redirected to: "
            "https://accounts.google.com/ Run 'notebooklm login' to re-authenticate."
        ),
        ValueError(
            "Authentication expired or invalid. Final URL: "
            "https://accounts.google.com/\nRun 'notebooklm login' to re-authenticate."
        ),
        ValueError("Authentication expired. Run 'notebooklm login' to re-authenticate."),
        RuntimeError("Authentication required. Run 'notebooklm login' to re-authenticate."),
        ValueError(
            "Authentication expired or invalid; authuser=0 did not return a signed-in "
            "account. Run 'notebooklm login' to re-authenticate."
        ),
        FileNotFoundError(
            "Storage file not found: /example/storage.json\n"
            "Run 'notebooklm login' to authenticate first."
        ),
    ],
)
def test_explicit_authentication_failures_still_require_login(error):
    translated = translate_notebook_error(error)

    assert isinstance(translated, NotebookAuthenticationError)
    assert translated.retryable is False


@pytest.mark.parametrize(
    "message",
    [
        "Request to https://accounts.google.com/ failed",
        "Try running notebooklm login if needed",
        "Could not launch browser to login to NotebookLM",
    ],
)
def test_login_mentions_alone_do_not_imply_expired_session(message):
    assert translate_notebook_error(RuntimeError(message)) is None
