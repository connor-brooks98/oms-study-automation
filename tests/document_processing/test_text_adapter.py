import hashlib
from pathlib import Path

from oms_hub.document_processing.domain import SegmentKind, SourceSnapshot
from oms_hub.document_processing.text_adapter import TextProcessor


def _snapshot(path: Path, media_type: str = "text/plain") -> SourceSnapshot:
    return SourceSnapshot(
        id="source-1",
        title="Questions",
        path=path,
        media_type=media_type,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def test_text_processor_emits_nonempty_paragraph_blocks_with_locators(tmp_path: Path) -> None:
    source = tmp_path / "questions.txt"
    source.write_text("First question.\n\n  \nSecond question.\n", encoding="utf-8")

    parsed = TextProcessor().parse(_snapshot(source), tmp_path / "assets")

    assert tuple(segment.key for segment in parsed.segments) == ("block-1", "block-2")
    assert tuple(segment.text for segment in parsed.segments) == (
        "First question.",
        "Second question.",
    )
    assert all(segment.kind is SegmentKind.PARAGRAPH for segment in parsed.segments)
    assert tuple(segment.locator.block_index for segment in parsed.segments) == (1, 2)


def test_text_processor_rejects_non_text_snapshots(tmp_path: Path) -> None:
    source = tmp_path / "questions.pdf"
    source.write_bytes(b"not a PDF")

    assert not TextProcessor().supports(_snapshot(source, "application/pdf"))
