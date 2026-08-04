from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


@dataclass(frozen=True, slots=True)
class PdfValidation:
    page_count: int
    size: int


@dataclass(frozen=True, slots=True)
class PdfInspection:
    pdf_type: str
    confidence: float
    page_count: int
    pages_needing_ocr: tuple[int, ...]
    used_pdf_inspector: bool


def validate_pdf(path: Path) -> PdfValidation:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("PDF is missing or empty")
    with path.open("rb") as stream:
        reader = PdfReader(stream, strict=True)
        page_count = len(reader.pages)
    if page_count < 1:
        raise ValueError("PDF contains no pages")
    return PdfValidation(page_count=page_count, size=path.stat().st_size)


def inspect_pdf(path: Path) -> PdfInspection:
    validation = validate_pdf(path)
    try:
        import pdf_inspector  # type: ignore[import-not-found]

        result = pdf_inspector.detect_pdf(str(path))
        return PdfInspection(
            pdf_type=str(result.pdf_type),
            confidence=float(result.confidence),
            page_count=int(result.page_count),
            pages_needing_ocr=tuple(int(page) for page in result.pages_needing_ocr),
            used_pdf_inspector=True,
        )
    except (ImportError, AttributeError, OSError, RuntimeError, TypeError, ValueError):
        # Keep local development and offline installs usable if the native wheel
        # is unavailable; pypdf still gives a conservative text/scanned signal.
        with path.open("rb") as stream:
            reader = PdfReader(stream, strict=True)
            pages = tuple(reader.pages)
        text_pages = sum(bool((page.extract_text() or "").strip()) for page in pages)
        if text_pages == validation.page_count:
            pdf_type = "text_based"
        elif text_pages == 0:
            pdf_type = "scanned"
        else:
            pdf_type = "mixed"
        return PdfInspection(
            pdf_type=pdf_type,
            confidence=0.5,
            page_count=validation.page_count,
            pages_needing_ocr=tuple(
                index
                for index, page in enumerate(pages)
                if not (page.extract_text() or "").strip()
            ),
            used_pdf_inspector=False,
        )
