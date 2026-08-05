import sys
from io import BytesIO
from pathlib import Path

from PIL import Image

from oms_hub.document_processing.anydoc_adapter import AnydocProcessor
from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedAsset,
    ParsedDocument,
    SegmentKind,
)
from oms_hub.document_processing.pptx_locator import PptxLocatorEnricher
from oms_hub.document_processing.presentation_render import PresentationRenderer
from tests.document_processing.pptx_factory import SlideFixture, build_pptx, snapshot_for


def test_enricher_uses_slide_order_for_text_notes_and_picture_locators(tmp_path: Path) -> None:
    source = build_pptx(
        tmp_path / "questions.pptx",
        slides=(
            SlideFixture("Question 1", "Which structure?", note="Answer: A", image=True),
            SlideFixture("Question 2", "Which pathway?", note="Answer: B"),
        ),
    )

    parsed = AnydocProcessor(PptxLocatorEnricher()).parse(
        snapshot_for(source), tmp_path / "assets"
    )

    notes = tuple(segment for segment in parsed.segments if segment.kind is SegmentKind.NOTE)
    images = tuple(segment for segment in parsed.segments if segment.kind is SegmentKind.IMAGE)
    assert tuple(segment.locator.label for segment in notes) == ("slide 1 notes", "slide 2 notes")
    assert tuple(segment.text for segment in notes) == ("Answer: A", "Answer: B")
    assert tuple(segment.locator.label for segment in images) == ("slide 1 image 1",)
    assert images[0].asset_keys == (parsed.assets[0].key,)
    assert all(segment.locator.slide_number is not None for segment in parsed.segments)


def test_enricher_retains_unmatched_anydoc_asset_without_auto_binding(tmp_path: Path) -> None:
    source = build_pptx(
        tmp_path / "questions.pptx",
        slides=(SlideFixture("Question", "Which structure?"),),
    )
    snapshot = snapshot_for(source)
    parsed = ParsedDocument(
        source_id=snapshot.id,
        source_sha256=snapshot.sha256,
        source_format="pptx",
        parser_name="fixture",
        parser_version="1",
        segments=(),
        assets=(
            ParsedAsset(
                key="asset-0",
                path=None,
                media_type="image/png",
                sha256="a" * 64,
                locator=DocumentLocator("asset 0"),
                origin="ppt/media/not-in-slides.png",
            ),
        ),
        warnings=(),
    )

    enriched = PptxLocatorEnricher().enrich(snapshot, parsed)

    assert enriched.assets[0].locator.slide_number is None
    assert enriched.warnings == ("asset 'asset-0' could not be matched to a PowerPoint slide",)
    assert all(segment.kind is not SegmentKind.IMAGE for segment in enriched.segments)


def test_renderer_persists_bounded_full_slide_candidates_with_slide_locators(
    tmp_path: Path, monkeypatch
) -> None:
    source = build_pptx(
        tmp_path / "questions.pptx",
        slides=(SlideFixture("Question", "Which structure?"),),
    )
    monkeypatch.setitem(sys.modules, "fitz", _FitzFixture())

    rendered = PresentationRenderer(_PdfFixtureConverter()).render(
        snapshot_for(source), tmp_path / "assets"
    )

    assert rendered.warnings == ()
    assert tuple(asset.key for asset in rendered.assets) == ("slide-1-render", "slide-2-render")
    assert tuple(asset.locator.label for asset in rendered.assets) == (
        "slide 1 render",
        "slide 2 render",
    )
    assert all(asset.path is not None and asset.path.is_file() for asset in rendered.assets)


class _PdfFixtureConverter:
    def convert(self, source: Path, destination: Path) -> None:
        destination.write_bytes(b"fixture pdf")


class _FitzFixture:
    def open(self, path: Path) -> "_FixtureDocument":
        return _FixtureDocument()


class _FixtureDocument:
    def __enter__(self) -> "_FixtureDocument":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

    def __iter__(self):
        return iter((_FixturePage(), _FixturePage()))


class _FixturePage:
    def get_pixmap(self, *, alpha: bool) -> "_FixturePixmap":
        assert not alpha
        return _FixturePixmap()


class _FixturePixmap:
    def tobytes(self, format: str) -> bytes:
        assert format == "png"
        image = Image.new("RGB", (3, 2), (20, 40, 60))
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
