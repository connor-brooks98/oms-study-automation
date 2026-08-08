import multiprocessing
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


OfficeWorker = Callable[[Path, Path], None]


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
        destination.parent.mkdir(parents=True, exist_ok=True)
        receiver, sender = self._context.Pipe(duplex=False)
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
                result = receiver.recv() if receiver.poll() else None
            except EOFError:
                result = None
            if result == ("ok", "") and process.exitcode == 0:
                return
            destination.unlink(missing_ok=True)
            error_type, _detail = result or ("OfficeConversionError", "")
            if error_type == "OfficeUnavailableError":
                raise OfficeUnavailableError(
                    "Microsoft Office conversion is available only on Windows"
                )
            raise OfficeConversionError("Microsoft Office could not export the PDF")
        finally:
            receiver.close()
            sender.close()
            if process.is_alive():
                process.kill()
                process.join(5)
            self._lock.release()


def _run_child(
    worker: OfficeWorker,
    source: Path,
    destination: Path,
    sender: Connection,
) -> None:
    try:
        worker(source, destination)
    except Exception as error:  # noqa: BLE001 - serialized child boundary
        sender.send((type(error).__name__, str(error)[:500]))
    else:
        sender.send(("ok", ""))
    finally:
        sender.close()
