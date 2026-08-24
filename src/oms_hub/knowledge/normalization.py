"""Deterministic course-source text normalization and evidence rendering."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Self

from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
)
from oms_hub.knowledge.ids import evidence_id as make_evidence_id
from oms_hub.knowledge.ids import sha256_text
from oms_hub.knowledge.models import EvidenceLocator, EvidenceLocatorKind, EvidenceUnit
from oms_hub.providers.contracts import AuthorityClass

__all__ = [
    "CourseRevisionInput",
    "SlideInput",
    "TranscriptInput",
    "TranscriptSegmentInput",
    "normalize_course_revision",
    "render_index_markdown",
]


@dataclass(frozen=True, slots=True)
class SlideInput:
    """Minimal synthetic slide adapter for the normalization contract."""

    number: int
    text: str = ""
    speaker_notes: str = ""
    ocr_text: str = ""


@dataclass(frozen=True, slots=True)
class TranscriptSegmentInput:
    """Minimal synthetic transcript adapter with a stable segment locator."""

    number: str | int | None = None
    text: str = ""
    locator: DocumentLocator | None = None
    segment_id: str | int | None = None

    def __post_init__(self) -> None:
        if self.number is None and self.segment_id is None and self.locator is None:
            raise ValueError("transcript segment needs number, segment_id, or locator")

    @property
    def value(self) -> str:
        if self.number is not None:
            return str(self.number)
        if self.segment_id is not None:
            return str(self.segment_id)
        assert self.locator is not None
        return _locator_value(self.locator)


TranscriptInput = TranscriptSegmentInput


@dataclass(frozen=True, slots=True)
class CourseRevisionInput:
    """Ordered parsed or synthetic course-material input for one revision."""

    source_revision_id: str
    course_id: str
    exam_id: str | None = None
    lecture_id: str | None = None
    parsed_document: ParsedDocument | None = None
    slides: tuple[SlideInput, ...] = ()
    transcript_segments: tuple[TranscriptSegmentInput, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "slides", tuple(self.slides))
        object.__setattr__(self, "transcript_segments", tuple(self.transcript_segments))

    @classmethod
    def synthetic(
        cls,
        *,
        source_revision_id: str,
        course_id: str,
        exam_id: str | None = None,
        lecture_id: str | None = None,
        slides: Sequence[SlideInput] = (),
        transcript_segments: Sequence[TranscriptSegmentInput] = (),
    ) -> Self:
        return cls(
            source_revision_id=source_revision_id,
            course_id=course_id,
            exam_id=exam_id,
            lecture_id=lecture_id,
            slides=tuple(slides),
            transcript_segments=tuple(transcript_segments),
        )


def normalize_course_revision(input: CourseRevisionInput) -> tuple[EvidenceUnit, ...]:
    """Convert ordered course input into immutable, source-located evidence units."""
    units: list[EvidenceUnit] = []

    if input.parsed_document is not None:
        for segment in input.parsed_document.segments:
            unit = _unit_from_parsed_segment(input, segment)
            if unit is not None:
                units.append(unit)

    for slide in input.slides:
        text = slide.text if _normalize_text(slide.text) else slide.ocr_text
        unit = _build_unit(
            input,
            EvidenceLocatorKind.SLIDE,
            str(slide.number),
            text,
        )
        if unit is not None:
            units.append(unit)
        note = _build_unit(
            input,
            EvidenceLocatorKind.SPEAKER_NOTE,
            str(slide.number),
            slide.speaker_notes,
        )
        if note is not None:
            units.append(note)

    for transcript in input.transcript_segments:
        unit = _build_unit(
            input,
            EvidenceLocatorKind.TRANSCRIPT_SEGMENT,
            transcript.value,
            transcript.text,
        )
        if unit is not None:
            units.append(unit)

    return tuple(units)


def render_index_markdown(units: Sequence[EvidenceUnit]) -> str:
    """Render full evidence text with deterministic provider markers."""
    blocks = [
        "\n".join(
            (
                f"[EVIDENCE:{unit.evidence_id}]",
                f"[SOURCE_REVISION:{unit.source_revision_id}]",
                f"[AUTHORITY:{unit.authority_class.value}]",
                f"[LOCATION:{unit.locator.kind.value} {unit.locator.value}]",
                "",
                unit.normalized_text,
            )
        )
        for unit in units
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _unit_from_parsed_segment(
    input: CourseRevisionInput, segment: ParsedSegment
) -> EvidenceUnit | None:
    kind, value = _parsed_locator(segment)
    image_asset_id = None
    if segment.kind is SegmentKind.IMAGE and len(segment.asset_keys) == 1:
        image_asset_id = segment.asset_keys[0]
    return _build_unit(input, kind, value, segment.text, image_asset_id=image_asset_id)


def _parsed_locator(segment: ParsedSegment) -> tuple[EvidenceLocatorKind, str]:
    if segment.kind is SegmentKind.NOTE:
        kind = EvidenceLocatorKind.SPEAKER_NOTE
    elif segment.kind is SegmentKind.TABLE:
        kind = EvidenceLocatorKind.TABLE
    elif segment.kind is SegmentKind.IMAGE:
        kind = EvidenceLocatorKind.FIGURE
    elif segment.locator.slide_number is not None:
        kind = EvidenceLocatorKind.SLIDE
    elif segment.locator.page_number is not None:
        kind = EvidenceLocatorKind.PAGE
    else:
        kind = EvidenceLocatorKind.SECTION
    return kind, _locator_value(segment.locator)


def _locator_value(locator: DocumentLocator) -> str:
    if locator.slide_number is not None:
        return str(locator.slide_number)
    if locator.page_number is not None:
        return str(locator.page_number)
    if locator.block_index is not None:
        return str(locator.block_index)
    return locator.label


def _build_unit(
    input: CourseRevisionInput,
    kind: EvidenceLocatorKind,
    value: str,
    text: str,
    *,
    image_asset_id: str | None = None,
) -> EvidenceUnit | None:
    normalized_text = _normalize_text(text)
    if not normalized_text:
        return None
    content_sha256 = sha256_text(normalized_text)
    return EvidenceUnit(
        evidence_id=make_evidence_id(
            input.source_revision_id,
            f"{kind.value}:{value}",
            content_sha256,
        ),
        source_revision_id=input.source_revision_id,
        authority_class=AuthorityClass.COURSE_MATERIAL,
        course_id=input.course_id,
        exam_id=input.exam_id,
        lecture_id=input.lecture_id,
        locator=EvidenceLocator(kind=kind, value=value),
        normalized_text=normalized_text,
        content_sha256=content_sha256,
        image_asset_id=image_asset_id,
    )


def _normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()
