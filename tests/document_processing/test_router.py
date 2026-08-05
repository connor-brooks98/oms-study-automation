from pathlib import Path

import pytest

from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
    SourceSnapshot,
)
from oms_hub.document_processing.router import DocumentProcessorRouter, ParserMode


def _snapshot(tmp_path: Path, suffix: str = ".txt") -> SourceSnapshot:
    source = tmp_path / f"questions{suffix}"
    source.write_text("Question", encoding="utf-8")
    return SourceSnapshot(
        id="source-1",
        title="Questions",
        path=source,
        media_type="text/plain",
        sha256="a" * 64,
    )


class TextFixtureProcessor:
    name = "text-fixture"
    version = "1"

    def __init__(self, warning: str | None = None) -> None:
        self.warning = warning
        self.calls = 0

    def supports(self, snapshot: SourceSnapshot) -> bool:
        return snapshot.path.suffix == ".txt"

    def parse(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument:
        self.calls += 1
        return ParsedDocument(
            source_id=snapshot.id,
            source_sha256=snapshot.sha256,
            source_format="txt",
            parser_name=self.name,
            parser_version=self.version,
            segments=(
                ParsedSegment(
                    "block-1",
                    SegmentKind.PARAGRAPH,
                    "Question",
                    DocumentLocator("block 1", block_index=1),
                ),
            ),
            assets=(),
            warnings=(self.warning,) if self.warning else (),
        )


class RaisingProcessor(TextFixtureProcessor):
    name = "raising"

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def parse(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument:
        self.calls += 1
        raise RuntimeError(self.message)


def test_router_uses_legacy_fallback_without_calling_primary(tmp_path: Path) -> None:
    primary = RaisingProcessor("should not run")
    fallback = TextFixtureProcessor()
    parsed = DocumentProcessorRouter(
        primary=primary,
        fallbacks=(fallback,),
        mode=ParserMode.LEGACY,
    ).parse(_snapshot(tmp_path), tmp_path / "assets")

    assert parsed.parser_name == "text-fixture"
    assert primary.calls == 0
    assert fallback.calls == 1


def test_router_runs_primary_in_anydoc_mode(tmp_path: Path) -> None:
    primary = TextFixtureProcessor()
    fallback = RaisingProcessor("fallback should not run")
    parsed = DocumentProcessorRouter(
        primary=primary,
        fallbacks=(fallback,),
        mode=ParserMode.ANYDOC,
    ).parse(_snapshot(tmp_path), tmp_path / "assets")

    assert parsed.parser_name == "text-fixture"
    assert primary.calls == 1
    assert fallback.calls == 0


def test_router_falls_back_and_records_primary_failure(tmp_path: Path) -> None:
    router = DocumentProcessorRouter(
        primary=RaisingProcessor("anydoc failed"),
        fallbacks=(TextFixtureProcessor(),),
        mode=ParserMode.ANYDOC,
    )
    parsed = router.parse(_snapshot(tmp_path, ".txt"), tmp_path / "assets")
    assert parsed.parser_name == "text-fixture"
    assert parsed.warnings == ("primary parser failed: anydoc failed",)


def test_router_keeps_legacy_result_and_records_shadow_failure(tmp_path: Path) -> None:
    parsed = DocumentProcessorRouter(
        primary=RaisingProcessor("anydoc failed"),
        fallbacks=(TextFixtureProcessor(),),
        mode=ParserMode.SHADOW,
    ).parse(_snapshot(tmp_path), tmp_path / "assets")

    assert parsed.parser_name == "text-fixture"
    assert parsed.warnings == ("shadow parser failed: anydoc failed",)


def test_router_preserves_primary_failure_when_all_fallbacks_fail(tmp_path: Path) -> None:
    router = DocumentProcessorRouter(
        primary=RaisingProcessor("primary failed"),
        fallbacks=(RaisingProcessor("fallback failed"),),
        mode=ParserMode.ANYDOC,
    )

    with pytest.raises(RuntimeError, match="fallback failed") as error:
        router.parse(_snapshot(tmp_path), tmp_path / "assets")

    assert error.value.__cause__ is not None
    assert str(error.value.__cause__) == "primary failed"


def test_router_rejects_unsupported_source_when_no_processor_matches(tmp_path: Path) -> None:
    router = DocumentProcessorRouter(
        primary=TextFixtureProcessor(),
        fallbacks=(),
        mode=ParserMode.LEGACY,
    )

    with pytest.raises(ValueError, match="no document processor supports"):
        router.parse(_snapshot(tmp_path, ".pdf"), tmp_path / "assets")
