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
        segments = _slide_segments(presentation, asset_keys_by_location)
        return replace(
            parsed,
            segments=tuple(segments),
            assets=assets,
            warnings=(*parsed.warnings, *warnings),
        )


def _locate_assets(
    parsed_assets: tuple[ParsedAsset, ...], image_locations: dict[str, tuple[_ImageLocation, ...]]
) -> tuple[tuple[ParsedAsset, ...], dict[tuple[int, int], str], tuple[str, ...]]:
    assets: list[ParsedAsset] = []
    keys_by_location: dict[tuple[int, int], str] = {}
    warnings: list[str] = []
    for asset in parsed_assets:
        matches = image_locations.get(_normalize_part_name(asset.origin), ())
        if len(matches) == 1:
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
                keys_by_location[(match.slide_number, match.image_number)] = located.key
            continue
        assets.append(asset)
        if matches:
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
