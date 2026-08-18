import hashlib
import sys
from pathlib import Path

from pypdf import PdfWriter

from oms_hub.document_processing.domain import SourceSnapshot
from oms_hub.document_processing.ocr import LocalOcr
from oms_hub.document_processing.pdf_adapter import PdfProcessor
from oms_hub.files.pdf import PdfInspection, inspect_pdf


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


def _blank_pdf_pages(path: Path, count: int) -> SourceSnapshot:
    writer = PdfWriter()
    for _ in range(count):
        writer.add_blank_page(width=72, height=72)
    with path.open("wb") as stream:
        writer.write(stream)
    return _snapshot(path)


def test_pdf_inspection_fallback_reads_pages_while_open_and_uses_one_based_numbers(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot = _blank_pdf(tmp_path / "fallback-scanned.pdf")
    monkeypatch.setitem(sys.modules, "pdf_inspector", None)

    inspection = inspect_pdf(snapshot.path)

    assert inspection.pdf_type == "scanned"
    assert inspection.pages_needing_ocr == (1,)
    assert inspection.used_pdf_inspector is False


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


def test_pdf_processor_adds_injected_ocr_for_required_page(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot = _blank_pdf(tmp_path / "ocr.pdf")
    monkeypatch.setattr(
        "oms_hub.document_processing.pdf_adapter.inspect_pdf",
        lambda _: PdfInspection("scanned", 1.0, 1, (1,), True),
    )
    monkeypatch.setattr(
        "oms_hub.document_processing.pdf_adapter._page_texts",
        lambda _: ("page footer",),
    )
    monkeypatch.setitem(sys.modules, "fitz", _FitzFixture())

    parsed = PdfProcessor(LocalOcr(lambda _path: "Scanned question text")).parse(
        snapshot, tmp_path / "assets"
    )

    assert tuple(segment.key for segment in parsed.segments) == ("page-1", "page-1-ocr")
    assert parsed.segments[1].text == "Scanned question text"
    assert parsed.segments[1].locator.page_number == 1
    assert all(not warning.startswith("BLOCKER:") for warning in parsed.warnings)


def test_pdf_processor_marks_required_page_when_injected_ocr_is_empty(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot = _blank_pdf(tmp_path / "unreadable.pdf")
    monkeypatch.setattr(
        "oms_hub.document_processing.pdf_adapter.inspect_pdf",
        lambda _: PdfInspection("scanned", 1.0, 1, (1,), True),
    )

    parsed = PdfProcessor(LocalOcr(lambda _path: "")).parse(snapshot, tmp_path / "assets")

    assert tuple(warning for warning in parsed.warnings if warning.startswith("BLOCKER:")) == (
        "BLOCKER: OCR is required but unavailable or empty for page 1",
    )


def test_pdf_processor_keeps_ocr_in_deterministic_page_order(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot = _blank_pdf_pages(tmp_path / "ordered.pdf", 2)
    monkeypatch.setattr(
        "oms_hub.document_processing.pdf_adapter.inspect_pdf",
        lambda _: PdfInspection("mixed", 1.0, 2, (1,), True),
    )
    monkeypatch.setattr(
        "oms_hub.document_processing.pdf_adapter._page_texts",
        lambda _: ("page one", "page two"),
    )
    monkeypatch.setitem(sys.modules, "fitz", _FitzFixture(2))

    parsed = PdfProcessor(LocalOcr(lambda _path: "page one screenshot")).parse(
        snapshot, tmp_path / "assets"
    )

    assert tuple(segment.key for segment in parsed.segments) == (
        "page-1",
        "page-1-ocr",
        "page-2",
    )


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
    def __init__(self, page_count: int = 1) -> None:
        self.page_count = page_count

    def open(self, _: Path) -> "_Document":
        return _Document(self.page_count)


class _Document:
    def __init__(self, page_count: int = 1) -> None:
        self.pages = tuple(_Page() for _ in range(page_count))

    def __enter__(self) -> "_Document":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def __iter__(self):
        return iter(self.pages)

    def __len__(self) -> int:
        return len(self.pages)

    def __getitem__(self, index: int) -> "_Page":
        return self.pages[index]

    def close(self) -> None:
        return None


class _Page:
    def get_images(self, *, full: bool) -> tuple[tuple[int], ...]:
        assert full
        return ((7,),)

    def get_pixmap(self, *, alpha: bool) -> "_Pixmap":
        assert not alpha
        return _Pixmap()


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
