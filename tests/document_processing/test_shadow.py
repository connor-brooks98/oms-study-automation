import hashlib
from pathlib import Path

from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
    SourceSnapshot,
)
from oms_hub.document_processing.shadow import DocumentShadowEvaluator


class FixtureProcessor:
    name = "legacy"
    version = "1"

    def supports(self, snapshot: SourceSnapshot) -> bool:
        return True

    def parse(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument:
        return ParsedDocument(
            source_id=snapshot.id,
            source_sha256=snapshot.sha256,
            source_format="pptx",
            parser_name=self.name,
            parser_version=self.version,
            segments=(
                ParsedSegment(
                    "slide-1",
                    SegmentKind.PARAGRAPH,
                    "Question text that must not appear in reports.",
                    DocumentLocator("slide 1", slide_number=1),
                ),
            ),
            assets=(),
            warnings=(),
        )


class RaisingProcessor(FixtureProcessor):
    name = "anydoc"

    def __init__(self, message: str) -> None:
        self.message = message

    def parse(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument:
        raise RuntimeError(self.message)


def _snapshot(tmp_path: Path) -> SourceSnapshot:
    path = tmp_path / "lecture.pptx"
    path.write_bytes(b"fixture deck")
    return SourceSnapshot(
        id="lecture-1",
        title="Lecture 1",
        path=path,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def test_comparison_records_candidate_failure_without_source_text(tmp_path: Path) -> None:
    evaluator = DocumentShadowEvaluator(RaisingProcessor("bad deck"), FixtureProcessor())

    comparison = evaluator.compare(_snapshot(tmp_path), tmp_path / "assets")
    report = comparison.report

    assert report["candidate_error"] == "bad deck"
    assert report["source_sha256"] == hashlib.sha256(b"fixture deck").hexdigest()
    assert report["legacy"]["normalized_text_sha256"]
    assert "Question text" not in str(report)
    assert report["promotion_blockers"] == ("candidate parser failed",)


def test_anydoc_primary_falls_back_to_legacy_with_degraded_report(tmp_path: Path) -> None:
    evaluator = DocumentShadowEvaluator(RaisingProcessor("bad deck"), FixtureProcessor())

    result = evaluator.parse_primary(_snapshot(tmp_path), tmp_path / "assets")

    assert result.document.parser_name == "legacy"
    assert result.degraded is True
    assert result.report["fallback_used"] is True
