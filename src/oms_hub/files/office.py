import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any, Protocol


class OfficeUnavailableError(RuntimeError):
    pass


class OfficeTimeoutError(RuntimeError):
    pass


class OfficeConversionError(RuntimeError):
    pass


class OfficeConverter(Protocol):
    def convert(self, source: Path, destination: Path) -> None: ...


OfficeFactory = Callable[[str], Any]


def _dispatch_factory(progid: str) -> Any:
    if sys.platform != "win32":
        raise OfficeUnavailableError("Microsoft Office conversion is available only on Windows")
    import pythoncom  # type: ignore[import-untyped]
    import win32com.client  # type: ignore[import-untyped]

    pythoncom.CoInitialize()
    return win32com.client.DispatchEx(progid)


class SerialOfficeConverter:
    _lock = threading.Lock()

    def __init__(
        self,
        timeout_seconds: int = 180,
        factory: OfficeFactory = _dispatch_factory,
    ):
        self.timeout_seconds = timeout_seconds
        self.factory = factory
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="oms-office")

    def convert(self, source: Path, destination: Path) -> None:
        suffix = source.suffix.casefold()
        if suffix not in {".ppt", ".pptx", ".doc", ".docx"}:
            raise OfficeConversionError(f"unsupported Office source type: {suffix}")
        if not self._lock.acquire(blocking=False):
            raise OfficeConversionError("another Office conversion is already running")
        destination.parent.mkdir(parents=True, exist_ok=True)
        future = self.executor.submit(self._convert_sync, source, destination)
        future.add_done_callback(lambda _: self._lock.release())
        try:
            future.result(timeout=self.timeout_seconds)
        except FutureTimeoutError as error:
            raise OfficeTimeoutError(
                f"Office conversion exceeded {self.timeout_seconds} seconds"
            ) from error
        except OfficeUnavailableError:
            raise
        except Exception as error:
            raise OfficeConversionError("Microsoft Office could not export the PDF") from error

    def _convert_sync(self, source: Path, destination: Path) -> None:
        application: Any = None
        document: Any = None
        try:
            if source.suffix.casefold() in {".ppt", ".pptx"}:
                application = self.factory("PowerPoint.Application")
                application.DisplayAlerts = 1
                document = application.Presentations.Open(
                    str(source),
                    ReadOnly=True,
                    WithWindow=False,
                )
                document.SaveAs(str(destination), 32)
            else:
                application = self.factory("Word.Application")
                application.Visible = False
                application.DisplayAlerts = 0
                document = application.Documents.Open(str(source), ReadOnly=True)
                document.ExportAsFixedFormat(str(destination), 17)
        finally:
            if document is not None:
                document.Close()
            if application is not None:
                application.Quit()
            if sys.platform == "win32" and self.factory is _dispatch_factory:
                import pythoncom  # type: ignore[import-untyped]

                pythoncom.CoUninitialize()
