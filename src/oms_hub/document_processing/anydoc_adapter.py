"""Lazy Anydoc adapter that maps its shared model onto canonical contracts."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from oms_hub.document_processing.assets import persist_asset
from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedAsset,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
    SourceSnapshot,
)
from oms_hub.document_processing.pptx_locator import PptxLocatorEnricher

type AnydocFormat = Literal[
    "doc", "docx", "odt", "ppt", "pptx", "rtf", "epub", "xlsx", "ods", "odp", "csv"
]


class AnydocProcessor:
    """Convert a supported local file through Anydoc without importing it at startup."""

    name = "anydoc"
    version = "0.1.5"

    def __init__(self, pptx_enricher: PptxLocatorEnricher) -> None:
        self.pptx_enricher = pptx_enricher

    def supports(self, snapshot: SourceSnapshot) -> bool:
        return format_from_snapshot(snapshot) is not None

    def parse(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument:
        import anydoc

        source_format = format_from_snapshot(snapshot)
        if source_format is None:
            raise ValueError(f"Anydoc does not support source {snapshot.path.name!r}")
        document = anydoc.to_document(snapshot.path.read_bytes(), format=source_format)
        parsed = convert_anydoc_document(snapshot, document, asset_root, source_format)
        return self.pptx_enricher.enrich(snapshot, parsed) if source_format == "pptx" else parsed


def format_from_snapshot(snapshot: SourceSnapshot) -> AnydocFormat | None:
    suffix = snapshot.path.suffix.casefold()
    formats: dict[str, AnydocFormat] = {
        ".doc": "doc",
        ".docx": "docx",
        ".odt": "odt",
        ".ppt": "ppt",
        ".pptx": "pptx",
        ".rtf": "rtf",
        ".epub": "epub",
        ".xlsx": "xlsx",
        ".ods": "ods",
        ".odp": "odp",
        ".csv": "csv",
    }
    return formats.get(suffix)


def convert_anydoc_document(
    snapshot: SourceSnapshot, document: Any, asset_root: Path, source_format: AnydocFormat
) -> ParsedDocument:
    assets, warnings = _convert_assets(document.assets, asset_root)
    segments = _convert_blocks(document.blocks, assets)
    segments.extend(_convert_notes(document.notes, len(segments)))
    return ParsedDocument(
        source_id=snapshot.id,
        source_sha256=snapshot.sha256,
        source_format=source_format,
        parser_name=AnydocProcessor.name,
        parser_version=AnydocProcessor.version,
        segments=tuple(segments),
        assets=tuple(assets),
        warnings=tuple(warnings),
    )


def _convert_assets(
    document_assets: Iterable[Any], asset_root: Path
) -> tuple[list[ParsedAsset], list[str]]:
    assets: list[ParsedAsset] = []
    warnings: list[str] = []
    for asset in document_assets:
        key = f"asset-{asset.id}"
        stored = persist_asset(asset_root, key, asset.media_type, asset.data)
        if stored.diagnostic:
            warnings.append(f"asset {key!r}: {stored.diagnostic}")
        assets.append(
            ParsedAsset(
                key=key,
                path=stored.path,
                media_type=stored.media_type,
                sha256=stored.sha256,
                locator=DocumentLocator(label=f"asset {asset.id}"),
                width=stored.width,
                height=stored.height,
                origin=asset.origin_part,
            )
        )
    return assets, warnings


def _convert_blocks(blocks: Iterable[Any], assets: Iterable[ParsedAsset]) -> list[ParsedSegment]:
    asset_keys = {asset.key for asset in assets}
    segments: list[ParsedSegment] = []
    for block in _walk_blocks(blocks):
        text = _block_text(block)
        block_index = len(segments) + 1
        references = tuple(
            f"asset-{source.asset_id}"
            for source in _image_sources(block)
            if source.asset_id is not None and f"asset-{source.asset_id}" in asset_keys
        )
        if references:
            segments.append(
                ParsedSegment(
                    key=f"block-{block_index}-image",
                    kind=SegmentKind.IMAGE,
                    text=text,
                    locator=DocumentLocator(label=f"block {block_index}", block_index=block_index),
                    asset_keys=references,
                )
            )
        if text:
            segments.append(
                ParsedSegment(
                    key=f"block-{len(segments) + 1}",
                    kind=_segment_kind(block.kind),
                    text=text,
                    locator=DocumentLocator(
                        label=f"block {len(segments) + 1}", block_index=len(segments) + 1
                    ),
                )
            )
    return segments


def _convert_notes(notes: Iterable[Any], offset: int) -> list[ParsedSegment]:
    result: list[ParsedSegment] = []
    for note in notes:
        text = "\n".join(filter(None, (_block_text(block) for block in note.blocks))).strip()
        if text:
            index = offset + len(result) + 1
            result.append(
                ParsedSegment(
                    key=f"note-{note.id}",
                    kind=SegmentKind.NOTE,
                    text=text,
                    locator=DocumentLocator(label=f"note {note.id}", block_index=index),
                )
            )
    return result


def _walk_blocks(blocks: Iterable[Any]) -> Iterable[Any]:
    for block in blocks:
        yield block
        nested = getattr(block, "blocks", None)
        if nested:
            yield from _walk_blocks(nested)
        listing = getattr(block, "list", None)
        if listing:
            for item in listing.items:
                yield from _walk_blocks(item.blocks)


def _block_text(block: Any) -> str:
    if block.text:
        return str(block.text).strip()
    if block.content:
        return _inline_text(block.content).strip()
    if block.table:
        return "\n".join(
            " | ".join(_table_cell_text(slot) for slot in row) for row in block.table.grid
        ).strip()
    return ""


def _table_cell_text(slot: Any) -> str:
    cell = slot.cell
    if cell is None:
        return ""
    return " ".join(filter(None, (_block_text(block) for block in cell.blocks)))


def _inline_text(inlines: Iterable[Any]) -> str:
    parts: list[str] = []
    for inline in inlines:
        if inline.text:
            parts.append(str(inline.text))
        elif inline.content:
            parts.append(_inline_text(inline.content))
        elif inline.kind == "line_break":
            parts.append("\n")
    return "".join(parts)


def _image_sources(block: Any) -> Iterable[Any]:
    for inline in block.content or ():
        if inline.kind == "image" and inline.source is not None:
            yield inline.source
        if inline.content:
            yield from _image_sources_from_inlines(inline.content)
    table = getattr(block, "table", None)
    if table:
        for row in table.grid:
            for slot in row:
                cell = slot.cell
                if cell is not None:
                    for cell_block in cell.blocks:
                        yield from _image_sources(cell_block)


def _image_sources_from_inlines(inlines: Iterable[Any]) -> Iterable[Any]:
    for inline in inlines:
        if inline.kind == "image" and inline.source is not None:
            yield inline.source
        if inline.content:
            yield from _image_sources_from_inlines(inline.content)


def _segment_kind(block_kind: str) -> SegmentKind:
    return {
        "heading": SegmentKind.HEADING,
        "list": SegmentKind.LIST_ITEM,
        "table": SegmentKind.TABLE,
    }.get(block_kind, SegmentKind.PARAGRAPH)
