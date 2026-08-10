from notebooklm import (
    AuthError,
    ChatError,
    ClientError,
    ConfigurationError,
    DecodingError,
    NetworkError,
    NotebookLimitError,
    NotebookLMError,
    RateLimitError,
    ServerError,
    SourceError,
    ValidationError,
)

from oms_hub.llm.domain import DiagnosticSource


class NotebookGatewayError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        source: DiagnosticSource,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.source = source
        self.retryable = retryable


class NotebookAuthenticationError(NotebookGatewayError):
    def __init__(
        self,
        message: str = "NotebookLM login expired; reconnect Google in Settings.",
    ) -> None:
        super().__init__(
            message,
            source=DiagnosticSource.AUTHENTICATION,
            retryable=False,
        )


class NotebookSourceNotFoundError(NotebookGatewayError):
    """The requested remote source is already absent."""

    def __init__(self) -> None:
        super().__init__(
            "NotebookLM source was already removed.",
            source=DiagnosticSource.SOURCE_PROCESSING,
            retryable=False,
        )


class NotebookScopeBusyError(NotebookGatewayError):
    """Another durable worker currently owns this logical notebook."""

    def __init__(self) -> None:
        super().__init__(
            "Another NotebookLM operation is already running for this subject and exam.",
            source=DiagnosticSource.STUDY_HUB,
            retryable=True,
        )


def translate_notebook_error(
    error: BaseException,
) -> NotebookGatewayError | None:
    if isinstance(error, NotebookGatewayError):
        return error
    message = str(error).casefold()
    if any(
        phrase in message
        for phrase in ("accounts.google.com", "notebooklm login", "login to notebooklm")
    ):
        return NotebookAuthenticationError()
    if isinstance(error, (AuthError, ConfigurationError)):
        return NotebookAuthenticationError()
    if isinstance(error, SourceError):
        return NotebookGatewayError(
            "NotebookLM could not process a selected source.",
            source=DiagnosticSource.SOURCE_PROCESSING,
            retryable=True,
        )
    if isinstance(error, RateLimitError):
        return NotebookGatewayError(
            "NotebookLM rate limit reached; the job will retry.",
            source=DiagnosticSource.QUOTA,
            retryable=True,
        )
    if isinstance(error, NotebookLimitError):
        return NotebookGatewayError(
            "NotebookLM notebook quota is exhausted.",
            source=DiagnosticSource.QUOTA,
            retryable=False,
        )
    if isinstance(error, (NetworkError, TimeoutError, ConnectionError)):
        return NotebookGatewayError(
            "NotebookLM is temporarily unreachable.",
            source=DiagnosticSource.NETWORK,
            retryable=True,
        )
    if isinstance(error, (ValidationError, ClientError)):
        return NotebookGatewayError(
            "NotebookLM rejected the request.",
            source=DiagnosticSource.VALIDATION,
            retryable=False,
        )
    if isinstance(
        error,
        (ServerError, ChatError, DecodingError, NotebookLMError),
    ):
        return NotebookGatewayError(
            "NotebookLM returned a service error; the job will retry.",
            source=DiagnosticSource.SERVICE,
            retryable=True,
        )
    return None
