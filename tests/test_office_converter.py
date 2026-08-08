import os
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

import oms_hub.files.office as office_module
from oms_hub.files import office_worker
from oms_hub.files.office import (
    OfficeConversionError,
    OfficeTimeoutError,
    SerialOfficeConverter,
)


def _hang(_source: Path, destination: Path, report_process) -> None:
    report_process(4242)
    destination.write_bytes(b"partial")
    time.sleep(30)


def _hang_without_process_id(
    _source: Path,
    destination: Path,
    _report_process,
) -> None:
    destination.write_bytes(b"partial")
    time.sleep(30)


def _succeed(_source: Path, destination: Path, _report_process) -> None:
    destination.write_bytes(b"pdf")


def _exit_without_result(_source: Path, destination: Path, _report_process) -> None:
    destination.write_bytes(b"partial")
    os._exit(7)


def _fail_with_detail(_source: Path, _destination: Path, _report_process) -> None:
    raise TypeError("PowerPoint HWND was rejected\nby pywin32")


def test_office_window_pid_coerces_integer_hwnd_to_pyhandle():
    converted: list[int] = []
    detached: list[int] = []

    class FakeHandle:
        def __init__(self, value: int) -> None:
            self.value = value

        def Detach(self) -> int:
            detached.append(self.value)
            return self.value

    class FakePyWinTypes:
        @staticmethod
        def HANDLE(value: int) -> object:
            converted.append(value)
            return FakeHandle(value)

    class FakeWin32Process:
        @staticmethod
        def GetWindowThreadProcessId(handle: object) -> tuple[int, int]:
            assert isinstance(handle, FakeHandle)
            assert handle.value == 987654
            return (123, 4242)

    assert (
        office_worker._process_id_for_window(
            987654,
            FakeWin32Process,
            FakePyWinTypes,
        )
        == 4242
    )
    assert converted == [987654]
    assert detached == [987654]


def test_office_window_pid_calls_method_style_hwnd_accessor():
    calls: list[str] = []

    class FakeHandle:
        def __init__(self, value: int) -> None:
            self.value = value

        def Detach(self) -> int:
            return self.value

    class FakePyWinTypes:
        @staticmethod
        def HANDLE(value: int) -> object:
            return FakeHandle(value)

    class FakeWin32Process:
        @staticmethod
        def GetWindowThreadProcessId(handle: object) -> tuple[int, int]:
            assert isinstance(handle, FakeHandle)
            assert handle.value == 987654
            return (123, 4242)

    def get_hwnd() -> int:
        calls.append("called")
        return 987654

    assert (
        office_worker._process_id_for_window(
            get_hwnd,
            FakeWin32Process,
            FakePyWinTypes,
        )
        == 4242
    )
    assert calls == ["called"]


def test_office_window_handle_uses_explicit_property_get_after_member_not_found():
    class MemberNotFoundError(Exception):
        hresult = -2147352573

    class FakePythonCom:
        DISP_E_MEMBERNOTFOUND = -2147352573
        INVOKE_PROPERTYGET = 2

    class FakeOleObject:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def GetIDsOfNames(self, lcid: int, name: str) -> int:
            self.calls.append(("GetIDsOfNames", lcid, name))
            return 42

        def Invoke(
            self,
            dispid: int,
            lcid: int,
            invoke_type: int,
            result_wanted: int,
        ) -> int:
            self.calls.append(
                ("Invoke", dispid, lcid, invoke_type, result_wanted)
            )
            return 987654

    class FakePowerPoint:
        def __init__(self) -> None:
            self._oleobj_ = FakeOleObject()

        @staticmethod
        def HWND() -> int:
            raise MemberNotFoundError("Member not found.")

    application = FakePowerPoint()

    assert (
        office_worker._window_handle_for_process_id(
            application,
            "HWND",
            FakePythonCom,
        )
        == 987654
    )
    assert application._oleobj_.calls == [
        ("GetIDsOfNames", 0, "HWND"),
        ("Invoke", 42, 0, FakePythonCom.INVOKE_PROPERTYGET, 1),
    ]


