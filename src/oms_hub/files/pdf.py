from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


@dataclass(frozen=True, slots=True)
class PdfValidation:
    page_count: int
    size: int


def validate_pdf(path: Path) -> PdfValidation:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("PDF is missing or empty")
    with path.open("rb") as stream:
        reader = PdfReader(stream, strict=True)
        page_count = len(reader.pages)
    if page_count < 1:
        raise ValueError("PDF contains no pages")
    return PdfValidation(page_count=page_count, size=path.stat().st_size)
