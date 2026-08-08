"""PowerPoint-specific provenance restored from package relationships."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedAsset,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
    SourceSnapshot,
)


class PptxLocatorEnricher:
    """Restore unambiguous slide, notes, and image locations for an Anydoc result."""

    def enrich(self, snapshot: SourceSnapshot, parsed: ParsedDocument) -> ParsedDocument:
        presentation = Presentation(str(snapshot.path))
        image_locations = _image_locations(presentation)
        assets, asset_keys_by_location, warnings = _locate_assets(parsed.assets, image_locations)
        segments = _enrich_segments(
            parsed.segments,
            tuple(_slide_segments(presentation, asset_keys_by_location)),
            assets,
        )
        return replace(
            parsed,
            segments=segments,
            assets=assets,
            warnings=(*parsed.warnings, *warnings),
        )


def _enrich_segments(
    parsed_segments: tuple[ParsedSegment, ...],
    source_segments: tuple[ParsedSegment, ...],
    assets: tuple[ParsedAsset, ...],
) -> tuple[ParsedSegment, ...]:
    """Restore only unambiguous provenance; preserve Anydoc semantics verbatim."""
    asset_slides = {
        asset.key: asset.locator.slide_number
        for asset in assets
        if asset.locator.slide_number is not None
    }
    locations: dict[str, DocumentLocator] = {}
    for kind in SegmentKind:
        candidates = tuple(segment for segment in parsed_segments if segment.kind is kind)
        references = tuple(segment for segment in source_segments if segment.kind is kind)
        for candidate in candidates:
            if not candidate.text.strip():
                continue
            matching_locations = {
                reference.locator
                for reference in source_segments
                if _normalize_text(reference.text) == _normalize_text(candidate.text)
            }
            if len(matching_locations) == 1:
                locations[candidate.key] = matching_locations.pop()
        if any(candidate.key in locations for candidate in candidates):
            continue
        if len(candidates) == len(references):
            locations.update(
                {
                    candidate.key: reference.locator
                    for candidate, reference in zip(candidates, references, strict=True)
                }
            )
            continue
        reference_slides = tuple(
            dict.fromkeys(
                reference.locator.slide_number
                for reference in references
                if reference.locator.slide_number is not None
            )
        )
        if len(candidates) == len(reference_slides):
            locations.update(
                {
                    candidate.key: DocumentLocator(
                        label=f"slide {slide_number}", slide_number=slide_number
                    )
                    for candidate, slide_number in zip(
                        candidates, reference_slides, strict=True
                    )
                }
            )
    enriched: list[ParsedSegment] = []
    for segment in parsed_segments:
        if segment.key in locations:
            enriched.append(replace(segment, locator=locations[segment.key]))
            continue
        slides = {asset_slides[key] for key in segment.asset_keys if key in asset_slides}
        if len(slides) == 1:
            slide_number = next(iter(slides))
            enriched.append(
                replace(
                    segment,
                    locator=DocumentLocator(
                        label=f"slide {slide_number}", slide_number=slide_number
                    ),
                )
            )
            continue
        enriched.append(segment)
    # Some Anydoc PPTX outputs omit speaker notes.  Retain the candidate sequence
    # verbatim and append only the missing, source-proven note records.
    if not any(segment.kind is SegmentKind.NOTE for segment in parsed_segments):
        enriched.extend(
            segment
            for segment in source_segments
            if segment.kind is SegmentKind.NOTE
        )
    return tuple(enriched)


def _locate_assets(
    parsed_assets: tuple[ParsedAsset, ...], image_locations: dict[str, tuple[_ImageLocation, ...]]
) -> tuple[tuple[ParsedAsset, ...], dict[tuple[int, int], str], tuple[str, ...]]:
    assets: list[ParsedAsset] = []
    keys_by_location: dict[tuple[int, int], str] = {}
    warnings: list[str] = []
    for asset in parsed_assets:
        matches = image_locations.get(_normalize_part_name(asset.origin), ())
        slide_numbers = {match.slide_number for match in matches}
        if len(slide_numbers) == 1:
            match = matches[0]
            located = replace(
                asset,
                locator=DocumentLocator(
                    label=f"slide {match.slide_number} image {match.image_number}",
                    slide_number=match.slide_number,
                ),
            )
            assets.append(located)
            if located.path is not None:
                for occurrence in matches:
                    keys_by_location[(occurrence.slide_number, occurrence.image_number)] = (
                        located.key
                    )
            continue
        assets.append(asset)
        if slide_numbers:
            warnings.append(
                f"asset {asset.key!r} occurs on multiple slides and was not automatically bound"
            )
        else:
            warnings.append(f"asset {asset.key!r} could not be matched to a PowerPoint slide")
    return tuple(assets), keys_by_location, tuple(warnings)


class _ImageLocation:
    def __init__(self, slide_number: int, image_number: int) -> None:
        self.slide_number = slide_number
        self.image_number = image_number


def _image_locations(presentation: Any) -> dict[str, tuple[_ImageLocation, ...]]:
    locations: dict[str, list[_ImageLocation]] = {}
    for slide_number, slide in enumerate(presentation.slides, start=1):
        image_number = 0
        for shape in _walk_shapes(slide.shapes):
            if shape.shape_type is not MSO_SHAPE_TYPE.PICTURE:
                continue
            image_number += 1
            part_name = _relationship_image_part_name(slide, shape)
            if part_name is None:
                continue
            locations.setdefault(part_name, []).append(_ImageLocation(slide_number, image_number))
    return {part_name: tuple(values) for part_name, values in locations.items()}


def _relationship_image_part_name(slide: Any, shape: Any) -> str | None:
    relationship_id = shape._element.blip_rId  # pyright: ignore[reportPrivateUsage]
    if relationship_id is None:
        return None
    relationship = slide.part.rels.get(relationship_id)
    if relationship is None or relationship.target_part is None:
        return None
    return _normalize_part_name(str(relationship.target_part.partname))


def _slide_segments(
    presentation: Any, asset_keys_by_location: dict[tuple[int, int], str]
) -> Iterable[ParsedSegment]:
    for slide_number, slide in enumerate(presentation.slides, start=1):
        text_number = 0
        image_number = 0
        for shape in _walk_shapes(slide.shapes):
            if shape.shape_type is MSO_SHAPE_TYPE.PICTURE:
                image_number += 1
                key = asset_keys_by_location.get((slide_number, image_number))
                if key is not None:
                    yield ParsedSegment(
                        key=f"slide-{slide_number}-image-{image_number}",
                        kind=SegmentKind.IMAGE,
                        text="",
                        locator=DocumentLocator(
                            label=f"slide {slide_number} image {image_number}",
                            slide_number=slide_number,
                        ),
                        asset_keys=(key,),
                    )
                continue
            text, kind = _shape_content(shape)
            if not text:
                continue
            text_number += 1
            yield ParsedSegment(
                key=f"slide-{slide_number}-content-{text_number}",
                kind=kind,
                text=text,
                locator=DocumentLocator(
                    label=f"slide {slide_number} content {text_number}",
                    slide_number=slide_number,
                ),
            )
        notes = _notes_text(slide)
        if notes:
            yield ParsedSegment(
                key=f"slide-{slide_number}-notes",
                kind=SegmentKind.NOTE,
                text=notes,
                locator=DocumentLocator(
                    label=f"slide {slide_number} notes", slide_number=slide_number
                ),
            )


def _walk_shapes(shapes: Iterable[Any]) -> Iterable[Any]:
    for shape in shapes:
        if shape.shape_type is MSO_SHAPE_TYPE.GROUP:
            yield from _walk_shapes(shape.shapes)
        else:
            yield shape


def _shape_content(shape: Any) -> tuple[str, SegmentKind]:
    if shape.has_table:
        table = shape.table
        return (
            "\n".join(
                " | ".join(cell.text.strip() for cell in row.cells) for row in table.rows
            ).strip(),
            SegmentKind.TABLE,
        )
    if shape.has_text_frame:
        return shape.text.strip(), SegmentKind.PARAGRAPH
    return "", SegmentKind.PARAGRAPH


def _notes_text(slide: Any) -> str:
    try:
        return str(slide.notes_slide.notes_text_frame.text).strip()
    except (AttributeError, KeyError):
        return ""


def _normalize_part_name(origin: str | None) -> str:
    if not origin:
        return ""
    normalized = origin.replace("\\", "/").lstrip("/")
    return str(PurePosixPath(normalized))


def _normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())
