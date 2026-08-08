import sys
from collections.abc import Callable
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


def _process_id_for_window(
    window_handle: Any,
    win32process: Any,
    pywintypes: Any,
) -> int:
    """Resolve an Office HWND exposed as either a property or COM method."""
    raw_handle = window_handle() if callable(window_handle) else window_handle
    handle = pywintypes.HANDLE(int(raw_handle))
    try:
        _, process_id = win32process.GetWindowThreadProcessId(handle)
        return int(process_id)
    finally:
        # HWND is borrowed from Office.  PyHANDLE normally calls CloseHandle,
        # which is invalid for a USER handle that this process does not own.
        handle.Detach()


def _is_member_not_found(error: Exception, pythoncom: Any) -> bool:
    """Return whether a COM call failed because the invoked member was absent."""
    hresult = getattr(error, "hresult", None)
    if hresult is None and error.args:
        hresult = error.args[0]
    return hresult == getattr(pythoncom, "DISP_E_MEMBERNOTFOUND", -2147352573)


def _window_handle_for_process_id(
    application: Any,
    property_name: str,
    pythoncom: Any,
) -> Any:
    """Read an Office HWND, correcting a generated wrapper's bad invoke kind."""
    window_handle = getattr(application, property_name)
    if not callable(window_handle):
        return window_handle

    try:
        return window_handle()
    except Exception as error:  # noqa: BLE001 - preserve non-COM failures below
        if not _is_member_not_found(error, pythoncom):
            raise

    # Some generated Office wrappers expose HWND as a method even though the
    # server only accepts a property get. Resolve the DISPID on the underlying
    # IDispatch, then invoke that one member explicitly as a property get.
    ole_object = application._oleobj_
    dispid = ole_object.GetIDsOfNames(0, property_name)
    return ole_object.Invoke(dispid, 0, pythoncom.INVOKE_PROPERTYGET, 1)


def convert_office_file(
    source: Path,
    destination: Path,
    report_process: Callable[[int], None],
) -> None:
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
            # PowerPoint's HWND member is unavailable in some automation
            # contexts.  The known-working conversion path does not require it.
            document = application.Presentations.Open(
                str(source),
                ReadOnly=True,
                WithWindow=False,
            )
            document.SaveAs(str(destination), 32)
        else:
            import pywintypes  # type: ignore[import-untyped]
            import win32process  # type: ignore[import-untyped]

            application = win32com.client.DispatchEx("Word.Application")
            application.Visible = False
            application.DisplayAlerts = 0
            _force_disable_macros(application)
            process_id = _process_id_for_window(
                _window_handle_for_process_id(application, "Hwnd", pythoncom),
                win32process,
                pywintypes,
            )
            report_process(process_id)
            document = application.Documents.Open(str(source), ReadOnly=True)
            document.ExportAsFixedFormat(str(destination), 17)
    finally:
        if document is not None:
            document.Close()
        if application is not None:
            application.Quit()
        pythoncom.CoUninitialize()
