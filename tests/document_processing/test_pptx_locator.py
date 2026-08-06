import os
import sys
import tempfile
from io import BytesIO
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.util import Inches

from oms_hub.document_processing.anydoc_adapter import AnydocProcessor
from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedAsset,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
)
from oms_hub.document_processing.pptx_locator import PptxLocatorEnricher
from oms_hub.document_processing.presentation_render import PresentationRenderer
from oms_hub.files.office import OfficeUnavailableError
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


def test_enricher_preserves_candidate_semantic_text_and_order_while_restoring_slides(
    tmp_path: Path,
) -> None:
    source = build_pptx(
        tmp_path / "questions.pptx",
        slides=(
            SlideFixture("Question 1", "Original first slide"),
            SlideFixture("Question 2", "Original second slide"),
        ),
    )
    snapshot = snapshot_for(source)
    parsed = ParsedDocument(
        source_id=snapshot.id,
        source_sha256=snapshot.sha256,
        source_format="pptx",
        parser_name="anydoc",
        parser_version="1",
        segments=(
            ParsedSegment(
                "semantic-a",
                SegmentKind.PARAGRAPH,
                "Semantic first",
                DocumentLocator("block 1"),
            ),
            ParsedSegment(
                "semantic-b",
                SegmentKind.PARAGRAPH,
                "Semantic second",
                DocumentLocator("block 2"),
            ),
        ),
        assets=(),
        warnings=(),
    )

    enriched = PptxLocatorEnricher().enrich(snapshot, parsed)

    assert tuple(segment.key for segment in enriched.segments) == ("semantic-a", "semantic-b")
    assert tuple(segment.text for segment in enriched.segments) == (
        "Semantic first",
        "Semantic second",
    )
    assert tuple(segment.locator.slide_number for segment in enriched.segments) == (1, 2)


def test_enricher_binds_repeated_media_on_one_slide_to_every_picture_occurrence(
    tmp_path: Path,
) -> None:
    source = build_pptx(
        tmp_path / "questions.pptx",
        slides=(SlideFixture("Question", "Which structure?", image=True),),
    )
    presentation = Presentation(source)
    presentation.slides[0].shapes.add_picture(
        str(source.with_suffix(".png")), Inches(3), Inches(3.5)
    )
    presentation.save(source)

    parsed = AnydocProcessor(PptxLocatorEnricher()).parse(
        snapshot_for(source), tmp_path / "assets"
    )

    images = tuple(segment for segment in parsed.segments if segment.kind is SegmentKind.IMAGE)
    assert parsed.warnings == ()
    assert parsed.assets[0].locator.slide_number == 1
    assert len(images) == 2
    assert all(segment.locator.slide_number == 1 for segment in images)
    assert all(segment.asset_keys == (parsed.assets[0].key,) for segment in images)


def test_enricher_leaves_media_reused_across_slides_unbound(tmp_path: Path) -> None:
    source = build_pptx(
        tmp_path / "questions.pptx",
        slides=(
            SlideFixture("Question 1", "Which structure?", image=True),
            SlideFixture("Question 2", "Which pathway?", image=True),
        ),
    )

    parsed = AnydocProcessor(PptxLocatorEnricher()).parse(
        snapshot_for(source), tmp_path / "assets"
    )

    assert parsed.assets[0].locator.slide_number is None
    assert parsed.warnings == (
        "asset 'asset-0' occurs on multiple slides and was not automatically bound",
    )
    images = tuple(segment for segment in parsed.segments if segment.kind is SegmentKind.IMAGE)
    assert len(images) == 2
    assert all(segment.locator.slide_number is None for segment in images)


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


def test_renderer_returns_nonblocking_warning_for_corrupt_rasterizer(
    tmp_path: Path, monkeypatch
) -> None:
    source = build_pptx(
        tmp_path / "questions.pptx",
        slides=(SlideFixture("Question", "Which structure?"),),
    )
    monkeypatch.setitem(sys.modules, "fitz", _BrokenFitzFixture())

    rendered = PresentationRenderer(_PdfFixtureConverter()).render(
        snapshot_for(source), tmp_path / "assets"
    )

    assert rendered.assets == ()
    assert rendered.warnings == ("slide renderer unavailable: corrupt PDF fixture",)


