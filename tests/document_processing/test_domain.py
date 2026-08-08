from pathlib import Path

import pytest

from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedAsset,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
    SourceSnapshot,
)


def _snapshot(tmp_path: Path) -> SourceSnapshot:
    source = tmp_path / "questions.txt"
    source.write_text("Question", encoding="utf-8")
    return SourceSnapshot(
        id="source-1",
        title="Questions",
        path=source,
        media_type="text/plain",
        sha256="a" * 64,
    )


def _segment(
    key: str = "s1", locator: DocumentLocator | None = None, asset_keys: tuple[str, ...] = ()
) -> ParsedSegment:
    return ParsedSegment(
        key,
        SegmentKind.PARAGRAPH,
        "Question",
        locator or DocumentLocator(label="block 1", block_index=1),
        asset_keys,
    )


def _parsed(
    tmp_path: Path,
    *,
    segments: tuple[ParsedSegment, ...] | None = None,
    assets: tuple[ParsedAsset, ...] = (),
    source_sha256: str = "a" * 64,
) -> ParsedDocument:
    _snapshot(tmp_path)
    return ParsedDocument(
        source_id="source-1",
        source_sha256=source_sha256,
        source_format="txt",
        parser_name="fixture",
        parser_version="1",
        segments=segments or (_segment(),),
        assets=assets,
        warnings=(),
    )


def test_source_snapshot_rejects_missing_source_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="source file is missing"):
        SourceSnapshot(
            id="source-1",
            title="Questions",
            path=tmp_path / "missing.txt",
            media_type="text/plain",
            sha256="a" * 64,
        )


def test_parsed_document_rejects_invalid_sha256(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="source SHA-256"):
        _parsed(tmp_path, source_sha256="not-a-sha")


def test_parsed_document_rejects_duplicate_segment_keys(tmp_path: Path) -> None:
    locator = DocumentLocator(label="slide 1", slide_number=1)
    segment = ParsedSegment("s1", SegmentKind.PARAGRAPH, "Question", locator)
    with pytest.raises(ValueError, match="segment keys must be unique"):
        _parsed(tmp_path, segments=(segment, segment))


def test_parsed_document_rejects_duplicate_asset_keys(tmp_path: Path) -> None:
    path = tmp_path / "image.png"
    path.write_bytes(b"image")
    asset = ParsedAsset("image-1", path, "image/png", "b" * 64, DocumentLocator("page 1"))
    with pytest.raises(ValueError, match="asset keys must be unique"):
        _parsed(tmp_path, assets=(asset, asset))


def test_parsed_document_rejects_unknown_asset_reference(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="references unknown asset"):
        _parsed(tmp_path, segments=(_segment(asset_keys=("missing",)),))


@pytest.mark.parametrize(
    "locator",
    (
        DocumentLocator("page 0", page_number=0),
        DocumentLocator("slide 0", slide_number=0),
    ),
)
def test_parsed_document_rejects_non_positive_page_or_slide_number(
    tmp_path: Path, locator: DocumentLocator
) -> None:
    with pytest.raises(ValueError, match="page and slide numbers must be positive"):
        _parsed(tmp_path, segments=(_segment(locator=locator),))


def test_parsed_document_rejects_missing_asset_file(tmp_path: Path) -> None:
    asset = ParsedAsset(
        "image-1",
        tmp_path / "missing.png",
        "image/png",
        "b" * 64,
        DocumentLocator("page 1", page_number=1),
    )
    with pytest.raises(ValueError, match="asset file is missing"):
        _parsed(tmp_path, assets=(asset,))
