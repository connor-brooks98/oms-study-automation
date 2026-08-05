import hashlib
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from oms_hub.document_processing.anydoc_adapter import AnydocProcessor
from oms_hub.document_processing.assets import persist_asset
from oms_hub.document_processing.domain import SegmentKind, SourceSnapshot
from oms_hub.document_processing.pptx_locator import PptxLocatorEnricher
from tests.document_processing.pptx_factory import SlideFixture, build_pptx, snapshot_for


def test_pptx_keeps_slide_numbers_notes_and_image_origin(tmp_path: Path) -> None:
    source = build_pptx(
        tmp_path / "questions.pptx",
        slides=(
            SlideFixture("Question 1", "Which structure?", note="Answer: A", image=True),
            SlideFixture("Question 2", "Which pathway?", note="Answer: B", image=False),
        ),
    )
    snapshot = snapshot_for(source)

    parsed = AnydocProcessor(PptxLocatorEnricher()).parse(snapshot, tmp_path / "assets")

    assert {segment.locator.slide_number for segment in parsed.segments} == {1, 2}
    assert any(segment.kind is SegmentKind.NOTE for segment in parsed.segments)
    assert parsed.assets[0].locator.slide_number == 1


def test_persist_asset_sanitizes_raster_payload_and_uses_content_address(tmp_path: Path) -> None:
    payload = _jpeg_payload()

    asset = persist_asset(tmp_path, "figure-1", "image/jpeg", payload)

    assert asset.path is not None
    assert asset.path.is_file()
    assert asset.path.suffix == ".png"
    assert asset.media_type == "image/png"
    assert asset.sha256 == hashlib.sha256(asset.path.read_bytes()).hexdigest()
    assert asset.width == 8
    assert asset.height == 6


def test_persist_asset_keeps_unsupported_object_as_unserved_diagnostic(tmp_path: Path) -> None:
    payload = b"an embedded office object"

    asset = persist_asset(tmp_path, "ole-object-1", "application/vnd.ms-office", payload)

    assert asset.path is None
    assert asset.sha256 == hashlib.sha256(payload).hexdigest()
    assert asset.diagnostic == "unsupported embedded asset media type: application/vnd.ms-office"


def test_persist_asset_rejects_invalid_key_and_media_type(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="asset key"):
        persist_asset(tmp_path, "../escape", "image/png", _png_payload())
    with pytest.raises(ValueError, match="media type"):
        persist_asset(tmp_path, "image-1", "not a mime type", _png_payload())


def test_adapter_leaves_pdf_sources_to_the_page_aware_pdf_processor(tmp_path: Path) -> None:
    source = tmp_path / "questions.pdf"
    source.write_bytes(b"%PDF-1.7")
    snapshot = SourceSnapshot(
        id="questions-pdf",
        title="Questions",
        path=source,
        media_type="application/pdf",
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    )

    assert not AnydocProcessor(PptxLocatorEnricher()).supports(snapshot)


def _jpeg_payload() -> bytes:
    image = Image.new("RGB", (8, 6), (40, 80, 120))
    output = BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


def _png_payload() -> bytes:
    image = Image.new("RGB", (2, 2), (20, 40, 60))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
