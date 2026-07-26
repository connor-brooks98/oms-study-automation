import pytest

from oms_hub.ingestion.worker import IngestionWorker
from oms_hub.llm.domain import DiagnosticSource, LLMRequestError


@pytest.mark.parametrize(
    "source",
    [
        DiagnosticSource.NETWORK,
        DiagnosticSource.QUOTA,
        DiagnosticSource.SERVICE,
    ],
)
def test_transient_llm_failures_are_retried(source):
    error = LLMRequestError("safe", source=source)
    assert IngestionWorker._is_transient(error) is True


@pytest.mark.parametrize(
    "source",
    [
        DiagnosticSource.AUTHENTICATION,
        DiagnosticSource.MODEL,
        DiagnosticSource.STUDY_HUB,
    ],
)
def test_permanent_llm_failures_are_not_retried(source):
    error = LLMRequestError("safe", source=source)
    assert IngestionWorker._is_transient(error) is False
