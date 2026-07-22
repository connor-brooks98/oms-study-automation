import pytest
from pypdf import PdfWriter

from oms_hub.files.pdf import validate_pdf


def test_pdf_requires_at_least_one_page(tmp_path) -> None:
    path = tmp_path / "one.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as stream:
        writer.write(stream)
    assert validate_pdf(path).page_count == 1


def test_invalid_pdf_is_rejected(tmp_path) -> None:
    path = tmp_path / "bad.pdf"
    path.write_bytes(b"%PDF-truncated")
    with pytest.raises(Exception):
        validate_pdf(path)
