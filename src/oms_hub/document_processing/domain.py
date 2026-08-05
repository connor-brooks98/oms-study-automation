"""Immutable, format-neutral document parsing contracts."""

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class SegmentKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    NOTE = "note"
    IMAGE = "image"


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """An immutable, local source payload acquired before parsing."""

    id: str
    title: str
    path: Path
    media_type: str
    sha256: str
    original_url: str | None = None

    def __post_init__(self) -> None:
        if not self.path.is_file():
            raise ValueError("source file is missing")
        _validate_sha256(self.sha256, "source SHA-256")


@dataclass(frozen=True, slots=True)
class DocumentLocator:
    """The best available source location for content or an asset."""

    label: str
    page_number: int | None = None
    slide_number: int | None = None
    block_index: int | None = None


@dataclass(frozen=True, slots=True)
class ParsedAsset:
    """An extracted asset, or diagnostic metadata when ``path`` is absent."""

    key: str
    path: Path | None
    media_type: str
    sha256: str
    locator: DocumentLocator
    width: int | None = None
    height: int | None = None
    origin: str | None = None

    def __post_init__(self) -> None:
        _validate_sha256(self.sha256, "asset SHA-256")


@dataclass(frozen=True, slots=True)
class ParsedSegment:
    """An ordered parsed content block and its local asset references."""

    key: str
    kind: SegmentKind
    text: str
    locator: DocumentLocator
    asset_keys: tuple[str, ...] = ()
    parent_key: str | None = None
    previous_key: str | None = None
    next_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset_keys", tuple(self.asset_keys))


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """A validated parser result for one immutable source snapshot."""

    source_id: str
    source_sha256: str
    source_format: str
    parser_name: str
    parser_version: str
    segments: tuple[ParsedSegment, ...]
    assets: tuple[ParsedAsset, ...]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "segments", tuple(self.segments))
        object.__setattr__(self, "assets", tuple(self.assets))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        _validate_sha256(self.source_sha256, "source SHA-256")

        segment_keys = tuple(segment.key for segment in self.segments)
        if len(segment_keys) != len(set(segment_keys)):
            raise ValueError("segment keys must be unique")

        asset_keys = tuple(asset.key for asset in self.assets)
        if len(asset_keys) != len(set(asset_keys)):
            raise ValueError("asset keys must be unique")

        known_assets = set(asset_keys)
        known_segments = set(segment_keys)
        for segment in self.segments:
            _validate_locator(segment.locator)
            for asset_key in segment.asset_keys:
                if asset_key not in known_assets:
                    raise ValueError(
                        f"segment {segment.key!r} references unknown asset {asset_key!r}"
                    )
            _validate_segment_reference(segment.parent_key, known_segments, "parent")
            _validate_segment_reference(segment.previous_key, known_segments, "previous")
            _validate_segment_reference(segment.next_key, known_segments, "next")
        for asset in self.assets:
            _validate_locator(asset.locator)
            if asset.path is not None and not asset.path.is_file():
                raise ValueError("asset file is missing")


class DocumentProcessor(Protocol):
    """A parser selected by the router for a compatible snapshot."""

    name: str
    version: str

    def supports(self, snapshot: SourceSnapshot) -> bool:
        raise NotImplementedError

    def parse(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument:
        raise NotImplementedError


def _validate_sha256(value: str, label: str) -> None:
    if not _SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase 64-character hexadecimal value")


def _validate_locator(locator: DocumentLocator) -> None:
    if (locator.page_number is not None and locator.page_number < 1) or (
        locator.slide_number is not None and locator.slide_number < 1
    ):
        raise ValueError("page and slide numbers must be positive")


def _validate_segment_reference(
    value: str | None, known_segments: set[str], relationship: str
) -> None:
    if value is not None and value not in known_segments:
        raise ValueError(f"{relationship} segment reference is unknown: {value!r}")
