import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from oms_hub.artifact_writes import ArtifactWriteClaimLost, ArtifactWriteContended
from oms_hub.ingestion.domain import IngestionJob, UploadKind
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


@pytest.mark.parametrize("error", [ArtifactWriteContended("held"), ArtifactWriteClaimLost("lost")])
def test_claim_failures_are_deferred_after_ingestion_retry_limit(error):
    job = IngestionJob(1, "item", UploadKind.TRANSCRIPTS, "process", 99, datetime.now(UTC))
    calls = []
    repository = SimpleNamespace(
        claim_next_job=lambda now: job,
        retry_job=lambda item, detail, delay: calls.append((item, detail, delay)),
        fail_job=lambda *args, **kwargs: pytest.fail("must not become terminal"),
    )
    pipeline = SimpleNamespace(process=lambda item_id: (_ for _ in ()).throw(error))
    assert IngestionWorker(repository, pipeline, pipeline).run_once() is True
    assert calls and calls[0][0] is job
