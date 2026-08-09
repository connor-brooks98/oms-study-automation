from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from oms_hub.files.office_worker import convert_office_file


class OfficeUnavailableError(RuntimeError):
    pass


class OfficeTimeoutError(RuntimeError):
    pass


class OfficeConversionError(RuntimeError):
    pass


class OfficeAdmissionTimeoutError(OfficeConversionError):
    """The bounded wait for the single Office automation slot expired."""

    pass


class OfficeConverter(Protocol):
    def convert(self, source: Path, destination: Path) -> None: ...


OfficeProcessReporter = Callable[[int], None]
OfficeWorker = Callable[[Path, Path, OfficeProcessReporter], None]
OfficeAdmissionReporter = Callable[[str], None]


class SerialOfficeConverter:
    _lock = threading.Lock()

    def __init__(
        self,
        timeout_seconds: float = 180,
        worker: OfficeWorker = convert_office_file,
        admission_timeout_seconds: float | None = None,
        admission_reporter: OfficeAdmissionReporter | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.worker = worker
        self.admission_timeout_seconds = _admission_timeout(admission_timeout_seconds)
        self.admission_reporter = admission_reporter
        self.admission_state = "idle"
        self._context = multiprocessing.get_context("spawn")

    def convert(self, source: Path, destination: Path) -> None:
        suffix = source.suffix.casefold()
        if suffix not in {".ppt", ".pptx", ".doc", ".docx"}:
            raise OfficeConversionError(f"unsupported Office source type: {suffix}")
        deadline = time.monotonic() + self.admission_timeout_seconds
        if not self._lock.acquire(blocking=False):
            self._report_admission("waiting")
            remaining = max(0.0, deadline - time.monotonic())
            if not self._lock.acquire(timeout=remaining):
                raise OfficeAdmissionTimeoutError(
                    "Office conversion admission timed out while waiting for the "
                    "in-process automation slot"
                )
        cross_process_lock: _WindowsOfficeLock | None = None
        try:
            cross_process_lock = _WindowsOfficeLock.acquire_until(
                deadline,
                self._report_admission,
            )
            self._report_admission("admitted")
            destination.parent.mkdir(parents=True, exist_ok=True)
            receiver, sender = self._context.Pipe(duplex=False)
            try:
                process = self._context.Process(
                    target=_run_child,
                    args=(self.worker, source, destination, sender),
                    name="oms-office-conversion",
                    daemon=True,
                )
                try:
                    process.start()
                    sender.close()
                    process.join(self.timeout_seconds)
                    if process.is_alive():
                        messages = _receive_messages(receiver, wait_seconds=0.25)
                        office_pid = _reported_office_pid(messages)
                        if office_pid is not None:
                            _terminate_office_process_tree(office_pid)
                            process.join(5)
                        if process.is_alive():
                            process.terminate()
                            process.join(5)
                        if process.is_alive():
                            process.kill()
                            process.join(5)
                        destination.unlink(missing_ok=True)
                        raise OfficeTimeoutError(
                            f"Office conversion exceeded {self.timeout_seconds:g} seconds"
                        )
                    try:
                        messages = _receive_messages(receiver)
                    except (EOFError, OSError):
                        messages = []
                    result = next(
                        (
                            message
                            for message in reversed(messages)
                            if message[0] != "office_pid"
                        ),
                        None,
                    )
                    if result == ("ok", "") and process.exitcode == 0:
                        return
                    destination.unlink(missing_ok=True)
                    error_type, detail = result or ("OfficeConversionError", "")
                    if error_type == "OfficeUnavailableError":
                        raise OfficeUnavailableError(
                            "Microsoft Office conversion is available only on Windows"
                        )
                    message = "Microsoft Office could not export the PDF"
                    concise_detail = " ".join(detail.split())
                    if concise_detail:
                        message = f"{message} ({error_type}: {concise_detail})"
                    raise OfficeConversionError(
                        message[:1000]
                    )
                finally:
                    if process.is_alive():
                        process.kill()
                        process.join(5)
            finally:
                receiver.close()
                sender.close()
        except OSError as error:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            raise OfficeConversionError(
                "Microsoft Office conversion process could not start"
            ) from error
        finally:
            if cross_process_lock is not None:
                cross_process_lock.release()
            self._lock.release()

    def _report_admission(self, state: str) -> None:
        self.admission_state = state
        if self.admission_reporter is None:
            return
        try:
            self.admission_reporter(state)
        except Exception:
            # Observability cannot alter an Office conversion outcome.
            pass


def _admission_timeout(value: float | None) -> float:
    configured = value
    if configured is None:
        configured = float(os.environ.get("OMS_HUB_OFFICE_ADMISSION_TIMEOUT_SECONDS", "120"))
    if configured <= 0:
        raise ValueError("Office admission timeout must be greater than zero")
    return configured


class _WindowsOfficeLock:
    """A process-wide lock for Windows Office automation only.

    Windows runs obtain a one-byte advisory lock in the shared temp directory.
    Other platforms retain only the class-level thread lock so macOS tests do
    not depend on Windows locking APIs.
    """

    def __init__(self, stream: BinaryIO | None) -> None:
        self._stream = stream

    @classmethod
    def acquire_until(
        cls,
        deadline: float,
        report_admission: OfficeAdmissionReporter | None = None,
    ) -> _WindowsOfficeLock:
        if sys.platform != "win32":
            return cls(None)
        import msvcrt
        import tempfile

        windows_msvcrt: Any = msvcrt

        lock_path = Path(tempfile.gettempdir()) / "oms-study-hub-office.lock"
        stream = lock_path.open("a+b")
        stream.seek(0)
        stream.write(b"\0")
        stream.flush()
        waiting_reported = False
        while True:
            try:
                stream.seek(0)
                windows_msvcrt.locking(
                    stream.fileno(),
                    windows_msvcrt.LK_NBLCK,
                    1,
                )
                return cls(stream)
            except OSError as error:
                if not waiting_reported and report_admission is not None:
                    report_admission("waiting")
                    waiting_reported = True
                if time.monotonic() >= deadline:
                    stream.close()
                    raise OfficeAdmissionTimeoutError(
                        "Office conversion admission timed out while waiting for "
                        "the cross-process automation slot"
                    ) from error
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    def release(self) -> None:
        if self._stream is None:
            return
        import msvcrt

        windows_msvcrt: Any = msvcrt
        stream = self._stream
        try:
            stream.seek(0)
            windows_msvcrt.locking(
                stream.fileno(),
                windows_msvcrt.LK_UNLCK,
                1,
            )
        finally:
            stream.close()


def _run_child(
    worker: OfficeWorker,
    source: Path,
    destination: Path,
    sender: Connection,
) -> None:
    try:
        worker(
            source,
            destination,
            lambda pid: sender.send(("office_pid", str(pid))),
        )
    except Exception as error:  # noqa: BLE001 - serialized child boundary
        sender.send((type(error).__name__, str(error)[:500]))
    else:
        sender.send(("ok", ""))
    finally:
        sender.close()


def _receive_messages(
    receiver: Connection,
    *,
    wait_seconds: float = 0,
) -> list[tuple[str, str]]:
    messages: list[tuple[str, str]] = []
    first = True
    while True:
        try:
            if not receiver.poll(wait_seconds if first else 0):
                break
            first = False
            messages.append(receiver.recv())
        except (EOFError, OSError):
            break
    return messages


def _reported_office_pid(messages: list[tuple[str, str]]) -> int | None:
    for message_type, value in reversed(messages):
        if message_type == "office_pid":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _terminate_office_process_tree(process_id: int) -> None:
    if sys.platform != "win32":
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(process_id), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
