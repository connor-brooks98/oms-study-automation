import sys
from pathlib import Path
from typing import Any


class OfficeUnavailableError(RuntimeError):
    pass


def _force_disable_macros(application: Any) -> None:
    """Best-effort VBA suppression for Office automation."""
    try:
        application.AutomationSecurity = 3
    except Exception:  # noqa: BLE001 - unsupported by some Office applications
        pass


def convert_office_file(source: Path, destination: Path) -> None:
    if sys.platform != "win32":
        raise OfficeUnavailableError(
            "Microsoft Office conversion is available only on Windows"
        )
    import pythoncom  # type: ignore[import-untyped]
    import win32com.client  # type: ignore[import-untyped]

    application: Any = None
    document: Any = None
    pythoncom.CoInitialize()
    try:
        if source.suffix.casefold() in {".ppt", ".pptx"}:
            application = win32com.client.DispatchEx("PowerPoint.Application")
            application.DisplayAlerts = 1
            _force_disable_macros(application)
            document = application.Presentations.Open(
                str(source),
                ReadOnly=True,
                WithWindow=False,
            )
            document.SaveAs(str(destination), 32)
        else:
            application = win32com.client.DispatchEx("Word.Application")
            application.Visible = False
            application.DisplayAlerts = 0
            _force_disable_macros(application)
            document = application.Documents.Open(str(source), ReadOnly=True)
            document.ExportAsFixedFormat(str(destination), 17)
    finally:
        if document is not None:
            document.Close()
        if application is not None:
            application.Quit()
        pythoncom.CoUninitialize()
