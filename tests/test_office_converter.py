import time
from pathlib import Path

import pytest

from oms_hub.files.office import OfficeTimeoutError, SerialOfficeConverter


def _hang(_source: Path, destination: Path) -> None:
    destination.write_bytes(b"partial")
    time.sleep(30)


def _succeed(_source: Path, destination: Path) -> None:
    destination.write_bytes(b"pdf")


def test_timeout_kills_child_cleans_partial_and_releases_instance_lock(tmp_path):
    source = tmp_path / "lecture.pptx"
    destination = tmp_path / "lecture.pdf"
    source.write_bytes(b"pptx")
    converter = SerialOfficeConverter(timeout_seconds=0.1, worker=_hang)

    with pytest.raises(OfficeTimeoutError):
        converter.convert(source, destination)

    assert not destination.exists()
    converter.worker = _succeed
    converter.convert(source, destination)
    assert destination.read_bytes() == b"pdf"
