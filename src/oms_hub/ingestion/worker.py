import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from oms_hub.files.office import (
    OfficeConversionError,
    OfficeTimeoutError,
    OfficeUnavailableError,
)
from oms_hub.ingestion.domain import IngestionJob, UploadKind, UploadState
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.transcripts.cleaner import OpenAIRateLimitError, OpenAITransientError
from oms_hub.transcripts.pipeline import TranscriptValidationError

logger = logging.getLogger(__name__)


class IngestionPipeline(Protocol):
    def process(self, item_id: str) -> object: ...


class IngestionWorker:
    max_attempts = 3

    def __init__(
        self,
        repository: IngestionRepository,
        slide_pipeline: IngestionPipeline,
        transcript_pipeline: IngestionPipeline,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.slide_pipeline = slide_pipeline
        self.transcript_pipeline = transcript_pipeline
        self.now = now or (lambda: datetime.now(UTC))

    def recover_interrupted_jobs(self) -> int:
        return self.repository.recover_interrupted_jobs()

    def run_once(self) -> bool:
        job = self.repository.claim_next_job(self.now())
        if job is None:
            return False
        try:
            if job.action != "process":
                raise ValueError(f"unsupported ingestion action: {job.action}")
            pipeline = (
                self.slide_pipeline
                if job.kind is UploadKind.SLIDES
                else self.transcript_pipeline
            )
            pipeline.process(job.upload_item_id)
        except Exception as error:  # noqa: BLE001 - job boundary records all failures
            self._handle_failure(job, error)
        return True

    def _handle_failure(self, job: IngestionJob, error: Exception) -> None:
        detail = self._concise_error(error)
        if self._is_transient(error) and job.attempts < self.max_attempts:
            delay = timedelta(seconds=5 * (2 ** (job.attempts - 1)))
            self.repository.retry_job(job, detail, delay=delay)
            logger.warning(
                "V2 ingestion job %s will retry after attempt %s: %s",
                job.id,
                job.attempts,
                detail,
            )
            return
        failure_state = (
            UploadState.QUARANTINED
            if isinstance(error, (TranscriptValidationError, ValueError))
            else UploadState.NEEDS_REVIEW
        )
        self.repository.fail_job(job, detail, state=failure_state)
        logger.error(
            "V2 ingestion job %s stopped after attempt %s: %s",
            job.id,
            job.attempts,
            detail,
        )

    @staticmethod
    def _is_transient(error: Exception) -> bool:
        return isinstance(
            error,
            (
                OfficeConversionError,
                OfficeTimeoutError,
                OfficeUnavailableError,
                OpenAIRateLimitError,
                OpenAITransientError,
            ),
        )

    @staticmethod
    def _concise_error(error: Exception) -> str:
        detail = " ".join(str(error).split())
        return (detail or type(error).__name__)[:1000]