def test_office_window_handle_reraises_non_member_not_found_errors():
    class FakePythonCom:
        DISP_E_MEMBERNOTFOUND = -2147352573

    class FakePowerPoint:
        @staticmethod
        def HWND() -> int:
            raise TypeError("unexpected COM wrapper failure")

    with pytest.raises(TypeError, match="unexpected COM wrapper failure"):
        office_worker._window_handle_for_process_id(
            FakePowerPoint(),
            "HWND",
            FakePythonCom,
        )


def test_powerpoint_conversion_does_not_probe_hwnd(monkeypatch, tmp_path):
    calls: list[tuple[object, ...]] = []

    class FakeDocument:
        def SaveAs(self, destination: str, file_type: int) -> None:
            calls.append(("SaveAs", destination, file_type))

        def Close(self) -> None:
            calls.append(("Close",))

    class FakePresentations:
        @staticmethod
        def Open(
            source: str,
            *,
            ReadOnly: bool,
            WithWindow: bool,
        ) -> FakeDocument:
            calls.append(("Open", source, ReadOnly, WithWindow))
            return FakeDocument()

    class FakePowerPoint:
        Presentations = FakePresentations()
        DisplayAlerts = 0
        AutomationSecurity = 0

        def __getattr__(self, name: str) -> object:
            if name == "HWND":
                raise AssertionError("PowerPoint HWND must not be accessed")
            raise AttributeError(name)

        def Quit(self) -> None:
            calls.append(("Quit",))

    pythoncom = ModuleType("pythoncom")
    pythoncom.CoInitialize = lambda: calls.append(("CoInitialize",))
    pythoncom.CoUninitialize = lambda: calls.append(("CoUninitialize",))
    win32com = ModuleType("win32com")
    client = ModuleType("win32com.client")
    client.DispatchEx = lambda progid: (
        calls.append(("DispatchEx", progid)) or FakePowerPoint()
    )
    win32com.client = client

    monkeypatch.setattr(office_worker.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", client)

    source = tmp_path / "lecture.pptx"
    destination = tmp_path / "lecture.pdf"
    reported: list[int] = []

    office_worker.convert_office_file(source, destination, reported.append)

    assert reported == []
    assert calls == [
        ("CoInitialize",),
        ("DispatchEx", "PowerPoint.Application"),
        ("Open", str(source), True, False),
        ("SaveAs", str(destination), 32),
        ("Close",),
        ("Quit",),
        ("CoUninitialize",),
    ]


def test_office_window_pid_detaches_borrowed_hwnd_after_lookup_failure():
    detached: list[int] = []

    class FakeHandle:
        def Detach(self) -> int:
            detached.append(987654)
            return 987654

    class FakePyWinTypes:
        @staticmethod
        def HANDLE(_value: int) -> object:
            return FakeHandle()

    class FakeWin32Process:
        @staticmethod
        def GetWindowThreadProcessId(_handle: object) -> tuple[int, int]:
            raise TypeError("lookup failed")

    with pytest.raises(TypeError, match="lookup failed"):
        office_worker._process_id_for_window(
            987654,
            FakeWin32Process,
            FakePyWinTypes,
        )

    assert detached == [987654]


@pytest.mark.skipif(sys.platform != "win32", reason="requires pywin32")
def test_windows_pyhandle_accepts_office_integer_hwnd():
    import pywintypes  # type: ignore[import-untyped]

    class FakeWin32Process:
        @staticmethod
        def GetWindowThreadProcessId(handle: object) -> tuple[int, int]:
            assert int(handle) == 987654
            return (123, 4242)

    assert (
        office_worker._process_id_for_window(
            987654,
            FakeWin32Process,
            pywintypes,
        )
        == 4242
    )


def test_timeout_kills_owned_office_tree_cleans_partial_and_releases_lock(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "lecture.pptx"
    destination = tmp_path / "lecture.pdf"
    source.write_bytes(b"pptx")
    converter = SerialOfficeConverter(timeout_seconds=0.1, worker=_hang)
    terminated: list[int] = []
    monkeypatch.setattr(
        office_module,
        "_terminate_office_process_tree",
        terminated.append,
    )

    with pytest.raises(OfficeTimeoutError):
        converter.convert(source, destination)

    assert terminated == [4242]
    assert not destination.exists()
    converter.worker = _succeed
    converter.timeout_seconds = 5
    converter.convert(source, destination)
    assert destination.read_bytes() == b"pdf"


def test_timeout_without_office_pid_cleans_partial_and_releases_lock(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "lecture.pptx"
    destination = tmp_path / "lecture.pdf"
    source.write_bytes(b"pptx")
    converter = SerialOfficeConverter(
        timeout_seconds=0.1,
        worker=_hang_without_process_id,
    )
    terminated: list[int] = []
    monkeypatch.setattr(
        office_module,
        "_terminate_office_process_tree",
        terminated.append,
    )

    with pytest.raises(OfficeTimeoutError):
        converter.convert(source, destination)

    assert terminated == []
    assert not destination.exists()
    converter.worker = _succeed
    converter.timeout_seconds = 5
    converter.convert(source, destination)
    assert destination.read_bytes() == b"pdf"


def test_child_exit_without_result_is_retryable_and_cleans_partial(tmp_path):
    source = tmp_path / "lecture.pptx"
    destination = tmp_path / "lecture.pdf"
    source.write_bytes(b"pptx")
    converter = SerialOfficeConverter(timeout_seconds=5, worker=_exit_without_result)

    with pytest.raises(OfficeConversionError):
        converter.convert(source, destination)

    assert not destination.exists()
    converter.worker = _succeed
    converter.convert(source, destination)
    assert destination.read_bytes() == b"pdf"


def test_child_error_detail_is_preserved_for_job_diagnostics(tmp_path):
    source = tmp_path / "lecture.pptx"
    destination = tmp_path / "lecture.pdf"
    source.write_bytes(b"pptx")
    converter = SerialOfficeConverter(timeout_seconds=5, worker=_fail_with_detail)

    with pytest.raises(OfficeConversionError) as error:
        converter.convert(source, destination)

    assert str(error.value) == (
        "Microsoft Office could not export the PDF "
        "(TypeError: PowerPoint HWND was rejected by pywin32)"
    )
    assert not destination.exists()


def test_office_process_tree_termination_uses_windows_taskkill(monkeypatch):
    calls: list[tuple[list[str], dict[str, object]]] = []

    def record_run(command, **kwargs):
        calls.append((command, kwargs))

    monkeypatch.setattr(office_module.sys, "platform", "win32")
    monkeypatch.setattr(office_module.subprocess, "run", record_run)

    office_module._terminate_office_process_tree(4242)

    assert calls == [
        (
            ["taskkill", "/PID", "4242", "/T", "/F"],
            {
                "check": False,
                "stdout": office_module.subprocess.DEVNULL,
                "stderr": office_module.subprocess.DEVNULL,
                "timeout": 15,
                "creationflags": getattr(
                    office_module.subprocess,
                    "CREATE_NO_WINDOW",
                    0,
                ),
            },
        )
    ]


def test_pipe_allocation_failure_releases_global_lock(tmp_path, monkeypatch):
    source = tmp_path / "lecture.pptx"
    destination = tmp_path / "lecture.pdf"
    source.write_bytes(b"pptx")
    converter = SerialOfficeConverter(timeout_seconds=5, worker=_succeed)

    def fail_pipe(*, duplex: bool):
        del duplex
        raise OSError("no handles available")

    monkeypatch.setattr(converter._context, "Pipe", fail_pipe)
    with pytest.raises(OfficeConversionError) as error:
        converter.convert(source, destination)
    assert isinstance(error.value.__cause__, OSError)
    assert "no handles available" in str(error.value.__cause__)

    monkeypatch.undo()
    converter.convert(source, destination)
    assert destination.read_bytes() == b"pdf"


def test_process_start_failure_is_retryable_and_releases_global_lock(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "lecture.pptx"
    destination = tmp_path / "lecture.pdf"
    source.write_bytes(b"pptx")
    converter = SerialOfficeConverter(timeout_seconds=5, worker=_succeed)

    class StartFailureProcess:
        def start(self) -> None:
            raise OSError("process resources unavailable")

        def is_alive(self) -> bool:
            return False

    monkeypatch.setattr(
        converter._context,
        "Process",
        lambda **_kwargs: StartFailureProcess(),
    )
    with pytest.raises(OfficeConversionError) as error:
        converter.convert(source, destination)
    assert isinstance(error.value.__cause__, OSError)
    assert "process resources unavailable" in str(error.value.__cause__)
    assert not destination.exists()

    monkeypatch.undo()
    converter.convert(source, destination)
    assert destination.read_bytes() == b"pdf"
