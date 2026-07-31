import pytest
from notebooklm import (
    AuthError,
    ChatResponseParseError,
    ClientError,
    NetworkError,
    NotebookLimitError,
    RateLimitError,
    ServerError,
    SourceProcessingError,
    ValidationError,
)

from oms_hub.llm.domain import DiagnosticSource
from oms_hub.study_generation.notebook_errors import (
    NotebookAuthenticationError,
    translate_notebook_error,
)


@pytest.mark.parametrize(
    ("error", "source", "retryable"),
    [
        (AuthError("expired"), DiagnosticSource.AUTHENTICATION, False),
        (NetworkError("offline"), DiagnosticSource.NETWORK, True),
        (TimeoutError("late"), DiagnosticSource.NETWORK, True),
        (RateLimitError("slow down"), DiagnosticSource.QUOTA, True),
        (NotebookLimitError(100), DiagnosticSource.QUOTA, False),
        (ServerError("upstream"), DiagnosticSource.SERVICE, True),
        (ChatResponseParseError("bad chat"), DiagnosticSource.SERVICE, True),
        (
            SourceProcessingError("source failed"),
            DiagnosticSource.SOURCE_PROCESSING,
            True,
        ),
        (ValidationError("bad input"), DiagnosticSource.VALIDATION, False),
        (ClientError("bad request"), DiagnosticSource.VALIDATION, False),
    ],
)
def test_notebook_failures_have_typed_diagnostics(error, source, retryable):
    translated = translate_notebook_error(error)

    assert translated is not None
    assert translated.source is source
    assert translated.retryable is retryable


def test_authentication_diagnostic_does_not_expose_provider_message():
    translated = translate_notebook_error(
        AuthError("accounts.google.com secret-cookie-value")
    )

    assert isinstance(translated, NotebookAuthenticationError)
    assert "secret-cookie-value" not in str(translated)


def test_arbitrary_message_text_is_not_used_for_classification():
    assert (
        translate_notebook_error(RuntimeError("authentication expired rate limit"))
        is None
    )
