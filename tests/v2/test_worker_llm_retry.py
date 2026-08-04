import sqlite3

import pytest
from sqlalchemy.exc import OperationalError

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
        DiagnosticSource.REQUEST,
        DiagnosticSource.STUDY_HUB,
    ],
)
def test_permanent_llm_failures_are_not_retried(source):
    error = LLMRequestError("safe", source=source)
    assert IngestionWorker._is_transient(error) is False


def test_sqlite_busy_errors_are_retried():
    orig = sqlite3.OperationalError("database is locked")
    orig.sqlite_errorcode = sqlite3.SQLITE_BUSY
    error = OperationalError("stmt", {}, orig)

    assert IngestionWorker._is_transient(error) is True
