import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from oms_hub.anki.domain import SourceKind
from oms_hub.ingestion.domain import StudyRevision, UploadKind

EXTRACTION_VERSION = "source-passages-v1"
ExtractionStatus = Literal["text", "vision", "vision_unavailable"]

_SPACE = re.compile(r"\s+")
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
_TIME = r"(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d+)?"
_TIMESTAMP_RANGE = re.compile(
    rf"^\s*(?P<start>{_TIME})\s*-->\s*(?P<end>{_TIME})\s*$"
)
_TIMESTAMP_PREFIX = re.compile(
    rf"^\s*\[?(?P<start>{_TIME})\]?\s+(?P<text>.+?)\s*$"
)


@dataclass(frozen=True, slots=True)
class SourcePassage:
    passage_id: str
    revision_id: int
    lecture_id: int
    artifact_id: str
    source_kind: SourceKind
    locator: str
    text: str
    content_hash: str
    extraction_status: ExtractionStatus
    slide_number: int | None = None
    start_seconds: float | None = None
    end_seconds: float | None = None

    @classmethod
    def create(
        cls,
        *,
        revision_id: int,
        lecture_id: int,
        artifact_id: str,
        source_kind: SourceKind,
        locator: str,
        text: str,
        extraction_status: ExtractionStatus = "text",
        slide_number: int | None = None,
        start_seconds: float | None = None,
        end_seconds: float | None = None,
    ) -> "SourcePassage":
        normalized = _normalize_text(text)
        if revision_id <= 0 or lecture_id <= 0:
            raise ValueError("source identifiers must be positive")
        if not artifact_id.strip() or not locator.strip():
            raise ValueError("source artifact and locator cannot be blank")
        if not normalized and extraction_status != "vision_unavailable":
            raise ValueError("source passage text cannot be blank")
        if slide_number is not None and slide_number <= 0:
            raise ValueError("slide number must be positive")
        if start_seconds is not None and start_seconds < 0:
            raise ValueError("transcript start cannot be negative")
        if end_seconds is not None and (
            end_seconds < 0
            or (
                start_seconds is not None
                and end_seconds < start_seconds
            )
        ):
            raise ValueError("transcript end is invalid")
        content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        identity = "\0".join(
            (
                EXTRACTION_VERSION,
                str(revision_id),
                source_kind.value,
                locator.strip(),
                extraction_status,
                content_hash,
            )
        )
        return cls(
            passage_id=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            revision_id=revision_id,
            lecture_id=lecture_id,
            artifact_id=artifact_id.strip(),
            source_kind=source_kind,
            locator=locator.strip(),
            text=normalized,
            content_hash=content_hash,
            extraction_status=extraction_status,
            slide_number=slide_number,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
        )

    @property
    def citation(self) -> str:
        if self.slide_number is not None:
            suffix = (
                " speaker notes"
                if self.source_kind is SourceKind.SPEAKER_NOTES
                else ""
            )
            return (
                f"Lecture {self.lecture_id}, slide "
                f"{self.slide_number}{suffix}"
            )
        if self.start_seconds is not None:
            interval = _format_time(self.start_seconds)
            if self.end_seconds is not None:
                interval = (
                    f"{interval}\N{EN DASH}{_format_time(self.end_seconds)}"
                )
            return f"Lecture {self.lecture_id}, transcript {interval}"
        return f"Lecture {self.lecture_id}, transcript"


class RevisionRepository(Protocol):
    def get_study_revision(
        self,
        revision_id: int,
    ) -> StudyRevision: ...


class SlideVisionExtractor(Protocol):
    def describe_slide(
        self,
        presentation_path: Path,
        slide_number: int,
    ) -> str | None: ...


@dataclass(frozen=True, slots=True)
class _TranscriptUnit:
    text: str
    start_seconds: float | None
    end_seconds: float | None


