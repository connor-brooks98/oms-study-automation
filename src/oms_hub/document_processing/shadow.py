"""Safe, report-only comparisons for a candidate lecture-document parser."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from pptx import Presentation

from oms_hub.document_processing.domain import (
    DocumentLocator,
    DocumentProcessor,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
    SourceSnapshot,
)
from oms_hub.document_processing.router import ParserMode


@dataclass(frozen=True, slots=True)
class ShadowComparison:
    legacy_document: ParsedDocument | None
    candidate_document: ParsedDocument | None
    report: dict[str, object]


@dataclass(frozen=True, slots=True)
class ShadowParseResult:
    document: ParsedDocument
    report: dict[str, object]
    degraded: bool


class LegacyPptxProcessor:
    """Local, non-Anki PowerPoint text extraction used as the shadow baseline."""

    name = "legacy-pptx"
    version = "1"

    def supports(self, snapshot: SourceSnapshot) -> bool:
        return snapshot.path.suffix.casefold() == ".pptx"

    def parse(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument:
        del asset_root
        if not self.supports(snapshot):
            raise ValueError(f"legacy PowerPoint parser does not support {snapshot.path.name!r}")
        presentation = Presentation(str(snapshot.path))
        segments: list[ParsedSegment] = []
        for slide_number, slide in enumerate(presentation.slides, start=1):
            for shape in slide.shapes:
                text = ""
                kind = SegmentKind.PARAGRAPH
                if getattr(shape, "has_table", False):
                    kind = SegmentKind.TABLE
                    text = "\n".join(
                        " | ".join(cell.text.strip() for cell in row.cells)
                        for row in shape.table.rows
                    ).strip()
                elif getattr(shape, "has_text_frame", False):
                    text = shape.text.strip()
                if text:
                    index = len(segments) + 1
                    segments.append(
                        ParsedSegment(
                            key=f"slide-{slide_number}-block-{index}",
                            kind=kind,
                            text=text,
                            locator=DocumentLocator(
                                f"slide {slide_number}",
                                slide_number=slide_number,
                                block_index=index,
                            ),
                        )
                    )
            notes = getattr(getattr(slide, "notes_slide", None), "notes_text_frame", None)
            note_text = (notes.text or "").strip() if notes is not None else ""
            if note_text:
                index = len(segments) + 1
                segments.append(
                    ParsedSegment(
                        key=f"slide-{slide_number}-note-{index}",
                        kind=SegmentKind.NOTE,
                        text=note_text,
                        locator=DocumentLocator(
                            f"slide {slide_number} notes",
                            slide_number=slide_number,
                            block_index=index,
                        ),
                    )
                )
        return ParsedDocument(
            source_id=snapshot.id,
            source_sha256=snapshot.sha256,
            source_format="pptx",
            parser_name=self.name,
            parser_version=self.version,
            segments=tuple(segments),
            assets=(),
            warnings=(),
        )


class DocumentShadowEvaluator:
    """Run a candidate parser without ever exposing document text in reports."""

    def __init__(self, candidate: DocumentProcessor, legacy: DocumentProcessor) -> None:
        self.candidate = candidate
        self.legacy = legacy

    def compare(self, snapshot: SourceSnapshot, asset_root: Path) -> ShadowComparison:
        started = perf_counter()
        legacy_document, legacy_error, legacy_duration = self._parse(
            self.legacy, snapshot, asset_root / "legacy"
        )
        candidate_document, candidate_error, candidate_duration = self._parse(
            self.candidate, snapshot, asset_root / "candidate"
        )
        blockers = list(_document_blockers(legacy_document, "legacy"))
        blockers.extend(_document_blockers(candidate_document, "candidate"))
        if legacy_error:
            blockers.append("legacy parser failed")
        if candidate_error:
            blockers.append("candidate parser failed")
        report: dict[str, object] = {
            "source_sha256": snapshot.sha256,
            "mode": ParserMode.SHADOW.value,
            "duration_ms": round((perf_counter() - started) * 1000, 3),
            "legacy": _document_report(
                legacy_document, self.legacy, legacy_duration, legacy_error
            ),
            "candidate": _document_report(
                candidate_document, self.candidate, candidate_duration, candidate_error
            ),
            "candidate_error": candidate_error,
            "fallback_used": False,
            "promotion_blockers": tuple(sorted(set(blockers))),
        }
        return ShadowComparison(legacy_document, candidate_document, report)

    def parse_primary(self, snapshot: SourceSnapshot, asset_root: Path) -> ShadowParseResult:
        comparison = self.compare(snapshot, asset_root)
        report = dict(comparison.report)
        report["mode"] = ParserMode.ANYDOC.value
        if comparison.candidate_document is not None:
            return ShadowParseResult(comparison.candidate_document, report, False)
        if comparison.legacy_document is not None:
            report["fallback_used"] = True
            return ShadowParseResult(comparison.legacy_document, report, True)
        raise ValueError("both candidate and legacy document parsers failed")

    def parse(
        self,
        snapshot: SourceSnapshot,
        asset_root: Path,
        mode: ParserMode,
    ) -> ShadowParseResult:
        if mode is ParserMode.ANYDOC:
            return self.parse_primary(snapshot, asset_root)
        comparison = self.compare(snapshot, asset_root)
        if comparison.legacy_document is None:
            raise ValueError("legacy document parser failed")
        report = dict(comparison.report)
        report["mode"] = mode.value
        return ShadowParseResult(comparison.legacy_document, report, False)

    @staticmethod
    def write_report(report: dict[str, object], destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.partial-{uuid4().hex}")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(report, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _parse(
        processor: DocumentProcessor,
        snapshot: SourceSnapshot,
        asset_root: Path,
    ) -> tuple[ParsedDocument | None, str | None, float]:
        started = perf_counter()
        if not processor.supports(snapshot):
            return None, "parser does not support this source", 0.0
        try:
            return processor.parse(snapshot, asset_root), None, round(
                (perf_counter() - started) * 1000, 3
            )
        except Exception as error:  # noqa: BLE001 - parser failures become safe diagnostics
            return None, _safe_message(str(error)), round((perf_counter() - started) * 1000, 3)


def _document_report(
    document: ParsedDocument | None,
    processor: DocumentProcessor,
    duration_ms: float,
    error: str | None,
) -> dict[str, object]:
    if document is None:
        return {
            "parser_name": processor.name,
            "parser_version": processor.version,
            "duration_ms": duration_ms,
            "segment_counts": {},
            "page_coverage": (),
            "slide_coverage": (),
            "notes": 0,
            "tables": 0,
            "assets": 0,
            "warnings": (),
            "normalized_text_sha256": None,
            "error": error,
        }
    counts = Counter(segment.kind.value for segment in document.segments)
    normalized = "\n".join(_normalize_text(segment.text) for segment in document.segments)
    return {
        "parser_name": document.parser_name,
        "parser_version": document.parser_version,
        "duration_ms": duration_ms,
        "segment_counts": dict(sorted(counts.items())),
        "page_coverage": tuple(
            sorted(
                {
                    segment.locator.page_number
                    for segment in document.segments
                    if segment.locator.page_number
                }
            )
        ),
        "slide_coverage": tuple(
            sorted(
                {
                    segment.locator.slide_number
                    for segment in document.segments
                    if segment.locator.slide_number
                }
            )
        ),
        "notes": counts[SegmentKind.NOTE.value],
        "tables": counts[SegmentKind.TABLE.value],
        "assets": len(document.assets),
        "warnings": tuple(_safe_message(warning) for warning in document.warnings),
        "normalized_text_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "error": None,
    }


def _document_blockers(document: ParsedDocument | None, parser: str) -> tuple[str, ...]:
    if document is None:
        return ()
    return tuple(
        f"{parser} warning requires review"
        for warning in document.warnings
        if warning.casefold().startswith("blocker:")
    )


def _normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _safe_message(value: str) -> str:
    compact = " ".join(value.split())[:500]
    return re.sub(
        r"(?i)\b(api[ _-]?key|token|password|secret)\b\s*[:=]\s*\S+",
        r"\1=[redacted]",
        compact,
    )
