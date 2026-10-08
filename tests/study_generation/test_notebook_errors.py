import pytest

from oms_hub.llm.domain import DiagnosticSource
from oms_hub.study_generation.notebook_errors import (
    NotebookAuthenticationError,
    NotebookGatewayError,
    NotebookScopeBusyError,
    NotebookScopeLostError,
    NotebookSourceNotFoundError,
    translate_notebook_error,
)


@pytest.mark.parametrize("error_type", [TimeoutError, ConnectionError])
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
    assert str(translated) == "NotebookLM is temporarily unreachable."


@pytest.mark.parametrize("error", [
    NotebookGatewayError("Configuration is invalid", source=DiagnosticSource.VALIDATION,
                         retryable=False),
    NotebookAuthenticationError(),
    NotebookSourceNotFoundError(),
    NotebookScopeBusyError(),
    NotebookScopeLostError(),
])
def test_app_owned_errors_retain_identity_and_diagnostic_metadata(error):
    assert translate_notebook_error(error) is error


@pytest.mark.parametrize(
    "error",
    [
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
def test_explicit_historical_authentication_failures_remain_classified(error):
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
