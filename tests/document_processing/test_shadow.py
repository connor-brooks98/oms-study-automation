import hashlib
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from pytest import MonkeyPatch

from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedAsset,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
    SourceSnapshot,
)
from oms_hub.document_processing.router import ParserMode
from oms_hub.document_processing.shadow import DocumentShadowEvaluator
from tests.document_processing.pptx_factory import SlideFixture, build_pptx


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


class FixedProcessor(FixtureProcessor):
    def __init__(
        self,
        name: str,
        segments: tuple[ParsedSegment, ...],
        *,
        assets: tuple[ParsedAsset, ...] = (),
        warnings: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.segments = segments
        self.assets = assets
        self.warnings = warnings

    def parse(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument:
        return ParsedDocument(
            source_id=snapshot.id,
            source_sha256=snapshot.sha256,
            source_format="pptx",
            parser_name=self.name,
            parser_version="1",
            segments=self.segments,
            assets=self.assets,
            warnings=self.warnings,
        )


class FailIfCalledProcessor(FixtureProcessor):
    def __init__(self) -> None:
        self.calls = 0

    def parse(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument:
        self.calls += 1
        raise AssertionError("candidate must not run")


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


def _typed_report(report: dict[str, object]) -> dict[str, Any]:
    return cast(dict[str, Any], report)


def test_comparison_records_candidate_failure_without_source_text(tmp_path: Path) -> None:
    evaluator = DocumentShadowEvaluator(
        RaisingProcessor("Bearer https://example.test/private?token=secret"), FixtureProcessor()
    )

    comparison = evaluator.compare(_snapshot(tmp_path), tmp_path / "assets")
    report = _typed_report(comparison.report)

    assert report["candidate_error"] == "candidate_parse_failed"
    assert report["source_sha256"] == hashlib.sha256(b"fixture deck").hexdigest()
    assert report["legacy"]["normalized_text_sha256"]
    assert "Question text" not in str(report)
    assert "example.test" not in str(report)
    assert "Bearer" not in str(report)
    assert report["promotion_blockers"] == ("candidate parser failed",)


def test_anydoc_primary_falls_back_to_legacy_with_degraded_report(tmp_path: Path) -> None:
    evaluator = DocumentShadowEvaluator(RaisingProcessor("bad deck"), FixtureProcessor())

    result = evaluator.parse_primary(_snapshot(tmp_path), tmp_path / "assets")

    assert result.document.parser_name == "legacy"
    assert result.degraded is True
    report = _typed_report(result.report)
    assert report["fallback_used"] is True
    assert report["degraded"] is True


def test_anydoc_primary_falls_back_when_candidate_silently_loses_text(tmp_path: Path) -> None:
    legacy = FixtureProcessor()
    candidate = FixedProcessor(
        "anydoc",
        (
            ParsedSegment(
                "slide-1",
                SegmentKind.PARAGRAPH,
                "Different semantic result",
                DocumentLocator("slide 1", slide_number=1),
            ),
        ),
    )

    result = DocumentShadowEvaluator(candidate, legacy).parse_primary(
        _snapshot(tmp_path), tmp_path / "assets"
    )

    assert result.document.parser_name == "legacy"
    assert result.degraded is True
    assert "normalized text differs" in _typed_report(result.report)["promotion_blockers"]


def test_shadow_comparison_blocks_empty_candidate_and_candidate_warnings(tmp_path: Path) -> None:
    candidate = FixedProcessor("anydoc", (), warnings=("Bearer token secret-source-text",))

    report = _typed_report(
        DocumentShadowEvaluator(candidate, FixtureProcessor()).compare(
            _snapshot(tmp_path), tmp_path / "assets"
        ).report
    )

    assert "candidate has no segments" in report["promotion_blockers"]
    assert "candidate emitted warnings" in report["promotion_blockers"]
    assert "Bearer" not in str(report)


def test_shadow_comparison_blocks_reduced_coverage_and_content_types(tmp_path: Path) -> None:
    legacy = FixedProcessor(
        "legacy",
        (
            ParsedSegment(
                "p1",
                SegmentKind.PARAGRAPH,
                "same",
                DocumentLocator("p1", page_number=1, slide_number=1),
            ),
            ParsedSegment(
                "table",
                SegmentKind.TABLE,
                "table",
                DocumentLocator("p2", page_number=2, slide_number=2),
            ),
            ParsedSegment(
                "note",
                SegmentKind.NOTE,
                "notes",
                DocumentLocator("p2", page_number=2, slide_number=2),
            ),
        ),
        assets=(
            ParsedAsset(
                "asset-1",
                None,
                "image/png",
                "a" * 64,
                DocumentLocator("p2", page_number=2, slide_number=2),
            ),
        ),
    )
    candidate = FixedProcessor(
        "anydoc",
        (
            ParsedSegment(
                "p1",
                SegmentKind.PARAGRAPH,
                "same",
                DocumentLocator("p1", page_number=1, slide_number=1),
            ),
        ),
    )

    report = _typed_report(
        DocumentShadowEvaluator(candidate, legacy).compare(
            _snapshot(tmp_path), tmp_path / "assets"
        ).report
    )

    blockers = set(report["promotion_blockers"])
    assert {
        "normalized text differs",
        "candidate reduced legacy page coverage",
        "candidate reduced legacy slide coverage",
        "candidate has fewer notes",
        "candidate has fewer tables",
        "candidate has fewer assets",
    } <= blockers


def test_exceptional_report_has_full_metrics_and_degraded_anydoc_state(tmp_path: Path) -> None:
    report = _typed_report(
        DocumentShadowEvaluator(FixtureProcessor(), FixtureProcessor()).exceptional_report(
            _snapshot(tmp_path).sha256,
            ParserMode.ANYDOC,
            "document_evaluation_failed",
        )
    )

    for parser in ("legacy", "candidate"):
        assert set(report[parser]) == {
            "parser_name",
            "parser_version",
            "duration_ms",
            "segment_counts",
            "page_coverage",
            "slide_coverage",
            "notes",
            "tables",
            "assets",
            "warnings",
            "normalized_text_sha256",
            "error",
        }
    assert report["fallback_used"] is False
    assert report["degraded"] is True


def test_legacy_mode_does_not_call_candidate(tmp_path: Path) -> None:
    candidate = FailIfCalledProcessor()

    result = DocumentShadowEvaluator(candidate, FixtureProcessor()).parse(
        _snapshot(tmp_path), tmp_path / "assets", mode=ParserMode.LEGACY
    )

    assert result.document.parser_name == "legacy"
    assert candidate.calls == 0


def test_corpus_exits_one_for_blockers_and_ignores_non_pptx_sources(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    corpus = importlib.import_module("scripts.evaluate_anydoc_corpus")
    root = tmp_path / "corpus"
    root.mkdir()
    build_pptx(root / "Deck.pptx", slides=(SlideFixture("Title", "Body"),))
    (root / "notes.docx").write_bytes(b"must remain unsupported")

    class BlockingEvaluator:
        def __init__(self, *args: object) -> None:
            pass

        def compare(self, snapshot: SourceSnapshot, asset_root: Path) -> SimpleNamespace:
            del asset_root
            return SimpleNamespace(
                report={
                    "source_sha256": snapshot.sha256,
                    "promotion_blockers": ("candidate parser failed",),
                }
            )

        write_report = staticmethod(DocumentShadowEvaluator.write_report)

    monkeypatch.setattr(corpus, "DocumentShadowEvaluator", BlockingEvaluator)
    output = tmp_path / "report.json"

    assert corpus.evaluate_corpus(root, output) == 1
    report = output.read_text(encoding="utf-8")
    assert "Deck.pptx" in report
    assert "notes.docx" not in report
