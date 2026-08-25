from dataclasses import dataclass
from typing import Protocol

GEMINI_INDEX_JOB_TYPE = "gemini_index_source_revision"


@dataclass(frozen=True, slots=True)
class WorkResult:
    worked: bool
    job_id: str | None = None

    def __post_init__(self) -> None:
        if self.job_id is not None and not self.job_id.strip():
            raise ValueError("job id must not be blank")


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    reclaimed_leases: int = 0
    resumed_jobs: int = 0
    terminal_failures: int = 0

    def __post_init__(self) -> None:
        if min(self.reclaimed_leases, self.resumed_jobs, self.terminal_failures) < 0:
            raise ValueError("recovery counts must not be negative")

    @property
    def recovered(self) -> int:
        return self.reclaimed_leases + self.resumed_jobs + self.terminal_failures


class SyncWorker(Protocol):
    """Shared shape of the background sync workers driven by a poll loop.

    Implemented by ``IngestionWorker``, ``GenerationWorker``, and
    ``StudioWorker``. Each claims and processes at most one queued job per
    call, returning whether it did any work so the caller can back off when
    idle.
    """

    def run_once(self) -> bool: ...


class RecoverableSyncWorker(SyncWorker, Protocol):
    def recover_interrupted_jobs(self) -> int: ...


class DurableWorker(Protocol):
    def run_once(self) -> WorkResult: ...

    def recover_interrupted(self) -> RecoveryReport: ...


@dataclass(frozen=True, slots=True)
class DurableWorkerAdapter:
    job_type: str
    worker: DurableWorker

    def __post_init__(self) -> None:
        if not self.job_type.strip():
            raise ValueError("job type must not be blank")

    def run_once(self) -> bool:
        return self.worker.run_once().worked

    def recover_interrupted_jobs(self) -> int:
        return self.worker.recover_interrupted().recovered


def adapt_durable_worker(job_type: str, worker: DurableWorker) -> DurableWorkerAdapter:
    return DurableWorkerAdapter(job_type, worker)


def build_worker_registry(
    *,
    ingestion_worker: RecoverableSyncWorker,
    generation_worker: RecoverableSyncWorker,
    studio_worker: RecoverableSyncWorker,
    indexing_worker: RecoverableSyncWorker | None = None,
) -> dict[str, RecoverableSyncWorker]:
    workers = {
        "ingestion_worker": ingestion_worker,
        "generation_worker": generation_worker,
        "studio_worker": studio_worker,
    }
    if indexing_worker is not None:
        workers["indexing_worker"] = indexing_worker
    return workers


__all__ = [
    "DurableWorker",
    "DurableWorkerAdapter",
    "GEMINI_INDEX_JOB_TYPE",
    "RecoverableSyncWorker",
    "RecoveryReport",
    "SyncWorker",
    "WorkResult",
    "adapt_durable_worker",
    "build_worker_registry",
]
