import os
import time
from pathlib import Path

import pytest

from oms_hub.files.office import (
    OfficeConversionError,
    OfficeTimeoutError,
    SerialOfficeConverter,
)


def _hang(_source: Path, destination: Path) -> None:
    destination.write_bytes(b"partial")
    time.sleep(30)


def _succeed(_source: Path, destination: Path) -> None:
    destination.write_bytes(b"pdf")


def _exit_without_result(_source: Path, destination: Path) -> None:
    destination.write_bytes(b"partial")
    os._exit(7)


def test_timeout_kills_child_cleans_partial_and_releases_global_lock(tmp_path):
    source = tmp_path / "lecture.pptx"
    destination = tmp_path / "lecture.pdf"
    source.write_bytes(b"pptx")
    converter = SerialOfficeConverter(timeout_seconds=0.1, worker=_hang)

    with pytest.raises(OfficeTimeoutError):
        converter.convert(source, destination)

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


def test_pipe_allocation_failure_releases_global_lock(tmp_path, monkeypatch):
    source = tmp_path / "lecture.pptx"
    destination = tmp_path / "lecture.pdf"
    source.write_bytes(b"pptx")
    converter = SerialOfficeConverter(timeout_seconds=5, worker=_succeed)

    def fail_pipe(*, duplex: bool):
        del duplex
        raise OSError("no handles available")

    monkeypatch.setattr(converter._context, "Pipe", fail_pipe)
    with pytest.raises(OSError, match="no handles available"):
        converter.convert(source, destination)

    monkeypatch.undo()
    converter.convert(source, destination)
    assert destination.read_bytes() == b"pdf"
