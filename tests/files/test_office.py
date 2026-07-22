import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from oms_hub.files.office import (
    OfficeConversionError,
    OfficeTimeoutError,
    SerialOfficeConverter,
)


class FakeDocument:
    def __init__(self) -> None:
        self.closed = False

    def SaveAs(self, destination: str, format_id: int) -> None:
        assert format_id == 32
        Path(destination).write_bytes(b"pdf")

    def ExportAsFixedFormat(self, destination: str, format_id: int) -> None:
        assert format_id == 17
        Path(destination).write_bytes(b"pdf")

    def Close(self) -> None:
        self.closed = True


class FakeApplication:
    def __init__(self, document: FakeDocument) -> None:
        self.document = document
        self.quit = False
        self.Presentations = SimpleNamespace(Open=lambda *args, **kwargs: document)
        self.Documents = SimpleNamespace(Open=lambda *args, **kwargs: document)

    def Quit(self) -> None:
        self.quit = True


@pytest.mark.parametrize("suffix,expected", [(".pptx", "PowerPoint.Application"), (".docx", "Word.Application")])
def test_converter_selects_office_application_and_cleans_owned_instance(tmp_path, suffix, expected) -> None:
    document = FakeDocument()
    application = FakeApplication(document)
    calls = []
    converter = SerialOfficeConverter(factory=lambda progid: calls.append(progid) or application)
    source = tmp_path / f"source{suffix}"
    source.write_bytes(b"source")
    destination = tmp_path / "result.pdf"
    converter.convert(source, destination)
    assert calls == [expected]
    assert document.closed is True
    assert application.quit is True
    assert destination.read_bytes() == b"pdf"


def test_converter_rejects_non_office_source(tmp_path) -> None:
    converter = SerialOfficeConverter(factory=lambda progid: None)
    with pytest.raises(OfficeConversionError, match="unsupported"):
        converter.convert(tmp_path / "source.pdf", tmp_path / "result.pdf")


def test_timeout_keeps_serial_lock_until_owned_work_finishes(tmp_path) -> None:
    release = threading.Event()

    class SlowDocument(FakeDocument):
        def SaveAs(self, destination: str, format_id: int) -> None:
            release.wait(1)

    converter = SerialOfficeConverter(timeout_seconds=0, factory=lambda progid: FakeApplication(SlowDocument()))
    source = tmp_path / "source.pptx"
    source.write_bytes(b"source")
    with pytest.raises(OfficeTimeoutError):
        converter.convert(source, tmp_path / "result.pdf")
    with pytest.raises(OfficeConversionError, match="already running"):
        converter.convert(source, tmp_path / "second.pdf")
    release.set()
