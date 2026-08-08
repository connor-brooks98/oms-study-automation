"""Native Office smoke coverage for controlled Windows release validation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from oms_hub.files.office import SerialOfficeConverter
from oms_hub.files.pdf import validate_pdf
from tests.document_processing.pptx_factory import SlideFixture, build_pptx


@pytest.mark.windows_office
@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows desktop Microsoft Office")
def test_serial_office_converter_exports_a_real_pptx_to_a_valid_pdf(tmp_path: Path) -> None:
    source = build_pptx(
        tmp_path / "office-smoke.pptx",
        slides=(SlideFixture("Office smoke", "Deterministic PDF export."),),
    )
    destination = tmp_path / "office-smoke.pdf"

    SerialOfficeConverter(timeout_seconds=120).convert(source, destination)

    assert destination.is_file()
    assert validate_pdf(destination).page_count == 1
