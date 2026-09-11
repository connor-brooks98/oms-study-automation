from datetime import UTC, datetime
from types import SimpleNamespace

from oms_hub.ingestion.domain import IngestionJob, UploadKind
from oms_hub.ingestion.worker import IngestionWorker


def test_ingestion_uses_recorded_backend_without_paid_fallback():
    calls = []
    job = IngestionJob(1, 'item', UploadKind.TRANSCRIPTS, 'process', 1, datetime.now(UTC),
        backend='codex_subscription')
    repository = SimpleNamespace(claim_next_job=lambda now: job)
    legacy = SimpleNamespace(process=lambda item: calls.append('legacy'))
    gpt = SimpleNamespace(process=lambda item: calls.append('gpt'))
    worker = IngestionWorker(repository, legacy, legacy, gpt_transcript_pipeline=gpt)
    assert worker.run_once()
    assert calls == ['gpt']
