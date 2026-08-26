"""Page-aware PDF parsing with explicit OCR blockers and safe raster assets."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from oms_hub.document_processing.assets import persist_asset
from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedAsset,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
    SourceSnapshot,
)
from oms_hub.document_processing.ocr import LocalOcr
from oms_hub.files.pdf import inspect_pdf


class PdfProcessor:
    """Preserve page provenance and distinguish OCR work from an empty document."""

    name = "pdf"
    version = "2"

    def __init__(self, ocr: LocalOcr | None = None) -> None:
        self.ocr = ocr or LocalOcr()

    def supports(self, snapshot: SourceSnapshot) -> bool:
        return (
            snapshot.media_type.split(";", 1)[0].casefold().strip() == "application/pdf"
            and snapshot.path.suffix.casefold() == ".pdf"
        )

    def parse(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument:
        if not self.supports(snapshot):
            raise ValueError(f"PDF processor does not support source {snapshot.path.name!r}")
        inspection = inspect_pdf(snapshot.path)
        warnings = [f"OCR required for page {page}" for page in inspection.pages_needing_ocr]
        segments: list[ParsedSegment] = []
        try:
            page_texts = _page_texts(snapshot.path)
        except Exception as error:  # reader construction cannot look like an empty PDF
            page_texts = ()
            warnings.append(f"BLOCKER: PDF parsing incomplete; text reader failed: {error}")
        for page_number, text in enumerate(page_texts, start=1):
            if isinstance(text, Exception):
                warnings.append(
                    f"BLOCKER: PDF parsing incomplete on page {page_number}: {text}"
                )
            elif text:
                segments.append(
                    ParsedSegment(
                        key=f"page-{page_number}",
                        kind=SegmentKind.PARAGRAPH,
                        text=text,
                        locator=DocumentLocator(f"page {page_number}", page_number=page_number),
                    )
                )
        if not segments:
            warnings.append("PDF contained no extractable text")
        assets, image_warnings = _extract_images(snapshot.path, asset_root)
        warnings.extend(image_warnings)
        ocr_segments, ocr_warnings = _ocr_required_pages(
            snapshot.path, asset_root, inspection.pages_needing_ocr, self.ocr
        )
        segments.extend(ocr_segments)
        warnings.extend(ocr_warnings)
        return ParsedDocument(
            source_id=snapshot.id,
            source_sha256=snapshot.sha256,
            source_format="pdf",
            parser_name=self.name,
            parser_version=self.version,
            segments=tuple(
                sorted(
                    segments,
                    key=lambda segment: (segment.locator.page_number or 10**9, segment.key),
                )
            ),
            assets=assets,
            warnings=tuple(warnings),
        )


def _ocr_required_pages(
    path: Path, asset_root: Path, pages: tuple[int, ...], ocr: LocalOcr
) -> tuple[tuple[ParsedSegment, ...], tuple[str, ...]]:
    if not pages:
        return (), ()
    fitz_module = _lazy_fitz()
    if fitz_module is None:
        return (), tuple(
            f"BLOCKER: OCR is required but unavailable or empty for page {page}" for page in pages
        )
    segments: list[ParsedSegment] = []
    warnings: list[str] = []
    try:
        with fitz_module.open(path) as document:
            for page_number in pages:
                if not 1 <= page_number <= len(document):
                    warnings.append(f"BLOCKER: OCR page {page_number} is outside the PDF")
                    continue
                stored = persist_asset(
                    asset_root,
                    f"page-{page_number}-ocr-render",
                    "image/png",
                    document[page_number - 1].get_pixmap(alpha=False).tobytes("png"),
                )
                text = ocr.text(stored.path) if stored.path is not None else None
                if text:
                    segments.append(
                        ParsedSegment(
                            key=f"page-{page_number}-ocr",
                            kind=SegmentKind.PARAGRAPH,
                            text=text,
                            locator=DocumentLocator(
                                f"page {page_number} OCR", page_number=page_number
                            ),
                        )
                    )
                else:
                    warnings.append(
                        f"BLOCKER: OCR is required but unavailable or empty for page {page_number}"
                    )
    except Exception:
        warnings.extend(
            f"BLOCKER: OCR is required but unavailable or empty for page {page}" for page in pages
        )
    return tuple(segments), tuple(dict.fromkeys(warnings))


def _page_texts(path: Path) -> tuple[str | Exception, ...]:
    with path.open("rb") as stream:
        reader = PdfReader(stream, strict=True)
        text: list[str | Exception] = []
        for page in reader.pages:
            try:
                text.append((page.extract_text() or "").strip())
            except Exception as error:  # retain text from other pages for review
                text.append(error)
        return tuple(text)


def _extract_images(
    path: Path, asset_root: Path
) -> tuple[tuple[ParsedAsset, ...], tuple[str, ...]]:
    fitz_module = _lazy_fitz()
    if fitz_module is None:
        return (), ("PDF image extraction unavailable: PyMuPDF is unavailable",)
    assets: list[ParsedAsset] = []
    warnings: list[str] = []
    try:
        with fitz_module.open(path) as document:
            for page_number, page in enumerate(document, start=1):
                for image_number, image in enumerate(page.get_images(full=True), start=1):
                    try:
                        payload = fitz_module.Pixmap(document, image[0]).tobytes("png")
                        stored = persist_asset(
                            asset_root,
                            f"page-{page_number}-image-{image_number}",
                            "image/png",
                            payload,
                        )
                        if stored.path is None:
                            warnings.append(
                                "PDF page "
                                f"{page_number} image {image_number} was not a supported raster"
                            )
                            continue
                        assets.append(
                            ParsedAsset(
                                key=stored.key,
                                path=stored.path,
                                media_type=stored.media_type,
                                sha256=stored.sha256,
                                locator=DocumentLocator(
                                    f"page {page_number} image {image_number}",
                                    page_number=page_number,
                                ),
                                width=stored.width,
                                height=stored.height,
                                origin="embedded-pdf-image",
                            )
                        )
                    except Exception as error:  # retain the page parse when one image is malformed
                        warnings.append(
                            "PDF page "
                            f"{page_number} image {image_number} extraction failed: {error}"
                        )
    except Exception as error:
        warnings.append(f"PDF image extraction unavailable: {error}")
    return tuple(assets), tuple(warnings)


def _lazy_fitz() -> Any | None:
    existing = sys.modules.get("fitz")
    if existing is not None:
        return existing
    # The local development interpreter is Python 3.13, where native PyMuPDF
    # imports have crashed. Production verification exercises the native path on
    # Python 3.12; fakes remain injectable here for deterministic tests.
    if sys.version_info >= (3, 13):
        return None
    try:
        import fitz  # type: ignore[import-untyped]
    except (ImportError, OSError, RuntimeError):
        return None
    return fitz
