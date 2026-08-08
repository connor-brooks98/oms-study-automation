import multiprocessing
import subprocess
import sys
import threading
from collections.abc import Callable
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Protocol

from oms_hub.files.office_worker import convert_office_file


class OfficeUnavailableError(RuntimeError):
    pass


class OfficeTimeoutError(RuntimeError):
    pass


class OfficeConversionError(RuntimeError):
    pass


class OfficeConverter(Protocol):
    def convert(self, source: Path, destination: Path) -> None: ...


OfficeProcessReporter = Callable[[int], None]
OfficeWorker = Callable[[Path, Path, OfficeProcessReporter], None]


class SerialOfficeConverter:
    _lock = threading.Lock()

    def __init__(
        self,
        timeout_seconds: float = 180,
        worker: OfficeWorker = convert_office_file,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.worker = worker
        self._context = multiprocessing.get_context("spawn")

    def convert(self, source: Path, destination: Path) -> None:
        suffix = source.suffix.casefold()
        if suffix not in {".ppt", ".pptx", ".doc", ".docx"}:
            raise OfficeConversionError(f"unsupported Office source type: {suffix}")
        if not self._lock.acquire(blocking=False):
            raise OfficeConversionError("another Office conversion is already running")
        try:
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
                    error_type, _detail = result or ("OfficeConversionError", "")
                    if error_type == "OfficeUnavailableError":
                        raise OfficeUnavailableError(
                            "Microsoft Office conversion is available only on Windows"
                        )
                    raise OfficeConversionError(
                        "Microsoft Office could not export the PDF"
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
            self._lock.release()


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