class LectureSourceExtractor:
    def __init__(
        self,
        revisions: RevisionRepository,
        *,
        vision: SlideVisionExtractor | None = None,
        transcript_max_chars: int = 800,
        transcript_overlap_sentences: int = 1,
    ) -> None:
        if transcript_max_chars < 50:
            raise ValueError("transcript passage size is too small")
        if transcript_overlap_sentences < 0:
            raise ValueError("transcript overlap cannot be negative")
        self.revisions = revisions
        self.vision = vision
        self.transcript_max_chars = transcript_max_chars
        self.transcript_overlap_sentences = (
            transcript_overlap_sentences
        )

    def extract(
        self,
        revision_ids: Sequence[int],
    ) -> list[SourcePassage]:
        if len(set(revision_ids)) != len(revision_ids):
            raise ValueError("source revision IDs must be unique")
        passages: list[SourcePassage] = []
        for revision_id in revision_ids:
            revision = self.revisions.get_study_revision(revision_id)
            if revision.kind is UploadKind.SLIDES:
                passages.extend(self._extract_slides(revision))
            elif revision.kind is UploadKind.TRANSCRIPTS:
                passages.extend(self._extract_transcript(revision))
            else:
                raise ValueError(
                    f"unsupported source revision kind: {revision.kind}"
                )
        return passages

    def _extract_slides(
        self,
        revision: StudyRevision,
    ) -> list[SourcePassage]:
        path = revision.immutable_source_path
        if not path.is_file():
            raise FileNotFoundError(path)
        presentation = Presentation(str(path))
        passages: list[SourcePassage] = []
        for slide_number, slide in enumerate(
            presentation.slides,
            start=1,
        ):
            text = _normalize_text(
                " ".join(
                    shape.text
                    for shape in slide.shapes
                    if getattr(shape, "has_text_frame", False)
                    and getattr(shape, "text", "").strip()
                )
            )
            if text:
                passages.append(
                    SourcePassage.create(
                        revision_id=revision.id,
                        lecture_id=revision.lecture_id,
                        artifact_id=revision.upload_item_id,
                        source_kind=SourceKind.SLIDE,
                        locator=f"slide:{slide_number}",
                        text=text,
                        slide_number=slide_number,
                    )
                )
            notes = ""
            if slide.has_notes_slide:
                notes = _normalize_text(
                    slide.notes_slide.notes_text_frame.text
                )
            if notes:
                passages.append(
                    SourcePassage.create(
                        revision_id=revision.id,
                        lecture_id=revision.lecture_id,
                        artifact_id=revision.upload_item_id,
                        source_kind=SourceKind.SPEAKER_NOTES,
                        locator=f"slide:{slide_number}:notes",
                        text=notes,
                        slide_number=slide_number,
                    )
                )
            has_image = any(
                shape.shape_type is MSO_SHAPE_TYPE.PICTURE
                for shape in slide.shapes
            )
            if not text and has_image:
                description = (
                    self.vision.describe_slide(path, slide_number)
                    if self.vision is not None
                    else None
                )
                passages.append(
                    SourcePassage.create(
                        revision_id=revision.id,
                        lecture_id=revision.lecture_id,
                        artifact_id=revision.upload_item_id,
                        source_kind=SourceKind.VISION,
                        locator=f"slide:{slide_number}:image",
                        text=description or "",
                        extraction_status=(
                            "vision"
                            if description and description.strip()
                            else "vision_unavailable"
                        ),
                        slide_number=slide_number,
                    )
                )
        return passages

    def _extract_transcript(
        self,
        revision: StudyRevision,
    ) -> list[SourcePassage]:
        path = (
            revision.immutable_derived_path
            if revision.immutable_derived_path is not None
            and revision.immutable_derived_path.is_file()
            else revision.immutable_source_path
        )
        raw_text = path.read_text(encoding="utf-8")
        units = _transcript_units(raw_text)
        windows = _overlapping_windows(
            units,
            max_chars=self.transcript_max_chars,
            overlap=self.transcript_overlap_sentences,
        )
        passages: list[SourcePassage] = []
        for position, window in enumerate(windows, start=1):
            start = next(
                (
                    unit.start_seconds
                    for unit in window
                    if unit.start_seconds is not None
                ),
                None,
            )
            end = next(
                (
                    unit.end_seconds
                    for unit in reversed(window)
                    if unit.end_seconds is not None
                ),
                None,
            )
            time_locator = (
                f"{_compact_time(start)}-{_compact_time(end)}"
                if start is not None
                else "untimed"
            )
            passages.append(
                SourcePassage.create(
                    revision_id=revision.id,
                    lecture_id=revision.lecture_id,
                    artifact_id=revision.upload_item_id,
                    source_kind=SourceKind.TRANSCRIPT,
                    locator=f"transcript:{position}:{time_locator}",
                    text=" ".join(unit.text for unit in window),
                    start_seconds=start,
                    end_seconds=end,
                )
            )
        return passages


def _transcript_units(value: str) -> list[_TranscriptUnit]:
    units: list[_TranscriptUnit] = []
    pending_range: tuple[float, float] | None = None
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        range_match = _TIMESTAMP_RANGE.fullmatch(line)
        if range_match is not None:
            pending_range = (
                _parse_time(range_match.group("start")),
                _parse_time(range_match.group("end")),
            )
            continue
        if line.isdigit() and pending_range is None:
            continue
        prefix_match = _TIMESTAMP_PREFIX.fullmatch(line)
        if prefix_match is not None:
            start = _parse_time(prefix_match.group("start"))
            end = None
            text = prefix_match.group("text")
        elif pending_range is not None:
            start, end = pending_range
            pending_range = None
            text = line
        else:
            start = None
            end = None
            text = line
        sentences = [
            _normalize_text(sentence)
            for sentence in _SENTENCE_BOUNDARY.split(text)
            if _normalize_text(sentence)
        ]
        units.extend(
            _TranscriptUnit(
                text=sentence,
                start_seconds=start,
                end_seconds=end,
            )
            for sentence in sentences
        )
    for index, unit in enumerate(units[:-1]):
        next_start = units[index + 1].start_seconds
        if unit.end_seconds is None and next_start is not None:
            units[index] = replace(unit, end_seconds=next_start)
    return units


def _overlapping_windows(
    units: Sequence[_TranscriptUnit],
    *,
    max_chars: int,
    overlap: int,
) -> list[tuple[_TranscriptUnit, ...]]:
    windows: list[tuple[_TranscriptUnit, ...]] = []
    start = 0
    while start < len(units):
        end = start
        characters = 0
        while end < len(units):
            added = len(units[end].text) + (1 if end > start else 0)
            if end > start and characters + added > max_chars:
                break
            characters += added
            end += 1
        windows.append(tuple(units[start:end]))
        if end >= len(units):
            break
        start = max(start + 1, end - overlap)
    return windows


def _parse_time(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    seconds = float(parts[-1])
    minutes = int(parts[-2])
    hours = int(parts[-3]) if len(parts) == 3 else 0
    return hours * 3_600 + minutes * 60 + seconds


def _format_time(value: float) -> str:
    total = int(value)
    hours, remainder = divmod(total, 3_600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _compact_time(value: float | None) -> str:
    return "open" if value is None else str(int(value))


def _normalize_text(value: str) -> str:
    return _SPACE.sub(" ", value).strip()
