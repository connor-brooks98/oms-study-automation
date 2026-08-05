"""Strict conversion of stored plain and lightweight structured text."""

import re
from pathlib import Path

from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
    SourceSnapshot,
)

_TEXT_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/x-yaml",
        "text/csv",
        "text/markdown",
        "text/plain",
        "text/xml",
        "text/yaml",
    }
)
_TEXT_SUFFIXES = frozenset(
    {".csv", ".json", ".md", ".markdown", ".txt", ".xml", ".yaml", ".yml"}
)


class TextProcessor:
    """Parse only local, explicitly text-like snapshots into paragraph blocks."""

    name = "text"
    version = "1"

    def supports(self, snapshot: SourceSnapshot) -> bool:
        return (
            _normalized_media_type(snapshot.media_type) in _TEXT_MEDIA_TYPES
            and snapshot.path.suffix.casefold() in _TEXT_SUFFIXES
        )

    def parse(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument:
        del asset_root
        if not self.supports(snapshot):
            raise ValueError(f"text processor does not support source {snapshot.path.name!r}")
        try:
            text = snapshot.path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("text source is not valid UTF-8") from error
        segments = tuple(
            ParsedSegment(
                key=f"block-{index}",
                kind=SegmentKind.PARAGRAPH,
                text=block.strip(),
                locator=DocumentLocator(f"block {index}", block_index=index),
            )
            for index, block in enumerate(re.split(r"\n\s*\n", text), start=1)
            if block.strip()
        )
        return ParsedDocument(
            source_id=snapshot.id,
            source_sha256=snapshot.sha256,
            source_format=snapshot.path.suffix.removeprefix(".").casefold() or "text",
            parser_name=self.name,
            parser_version=self.version,
            segments=segments,
            assets=(),
            warnings=(),
        )


def _normalized_media_type(media_type: str) -> str:
    return media_type.split(";", 1)[0].casefold().strip()
