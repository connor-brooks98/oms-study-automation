"""Bounded PowerPoint rendering for visual content without extractable images."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from math import ceil
from pathlib import Path
from tempfile import TemporaryDirectory

from oms_hub.document_processing.assets import persist_asset
from oms_hub.document_processing.domain import DocumentLocator, ParsedAsset, SourceSnapshot
from oms_hub.files.office import OfficeConverter


@dataclass(frozen=True, slots=True)
class PresentationRenderResult:
    assets: tuple[ParsedAsset, ...]
    warnings: tuple[str, ...]


class PresentationRenderer:
    """Create optional, sanitized full-slide PNG candidates through Office and PyMuPDF."""

    def __init__(self, converter: OfficeConverter) -> None:
        self.converter = converter

    def render(
        self,
        source: SourceSnapshot,
        asset_root: Path,
        *,
        max_pages: int = 500,
        max_pixels: int | None = None,
    ) -> PresentationRenderResult:
        if max_pages < 1 or (max_pixels is not None and max_pixels < 1):
            raise ValueError("render limits must be positive")
        if source.path.suffix.casefold() == ".pdf":
            try:
                return self._rasterize(
                    source.path,
                    asset_root,
                    page_locators=True,
                    max_pages=max_pages,
                    max_pixels=max_pixels or 4_000_000,
                )
            except Exception as error:  # noqa: BLE001 - preserve a visible review blocker
                return PresentationRenderResult((), (f"slide renderer unavailable: {error}",))
        if source.path.suffix.casefold() not in {".ppt", ".pptx"}:
            return PresentationRenderResult(
                (), ("slide renderer supports only PowerPoint sources",)
            )
        with TemporaryDirectory(
            prefix="oms-slide-render-", ignore_cleanup_errors=True
        ) as temporary_directory:
            pdf_path = Path(temporary_directory) / "slides.pdf"
            try:
                self.converter.convert(source.path, pdf_path)
                return self._rasterize(
                    pdf_path, asset_root, max_pages=max_pages, max_pixels=max_pixels
                )
            except Exception as error:  # noqa: BLE001 - renderer degradation is non-blocking
                return PresentationRenderResult((), (f"slide renderer unavailable: {error}",))
            finally:
                try:
                    pdf_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _rasterize(
        self,
        pdf_path: Path,
        asset_root: Path,
        *,
        page_locators: bool = False,
        max_pages: int = 500,
        max_pixels: int | None = None,
    ) -> PresentationRenderResult:
        import fitz  # type: ignore[import-untyped]

        assets: list[ParsedAsset] = []
        with fitz.open(pdf_path) as document:
            pages = list(islice(document, max_pages + 1))
            if len(pages) > max_pages:
                raise ValueError("slide render page limit exceeded")
            if max_pixels is not None and any(
                ceil(page.rect.width) * ceil(page.rect.height) > max_pixels for page in pages
            ):
                raise ValueError("slide render pixel limit exceeded")
            prefix = "page" if page_locators else "slide"
            for slide_number, page in enumerate(pages, start=1):
                stored = persist_asset(
                    asset_root,
                    f"{prefix}-{slide_number}-render",
                    "image/png",
                    page.get_pixmap(alpha=False).tobytes("png"),
                )
                if stored.path is None:
                    continue
                assets.append(
                    ParsedAsset(
                        key=stored.key,
                        path=stored.path,
                        media_type=stored.media_type,
                        sha256=stored.sha256,
                        width=stored.width,
                        height=stored.height,
                        locator=DocumentLocator(
                            label=f"{prefix} {slide_number} render",
                            page_number=slide_number if page_locators else None,
                            slide_number=None if page_locators else slide_number,
                        ),
                        origin="full-page-render" if page_locators else "full-slide-render",
                    )
                )
        return PresentationRenderResult(tuple(assets), ())
