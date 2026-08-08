import hashlib
import sys
from pathlib import Path

from pypdf import PdfWriter

from oms_hub.document_processing.domain import SourceSnapshot
from oms_hub.document_processing.pdf_adapter import PdfProcessor
from oms_hub.files.pdf import PdfInspection


def _snapshot(path: Path) -> SourceSnapshot:
    return SourceSnapshot(
        id="source-1",
        title="Questions",
        path=path,
        media_type="application/pdf",
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _blank_pdf(path: Path) -> SourceSnapshot:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as stream:
        writer.write(stream)
    return _snapshot(path)


def test_scanned_pdf_returns_ocr_blocker_instead_of_empty_success(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot = _blank_pdf(tmp_path / "scanned.pdf")
    monkeypatch.setattr(
        "oms_hub.document_processing.pdf_adapter.inspect_pdf",
        lambda _: PdfInspection("scanned", 1.0, 1, (1,), True),
    )

    parsed = PdfProcessor().parse(snapshot, tmp_path / "assets")

    assert parsed.segments == ()
    assert "OCR required for page 1" in parsed.warnings
    assert "PDF contained no extractable text" in parsed.warnings


def test_pdf_processor_retains_page_locators_and_sanitized_raster_images(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot = _blank_pdf(tmp_path / "questions.pdf")
    monkeypatch.setattr(
        "oms_hub.document_processing.pdf_adapter.inspect_pdf",
        lambda _: PdfInspection("text_based", 1.0, 1, (), True),
    )
    monkeypatch.setattr(
        "oms_hub.document_processing.pdf_adapter._page_texts",
        lambda _: ("Question on page one",),
    )
    monkeypatch.setitem(sys.modules, "fitz", _FitzFixture())

    parsed = PdfProcessor().parse(snapshot, tmp_path / "assets")

    assert parsed.segments[0].locator.label == "page 1"
    assert parsed.segments[0].locator.page_number == 1
    assert parsed.assets[0].locator.label == "page 1 image 1"
    assert parsed.assets[0].path is not None and parsed.assets[0].path.is_file()


def test_pdf_processor_keeps_successful_pages_and_marks_failed_page_incomplete(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot = _blank_pdf(tmp_path / "questions.pdf")
    monkeypatch.setattr(
        "oms_hub.document_processing.pdf_adapter.inspect_pdf",
        lambda _: PdfInspection("mixed", 1.0, 3, (), True),
    )
    monkeypatch.setattr(
        "oms_hub.document_processing.pdf_adapter._page_texts",
        lambda _: ("Question one", RuntimeError("bad content stream"), "Question three"),
    )

    parsed = PdfProcessor().parse(snapshot, tmp_path / "assets")

    assert tuple(segment.key for segment in parsed.segments) == ("page-1", "page-3")
    assert parsed.warnings[0].startswith("BLOCKER: PDF parsing incomplete on page 2")


def test_pdf_processor_marks_all_non_ocr_text_failures_as_incomplete(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot = _blank_pdf(tmp_path / "questions.pdf")
    monkeypatch.setattr(
        "oms_hub.document_processing.pdf_adapter.inspect_pdf",
        lambda _: PdfInspection("text_based", 1.0, 1, (), True),
    )
    monkeypatch.setattr(
        "oms_hub.document_processing.pdf_adapter._page_texts",
        lambda _: (RuntimeError("bad content stream"),),
    )

    parsed = PdfProcessor().parse(snapshot, tmp_path / "assets")

    assert parsed.segments == ()
    assert parsed.warnings[0].startswith("BLOCKER: PDF parsing incomplete on page 1")


class _FitzFixture:
    def open(self, _: Path) -> "_Document":
        return _Document()


class _Document:
    def __enter__(self) -> "_Document":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def __iter__(self):
        return iter((_Page(),))

    def close(self) -> None:
        return None


class _Page:
    def get_images(self, *, full: bool) -> tuple[tuple[int], ...]:
        assert full
        return ((7,),)


class _Pixmap:
    def __init__(self, *_: object) -> None:
        pass

    def tobytes(self, _: str) -> bytes:
        from io import BytesIO

        from PIL import Image

        output = BytesIO()
        Image.new("RGB", (2, 2), "green").save(output, format="PNG")
        return output.getvalue()


_FitzFixture.Pixmap = _Pixmap  # type: ignore[attr-defined]
