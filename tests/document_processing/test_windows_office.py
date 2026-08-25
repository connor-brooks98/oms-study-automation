"""Native Office smoke coverage for controlled Windows release validation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pypdf import PdfReader

from oms_hub.files.office import SerialOfficeConverter
from tests.document_processing.pptx_factory import SlideFixture, build_pptx


@pytest.mark.windows_office
@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows desktop Microsoft Office")
def test_serial_office_converter_preserves_three_slide_page_mapping(
    tmp_path: Path,
) -> None:
    markers = ("GATE2A-SLIDE-1", "GATE2A-SLIDE-2", "GATE2A-SLIDE-3")
    source = build_pptx(
        tmp_path / "office-smoke.pptx",
        slides=tuple(
            SlideFixture(marker, "Deterministic PDF export.") for marker in markers
        ),
    )
    destination = tmp_path / "office-smoke.pdf"

    SerialOfficeConverter(timeout_seconds=120).convert(source, destination)

    assert destination.is_file()
    with destination.open("rb") as stream:
        pages = PdfReader(stream, strict=True).pages
        assert len(pages) == 3
        page_text = tuple(page.extract_text() or "" for page in pages)

    assert len(markers) == len(set(markers)) == 3
    for expected_page, marker in enumerate(markers):
        assert marker in page_text[expected_page]
        assert all(
            marker not in text
            for page_number, text in enumerate(page_text)
            if page_number != expected_page
        )
