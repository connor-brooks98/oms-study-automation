"""Observable ownership for the non-Anki synchronous worker fleet."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import monotonic
from typing import Protocol


class RecoverableWorker(Protocol):
    def recover_interrupted_jobs(self) -> int: ...

    def run_once(self) -> bool: ...


@dataclass(slots=True)
class _WorkerState:
    name: str
    worker: RecoverableWorker
    thread: threading.Thread | None = None
    start_count: int = 0
    heartbeat_at: float | None = None
    last_work_at: float | None = None
    active_started_at: float | None = None
    recovery_error: str | None = None
    current_error: str | None = None
    last_error: str | None = None
    maintenance: Callable[[], int] | None = None
    maintenance_runs: int = 0
    maintenance_at: float | None = None
    maintenance_removed: int = 0
    maintenance_error: str | None = None


class WorkerSupervisor:
    """The single lifecycle owner for synchronous non-Anki workers."""

    def __init__(
        self,
        workers: Mapping[str, RecoverableWorker],
        *,
        heartbeat_timeout_seconds: float = 30.0,
        active_work_timeout_seconds: float = 900.0,
        maintenance_tasks: Mapping[str, Callable[[], int]] | None = None,
        maintenance_interval_seconds: float = 300.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if active_work_timeout_seconds <= 0:
            raise ValueError("active_work_timeout_seconds must be positive")
        if maintenance_interval_seconds <= 0:
            raise ValueError("maintenance_interval_seconds must be positive")
        unknown_maintenance = set(maintenance_tasks or {}) - set(workers)
        if unknown_maintenance:
            raise ValueError("maintenance task must belong to a configured worker")
        self._workers = {
            name: _WorkerState(
                name,
                worker,
                maintenance=(maintenance_tasks or {}).get(name),
            )
            for name, worker in sorted(workers.items())
        }
        self._heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self._active_work_timeout_seconds = active_work_timeout_seconds
        self._maintenance_interval_seconds = maintenance_interval_seconds
        self._clock = clock
        self._stop = threading.Event()
        self._quiesce = threading.Event()
        self._lock = threading.RLock()

    def start(self) -> None:
        """Recover once, then start exactly one thread for every expected worker."""
        with self._lock:
            if any(
                state.thread is not None and state.thread.is_alive()
                for state in self._workers.values()
            ):
                return
            self._stop.clear()
            for state in self._workers.values():
                try:
                    state.worker.recover_interrupted_jobs()
                except Exception as error:  # noqa: BLE001 - health records a safe boundary
                    state.recovery_error = type(error).__name__
                    state.last_error = state.recovery_error
                    logging.getLogger(__name__).exception(
                        "%s worker recovery failed", state.name
                    )
                else:
                    state.recovery_error = None
                if state.maintenance is not None:
                    self._run_maintenance(state)
                state.heartbeat_at = self._clock()
                state.thread = threading.Thread(
                    target=self._run,
                    args=(state,),
                    name=f"oms-{state.name}",
                    daemon=True,
                )
                state.start_count += 1
                state.thread.start()

    def stop(self, timeout_seconds: float = 10.0) -> None:
        self._stop.set()
        deadline = self._clock() + timeout_seconds
        for state in self._workers.values():
            if state.thread is not None:
                remaining = max(0.0, deadline - self._clock())
                if remaining == 0:
                    break
                state.thread.join(timeout=remaining)

    @property
    def is_quiesced(self) -> bool:
        return self._quiesce.is_set()

    def quiesce(self, timeout_seconds: float = 10.0) -> bool:
        """Prevent new cycles and wait for all current non-Anki work to finish."""
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        with self._lock:
            self._quiesce.set()
        deadline = monotonic() + timeout_seconds
        while True:
            with self._lock:
                if all(state.active_started_at is None for state in self._workers.values()):
                    return True
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            self._stop.wait(min(0.05, remaining))

    def resume(self) -> None:
        """Resume worker cycles after an armed gate expires or is rejected."""
        with self._lock:
            self._quiesce.clear()

    def snapshot(self) -> dict[str, dict[str, object]]:
        now = self._clock()
        with self._lock:
            return {
                name: {
                    "name": state.name,
                    "thread_id": state.thread.ident if state.thread is not None else None,
                    "start_count": state.start_count,
                    "alive": bool(state.thread is not None and state.thread.is_alive()),
                    "heartbeat_age_seconds": _age(now, state.heartbeat_at),
                    "last_work_age_seconds": _age(now, state.last_work_at),
                    "active_work_age_seconds": _age(now, state.active_started_at),
                    "active_work_timeout_seconds": self._active_work_timeout_seconds,
                    "recovery_error": state.recovery_error,
                    "current_error": state.current_error,
                    "last_error": state.last_error,
                    "maintenance_runs": state.maintenance_runs,
                    "maintenance_age_seconds": _age(now, state.maintenance_at),
                    "maintenance_removed": state.maintenance_removed,
                    "maintenance_error": state.maintenance_error,
                }
                for name, state in self._workers.items()
            }

    def ready(self) -> tuple[bool, str | None]:
        now = self._clock()
        with self._lock:
            expected_workers = {
                "generation_worker",
                "ingestion_worker",
                "studio_worker",
            }
            if expected_workers - set(self._workers):
                return False, "worker_missing"
            if set(self._workers) - (expected_workers | {"indexing_worker"}):
                return False, "worker_configuration"
            for state in self._workers.values():
                if state.start_count == 0:
                    return False, "worker_not_started"
                if state.start_count > 1:
                    return False, "worker_duplicate"
                if state.thread is None or not state.thread.is_alive():
                    return False, "worker_dead"
                if state.recovery_error is not None:
                    return False, "worker_recovery_error"
                if state.current_error is not None:
                    return False, "worker_error"
                if state.maintenance_error is not None:
                    return False, "worker_maintenance_error"
                if (
                    state.active_started_at is not None
                    and now - state.active_started_at > self._active_work_timeout_seconds
                ):
                    return False, "worker_active_timeout"
                if (
                    state.active_started_at is None
                    and (
                        state.heartbeat_at is None
                        or now - state.heartbeat_at > self._heartbeat_timeout_seconds
                    )
                ):
                    return False, "worker_stale"
        return True, None

    def _run(self, state: _WorkerState) -> None:
        while not self._stop.is_set():
            if self._quiesce.is_set():
                with self._lock:
                    state.heartbeat_at = self._clock()
                self._stop.wait(0.1)
                continue
            worked = self._run_cycle(state)
            if worked:
                with self._lock:
                    state.last_work_at = self._clock()
            self._stop.wait(0.5 if worked else 5.0)

    def _run_cycle(self, state: _WorkerState) -> bool:
        with self._lock:
            if self._quiesce.is_set():
                return False
            state.active_started_at = self._clock()
        maintenance_worked = False
        try:
            if (
                state.maintenance is not None
                and (
                    state.maintenance_at is None
                    or self._clock() - state.maintenance_at
                    >= self._maintenance_interval_seconds
                )
            ):
                maintenance_worked = self._run_maintenance(state) > 0
            worked = state.worker.run_once()
        except Exception as error:  # noqa: BLE001 - health records a safe boundary
            error_name = type(error).__name__
            with self._lock:
                state.current_error = error_name
                state.last_error = error_name
            logging.getLogger(__name__).exception("%s worker failed", state.name)
            worked = False
        else:
            with self._lock:
                state.current_error = None
        finally:
            with self._lock:
                state.active_started_at = None
                state.heartbeat_at = self._clock()
        return worked or maintenance_worked

    def _run_maintenance(self, state: _WorkerState) -> int:
        assert state.maintenance is not None
        removed = 0
        try:
            removed = state.maintenance()
        except Exception as error:  # noqa: BLE001 - readiness exposes safe telemetry
            error_name = type(error).__name__
            with self._lock:
                state.maintenance_runs += 1
                state.maintenance_at = self._clock()
                state.maintenance_removed = 0
                state.maintenance_error = error_name
                state.last_error = error_name
            logging.getLogger(__name__).exception(
                "%s maintenance failed", state.name
            )
            return 0
        with self._lock:
            state.maintenance_runs += 1
            state.maintenance_at = self._clock()
            state.maintenance_removed = removed
            state.maintenance_error = None
        return removed


def configure_application_logging(data_dir: Path) -> Path:
    """Configure one bounded file sink and one console sink for application logs."""
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "oms-study-hub.log"
    application_logger = logging.getLogger("oms_hub")
    application_logger.setLevel(logging.INFO)
    # Keep child records visible to process-level observers such as Uvicorn and
    # pytest's capture handler.  The local console sink is filtered whenever
    # the root logger already owns a console sink, preventing duplicate output.
    application_logger.propagate = True
    for handler in tuple(application_logger.handlers):
        if handler.get_name().startswith("oms-hub-"):
            application_logger.removeHandler(handler)
            handler.close()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.set_name("oms-hub-file")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.set_name("oms-hub-console")
    console_handler.setFormatter(formatter)
    console_handler.addFilter(_ConsoleFallbackFilter())
    application_logger.addHandler(file_handler)
    application_logger.addHandler(console_handler)
    return log_path


class _ConsoleFallbackFilter(logging.Filter):
    """Emit locally only when the process root has no non-file stream sink."""

    def filter(self, record: logging.LogRecord) -> bool:
        del record
        return not any(
            isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, logging.FileHandler)
            for handler in logging.getLogger().handlers
        )


def _age(now: float, then: float | None) -> float | None:
    if then is None:
        return None
    return round(max(0.0, now - then), 3)