def test_renderer_returns_nonblocking_warning_for_sanitization_failure(
    tmp_path: Path, monkeypatch
) -> None:
    source = build_pptx(
        tmp_path / "questions.pptx",
        slides=(SlideFixture("Question", "Which structure?"),),
    )
    monkeypatch.setitem(sys.modules, "fitz", _InvalidPngFitzFixture())

    rendered = PresentationRenderer(_PdfFixtureConverter()).render(
        snapshot_for(source), tmp_path / "assets"
    )

    assert rendered.assets == ()
    assert rendered.warnings[0].startswith("slide renderer unavailable: quiz image must be")


def test_renderer_preserves_unavailable_warning_when_temp_pdf_is_locked(
    tmp_path: Path, monkeypatch
) -> None:
    source = build_pptx(
        tmp_path / "questions.pptx",
        slides=(SlideFixture("Question", "Which structure?"),),
    )
    original_unlink = Path.unlink

    def locked_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path.name == "slides.pdf":
            raise PermissionError("locked PDF")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", locked_unlink)
    rendered = PresentationRenderer(_UnavailableConverter()).render(
        snapshot_for(source), tmp_path / "assets"
    )

    assert rendered.assets == ()
    assert rendered.warnings == ("slide renderer unavailable: Office is unavailable",)


def test_renderer_preserves_warning_when_temporary_directory_cleanup_hits_locked_pdf(
    tmp_path: Path, monkeypatch
) -> None:
    source = build_pptx(
        tmp_path / "questions.pptx",
        slides=(SlideFixture("Question", "Which structure?"),),
    )

    def locked_rmtree(name: str, *, onexc) -> None:
        onexc(os.unlink, name, PermissionError("locked PDF"))

    monkeypatch.setattr(tempfile._shutil, "rmtree", locked_rmtree)
    rendered = PresentationRenderer(_UnavailableConverter()).render(
        snapshot_for(source), tmp_path / "assets"
    )

    assert rendered.assets == ()
    assert rendered.warnings == ("slide renderer unavailable: Office is unavailable",)


def test_renderer_removes_temporary_pdf_after_success(tmp_path: Path, monkeypatch) -> None:
    source = build_pptx(
        tmp_path / "questions.pptx",
        slides=(SlideFixture("Question", "Which structure?"),),
    )
    deleted_paths: list[Path] = []
    original_unlink = Path.unlink

    def tracking_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path.name == "slides.pdf":
            deleted_paths.append(path)
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", tracking_unlink)
    monkeypatch.setitem(sys.modules, "fitz", _FitzFixture())

    rendered = PresentationRenderer(_PdfFixtureConverter()).render(
        snapshot_for(source), tmp_path / "assets"
    )

    assert rendered.warnings == ()
    assert len(deleted_paths) == 1
    assert not deleted_paths[0].exists()


class _PdfFixtureConverter:
    def convert(self, source: Path, destination: Path) -> None:
        destination.write_bytes(b"fixture pdf")


class _UnavailableConverter:
    def convert(self, source: Path, destination: Path) -> None:
        raise OfficeUnavailableError("Office is unavailable")


class _FitzFixture:
    def open(self, path: Path) -> "_FixtureDocument":
        return _FixtureDocument()


class _BrokenFitzFixture:
    def open(self, path: Path) -> "_FixtureDocument":
        raise RuntimeError("corrupt PDF fixture")


class _InvalidPngFitzFixture:
    def open(self, path: Path) -> "_InvalidPngFixtureDocument":
        return _InvalidPngFixtureDocument()


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


class _InvalidPngFixtureDocument(_FixtureDocument):
    def __iter__(self):
        return iter((_InvalidPngFixturePage(),))


class _InvalidPngFixturePage:
    def get_pixmap(self, *, alpha: bool) -> "_InvalidPngFixturePixmap":
        assert not alpha
        return _InvalidPngFixturePixmap()


class _InvalidPngFixturePixmap:
    def tobytes(self, format: str) -> bytes:
        assert format == "png"
        return b"not an image"
