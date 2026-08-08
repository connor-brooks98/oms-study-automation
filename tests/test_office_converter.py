import os
import time
from pathlib import Path

import pytest

import oms_hub.files.office as office_module
from oms_hub.files.office import (
    OfficeConversionError,
    OfficeTimeoutError,
    SerialOfficeConverter,
)


def _hang(_source: Path, destination: Path, report_process) -> None:
    report_process(4242)
    destination.write_bytes(b"partial")
    time.sleep(30)


def _succeed(_source: Path, destination: Path, _report_process) -> None:
    destination.write_bytes(b"pdf")


def _exit_without_result(_source: Path, destination: Path, _report_process) -> None:
    destination.write_bytes(b"partial")
    os._exit(7)


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
