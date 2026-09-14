"""Local lecture admission and extraction; original files remain authoritative."""

import json
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import escape
from zipfile import BadZipFile, ZipFile

from oms_hub.document_processing.anydoc_adapter import AnydocProcessor
from oms_hub.document_processing.domain import ParsedDocument, SourceSnapshot
from oms_hub.document_processing.pdf_adapter import PdfProcessor
from oms_hub.document_processing.pptx_locator import PptxLocatorEnricher
from oms_hub.document_processing.text_adapter import TextProcessor
from oms_hub.files.atomic import sha256_file, verified_atomic_write
from oms_hub.files.pdf import validate_pdf

SUPPORTED_LECTURE_SUFFIXES = (".pptx", ".pdf", ".docx", ".txt", ".md", ".rtf")
LECTURE_MEDIA_TYPES = {
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".rtf": "application/rtf",
}


def validate_lecture_source(path: Path, suffix: str) -> None:
    """Check real content, including when staging uses a suffixless temporary file."""
    if suffix not in SUPPORTED_LECTURE_SUFFIXES:
        raise ValueError("supported lecture formats: " + ", ".join(SUPPORTED_LECTURE_SUFFIXES))
    if path.stat().st_size == 0:
        raise ValueError("uploaded file is empty")
    if suffix in {".pptx", ".docx"}:
        main, tag = (
            (
                "ppt/presentation.xml",
                "{http://schemas.openxmlformats.org/presentationml/2006/main}presentation",
            )
            if suffix == ".pptx"
            else (
                "word/document.xml",
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document",
            )
        )
        try:
            with ZipFile(path) as archive:
                members = archive.infolist()
                names = [member.filename for member in members]
                if len(names) != len(set(names)) or len(names) > 20000:
                    raise ValueError("Office archive has duplicate or excessive members")
                if sum(member.file_size for member in members) > 512 * 1024 * 1024:
                    raise ValueError("Office archive exceeds expanded size limit")
                for member_name in ("[Content_Types].xml", main):
                    member = archive.getinfo(member_name)
                    if member.file_size > 32 * 1024 * 1024:
                        raise ValueError("Office XML exceeds size limit")
                    payload = archive.read(member)
                    if b"<!DOCTYPE" in payload or b"<!ENTITY" in payload:
                        raise ValueError("Office XML entities are not supported")
                    root = ElementTree.fromstring(payload)
                    if member_name == main and root.tag != tag:
                        raise ValueError("Office document type does not match extension")
        except (BadZipFile, KeyError, ElementTree.ParseError, RuntimeError) as error:
            raise ValueError(
                "file is not a valid "
                + ("PowerPoint presentation" if suffix == ".pptx" else "Word document")
            ) from error
    elif suffix == ".pdf":
        try:
            validate_pdf(path)
        except Exception as error:
            raise ValueError("file is not a readable, unencrypted PDF") from error
    elif suffix == ".rtf":
        payload = path.read_bytes().strip()
        if not payload.startswith(rb"{\rtf1") or not payload.endswith(b"}"):
            raise ValueError("file is not a valid RTF document")
        depth = 0
        escaped = False
        for byte in payload:
            if escaped:
                escaped = False
                continue
            if byte == 92:
                escaped = True
            elif byte == 123:
                depth += 1
            elif byte == 125:
                depth -= 1
                if depth < 0:
                    raise ValueError("RTF groups are unbalanced")
        if depth:
            raise ValueError("RTF groups are unbalanced")
    else:
        try:
            text = path.read_bytes().decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError("transcript is not UTF-8") from error
        if not text.strip():
            raise ValueError("transcript contains no text")
        if "\x00" in text or sum(c.isprintable() or c in "\r\n\t" for c in text) / len(text) < 0.85:
            raise ValueError("transcript contains binary data")


def parse_lecture_source(
    path: Path, asset_root: Path, *, require_text: bool = True
) -> ParsedDocument:
    suffix = path.suffix.casefold()
    validate_lecture_source(path, suffix)
    digest = sha256_file(path)
    snapshot = SourceSnapshot(digest, path.name, path, LECTURE_MEDIA_TYPES[suffix], digest)
    if suffix == ".pdf":
        parsed = PdfProcessor().parse(snapshot, asset_root)
    elif suffix in {".txt", ".md"}:
        parsed = TextProcessor().parse(snapshot, asset_root)
    else:
        parsed = AnydocProcessor(PptxLocatorEnricher()).parse(snapshot, asset_root)
    blockers = [warning for warning in parsed.warnings if warning.startswith("BLOCKER:")]
    if blockers and require_text:
        raise ValueError("; ".join(blockers))
    if require_text and not any(segment.text.strip() for segment in parsed.segments):
        raise ValueError(
            "document contains no extractable text; supply a text transcript or searchable PDF"
        )
    # The snapshot's original hash and locators remain available beside derived output.
    verified_atomic_write(
        json.dumps(asdict(parsed), default=str, ensure_ascii=False, indent=2).encode(),
        asset_root / "document.json",
    )
    return parsed


@lru_cache(maxsize=1)
def _reading_font() -> str:
    from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
    from reportlab.pdfbase.ttfonts import TTFont  # type: ignore[import-untyped]

    font = Path(__file__).parents[1] / "study_generation/assets/quiz_fonts/DejaVuSans.ttf"
    name = "OMSLectureReading"
    pdfmetrics.registerFont(TTFont(name, str(font)))
    return name


def render_reading_pdf(document: ParsedDocument, destination: Path) -> None:
    """Make a reflowed reading copy, never pretend to reproduce Office pagination."""
    from reportlab.lib.styles import ParagraphStyle  # type: ignore[import-untyped]
    from reportlab.platypus import (  # type: ignore[import-untyped]
        Image,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
    )

    style = ParagraphStyle(
        "lecture", fontName=_reading_font(), fontSize=10, leading=14, spaceAfter=8
    )
    story = [
        Paragraph(
            "Reflowed reading copy — source block labels refer to the original document.", style
        )
    ]
    for warning in document.warnings:
        story.append(Paragraph("Extraction note: " + escape(warning), style))
    assets = {asset.key: asset for asset in document.assets}
    displayed: set[str] = set()
    for segment in document.segments:
        story.append(Paragraph(escape(segment.locator.label), style))
        if segment.text.strip():
            story.append(Paragraph(escape(segment.text).replace("\n", "<br/>"), style))
        for key in segment.asset_keys:
            asset = assets[key]
            if key in displayed or asset.path is None or not asset.width or not asset.height:
                continue
            scale = min(1.0, 480 / asset.width, 600 / asset.height)
            story.append(
                Image(str(asset.path), width=asset.width * scale, height=asset.height * scale)
            )
            displayed.add(key)
        story.append(Spacer(1, 5))
    destination.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(str(destination), invariant=1, leftMargin=54, rightMargin=54).build(story)
    validate_pdf(destination)
