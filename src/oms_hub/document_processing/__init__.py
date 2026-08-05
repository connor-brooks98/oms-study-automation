"""Canonical contracts and routing for immutable document snapshots."""

from oms_hub.document_processing.domain import (
    DocumentLocator,
    DocumentProcessor,
    ParsedAsset,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
    SourceSnapshot,
)
from oms_hub.document_processing.router import DocumentProcessorRouter, ParserMode

__all__ = [
    "DocumentLocator",
    "DocumentProcessor",
    "DocumentProcessorRouter",
    "ParsedAsset",
    "ParsedDocument",
    "ParsedSegment",
    "ParserMode",
    "SegmentKind",
    "SourceSnapshot",
]
