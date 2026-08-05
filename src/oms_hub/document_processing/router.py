"""Mode-aware parser selection with explicit fallback diagnostics."""

from dataclasses import replace
from enum import StrEnum
from pathlib import Path

from oms_hub.document_processing.domain import DocumentProcessor, ParsedDocument, SourceSnapshot


class ParserMode(StrEnum):
    LEGACY = "legacy"
    SHADOW = "shadow"
    ANYDOC = "anydoc"


class DocumentProcessorRouter:
    """Route a snapshot to legacy, shadow, or Anydoc-primary processors."""

    def __init__(
        self,
        primary: DocumentProcessor,
        fallbacks: tuple[DocumentProcessor, ...],
        mode: ParserMode,
    ) -> None:
        self.primary = primary
        self.fallbacks = fallbacks
        self.mode = mode

    def parse(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument:
        if self.mode is ParserMode.LEGACY:
            return self._parse_fallback(snapshot, asset_root)
        if self.mode is ParserMode.SHADOW:
            return self._parse_shadow(snapshot, asset_root)
        return self._parse_primary(snapshot, asset_root)

    def _parse_primary(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument:
        if not self.primary.supports(snapshot):
            return self._parse_fallback(snapshot, asset_root)
        try:
            return self.primary.parse(snapshot, asset_root)
        except Exception as primary_error:
            try:
                fallback = self._parse_fallback(snapshot, asset_root)
            except Exception as fallback_error:
                raise fallback_error from primary_error
            return _with_warning(fallback, f"primary parser failed: {primary_error}")

    def _parse_shadow(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument:
        legacy = self._parse_fallback(snapshot, asset_root)
        if not self.primary.supports(snapshot):
            return legacy
        try:
            self.primary.parse(snapshot, asset_root)
        except Exception as primary_error:
            return _with_warning(legacy, f"shadow parser failed: {primary_error}")
        return legacy

    def _parse_fallback(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument:
        """Use the ordered format adapters; ``supports`` owns MIME/signature checks."""
        processors = tuple(
            processor for processor in self.fallbacks if processor.supports(snapshot)
        )
        if not processors:
            raise ValueError(f"no document processor supports source {snapshot.path.name!r}")

        last_error: Exception | None = None
        for processor in processors:
            try:
                return processor.parse(snapshot, asset_root)
            except Exception as error:
                last_error = error
        assert last_error is not None
        raise last_error


def _with_warning(parsed: ParsedDocument, warning: str) -> ParsedDocument:
    return replace(parsed, warnings=(*parsed.warnings, warning))
