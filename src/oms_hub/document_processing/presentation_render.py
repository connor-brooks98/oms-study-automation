"""Bounded PowerPoint rendering for visual content without extractable images."""

from __future__ import annotations

from dataclasses import dataclass
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

    def render(self, source: SourceSnapshot, asset_root: Path) -> PresentationRenderResult:
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
                return self._rasterize(pdf_path, asset_root)
            except Exception as error:  # noqa: BLE001 - renderer degradation is non-blocking
                return PresentationRenderResult((), (f"slide renderer unavailable: {error}",))
            finally:
                try:
                    pdf_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _rasterize(self, pdf_path: Path, asset_root: Path) -> PresentationRenderResult:
        import fitz  # type: ignore[import-untyped]

        assets: list[ParsedAsset] = []
        with fitz.open(pdf_path) as document:
            for slide_number, page in enumerate(document, start=1):
                stored = persist_asset(
                    asset_root,
                    f"slide-{slide_number}-render",
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
                            label=f"slide {slide_number} render", slide_number=slide_number
                        ),
                        origin="full-slide-render",
                    )
                )
        return PresentationRenderResult(tuple(assets), ())
