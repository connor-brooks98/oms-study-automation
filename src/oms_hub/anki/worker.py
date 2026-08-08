import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from oms_hub.anki.ankiconnect import AnkiConnectUnavailable
from oms_hub.anki.domain import CurationState
from oms_hub.anki.pipeline import (
    CurationPipeline,
    PinnedInputChanged,
    stage_definition,
)
from oms_hub.anki.repository import (
    AnkiCurationRepository,
    InvalidCurationTransition,
)
from oms_hub.anki.semantic.store import SemanticSnapshotError
from oms_hub.anki.semantic.voyage import VoyageEmbeddingError
from oms_hub.db import is_sqlite_busy
from oms_hub.llm.domain import DiagnosticSource, LLMRequestError
from oms_hub.llm.structured import StructuredOutputError

logger = logging.getLogger(__name__)


class AnkiCurationWorker:
    def __init__(
        self,
        repository: AnkiCurationRepository,
        pipeline: CurationPipeline,
        *,
        worker_id: str,
        lease_seconds: int = 120,
        poll_seconds: float = 5.0,
        max_stage_attempts: int = 3,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        worker_id = worker_id.strip()
        if not worker_id or len(worker_id) > 100:
            raise ValueError("worker ID is invalid")
        if lease_seconds < 3:
            raise ValueError("lease duration must be at least three seconds")
        if poll_seconds <= 0:
            raise ValueError("worker poll interval must be positive")
        if max_stage_attempts < 1:
            raise ValueError("maximum stage attempts must be positive")
        self.repository = repository
        self.pipeline = pipeline
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.max_stage_attempts = max_stage_attempts
        self.now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> bool:
        job = self.repository.claim_next_job(
            self.now(),
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if job is None:
            return False
        renewal_stop = asyncio.Event()
        renewal = asyncio.create_task(
            self._renew_lease(job.id, renewal_stop)
        )
        try:
            result = await self.pipeline.run_stage(
                job.id,
                lease_owner=self.worker_id,
                lease_clock=self.now,
            )
            if result is not None and result.state is CurationState.FAILED:
                current = self.repository.require_job(job.id)
                logger.error(
                    "Anki curation job %s stopped: %s",
                    job.id,
                    current.error,
                )
        except Exception as exc:  # noqa: BLE001 - durable worker boundary
            await self._handle_failure(job.id, job.state, exc)
        finally:
            renewal_stop.set()
            try:
                await renewal
            finally:
                self.repository.release_lease(job.id, self.worker_id)
        return True

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self.run_forever(),
            name=f"anki-curation-{self.worker_id}",
        )

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            await task

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                worked = await self.run_once()
            except Exception:
                logger.exception("Anki curation worker loop failed")
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self.poll_seconds,
                    )
                except TimeoutError:
                    pass

    async def _renew_lease(
        self,
        job_id: UUID,
        stop: asyncio.Event,
    ) -> None:
        interval = self.lease_seconds / 3
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                try:
                    renewed = self.repository.renew_lease(
                        job_id,
                        self.worker_id,
                        self.now(),
                        lease_seconds=self.lease_seconds,
                    )
                except Exception:
                    logger.exception(
                        "Anki curation lease renewal failed for %s",
                        job_id,
                    )
                    return
                if not renewed:
                    return

    async def _handle_failure(
        self,
        job_id: UUID,
        state: CurationState,
        error: Exception,
    ) -> None:
        current = self.repository.require_job(job_id)
        if current.state is CurationState.CANCELED:
            return
        definition = stage_definition(state, current.pipeline_contract_version)
        attempts = 1
        if definition is not None:
            stage = self.repository.get_stage(job_id, definition.stage)
            if stage is not None:
                attempts = stage.attempt_count
        safe = _safe_error(error)
        handled_at = self.now()
        try:
            if _is_retryable(error) and attempts < self.max_stage_attempts:
                delay = timedelta(
                    seconds=min(5 * (2 ** max(attempts - 1, 0)), 300)
                )
                self.repository.defer_job(
                    job_id,
                    self.worker_id,
                    safe,
                    expected_state=state,
                    available_at=handled_at + delay,
                    now=handled_at,
                )
                logger.warning(
                    "Anki curation job %s stage will retry: %s",
                    job_id,
                    safe,
                )
            else:
                self.repository.fail_job(
                    job_id,
                    self.worker_id,
                    safe,
                    expected_state=state,
                    now=handled_at,
                )
                logger.error(
                    "Anki curation job %s stopped: %s",
                    job_id,
                    safe,
                )
        except InvalidCurationTransition:
            latest = self.repository.require_job(job_id)
            lease_expired = latest.lease_expires_at is None or datetime.fromisoformat(
                latest.lease_expires_at
            ) <= handled_at
            if (
                latest.state is CurationState.CANCELED
                or latest.lease_owner != self.worker_id
                or lease_expired
            ):
                return
            raise


def _is_retryable(error: Exception) -> bool:
    if isinstance(error, PinnedInputChanged):
        return False
    if is_sqlite_busy(error):
        return True
    if isinstance(error, LLMRequestError):
        return error.source in {
            DiagnosticSource.NETWORK,
            DiagnosticSource.QUOTA,
            DiagnosticSource.SERVICE,
        }
    if isinstance(error, StructuredOutputError):
        return True
    return isinstance(
        error,
        (
            AnkiConnectUnavailable,
            VoyageEmbeddingError,
            SemanticSnapshotError,
            ConnectionError,
            TimeoutError,
        ),
    )


def _safe_error(error: Exception) -> str:
    return (" ".join(str(error).split()) or type(error).__name__)[:1_000]
