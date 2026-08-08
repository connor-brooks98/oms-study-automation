from typing import Protocol


class SyncWorker(Protocol):
    """Shared shape of the background sync workers driven by a poll loop.

    Implemented by ``IngestionWorker``, ``GenerationWorker``, and
    ``StudioWorker``. Each claims and processes at most one queued job per
    call, returning whether it did any work so the caller can back off when
    idle.
    """

    def run_once(self) -> bool: ...
