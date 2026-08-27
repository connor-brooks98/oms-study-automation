"""Read-only schema-29 projection into the approved CP-0002 indexing view."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from oms_hub.document_processing.shadow import LegacyPptxProcessor
from oms_hub.knowledge.backfill import SlideRevisionBackfill
from oms_hub.knowledge.models import EvidenceUnit, SourceRevision, SourceRevisionState
from oms_hub.knowledge.service import IndexInputView, KnowledgeService
from oms_hub.providers.contracts import AuthorityClass

__all__ = ["project_schema29_index_input"]


@dataclass(frozen=True, slots=True)
class _ProjectedKnowledge:
    revision: SourceRevision
    evidence: tuple[EvidenceUnit, ...]
    source_authority: AuthorityClass = AuthorityClass.COURSE_MATERIAL

    def get_revision(self, revision_id: str) -> SourceRevision | None:
        return self.revision if revision_id == self.revision.source_revision_id else None

    def list_evidence(self, revision_id: str) -> tuple[EvidenceUnit, ...]:
        return self.evidence if revision_id == self.revision.source_revision_id else ()


class _ScratchArtifacts:
    def __init__(self, artifacts: Any, materialization_root: Path) -> None:
        self._artifacts = artifacts
        self.repository = artifacts.repository
        self.catalog = artifacts.catalog
        self.settings = SimpleNamespace(data_dir=materialization_root)

    def resolve(self, revision_id: int, role: object) -> object:
        return self._artifacts.resolve(revision_id, role)


class _ReadOnlySlidePreparation(SlideRevisionBackfill):
    def __init__(self, ingestion: Any, catalog: Any, parser: Any) -> None:
        self.ingestion = ingestion
        self.catalog = catalog
        self.parser = parser

    def prepare(self, slide_revision_id: str) -> Any:
        return self._prepare(slide_revision_id)


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _prepare_descendant(root: Path, relative: Path) -> Path:
    current = root
    for part in relative.parts:
        current = current / part
        if _is_link_or_reparse(current):
            raise ValueError("materialization path contains a link or reparse point")
        current.mkdir(mode=0o700, exist_ok=True)
        if _is_link_or_reparse(current) or not current.is_dir():
            raise ValueError("materialization path contains a link or reparse point")
        try:
            current.resolve().relative_to(root)
        except ValueError:
            raise ValueError("materialization path escapes the scratch root") from None
    return current


def project_schema29_index_input(
    slide_revision_id: str,
    *,
    schema_version: int,
    ingestion: Any,
    catalog: Any,
    artifacts: Any,
    materialization_root: Path,
    parser: Any | None = None,
) -> IndexInputView:
    """Project one current schema-29 slide revision without Source Trust writes."""

    if schema_version != 29 or isinstance(schema_version, bool):
        raise ValueError("source-trust compatibility requires schema 29")
    root = Path(materialization_root)
    if not root.is_absolute() or not root.is_dir() or root.resolve() != root:
        raise ValueError("materialization root must be an existing canonical directory")
    _prepare_descendant(root, Path("index-inputs"))

    resolved_parser = parser or LegacyPptxProcessor()
    candidate = _ReadOnlySlidePreparation(
        ingestion, catalog, resolved_parser
    ).prepare(slide_revision_id)
    markdown_parent = _prepare_descendant(
        root,
        Path("index-inputs") / candidate.source_revision_id,
    )
    if _is_link_or_reparse(markdown_parent / "normalized.md"):
        raise ValueError("materialization path contains a link or reparse point")
    projection = _ProjectedKnowledge(
        SourceRevision(
            source_document_id=candidate.source_document_id,
            source_revision_id=candidate.source_revision_id,
            file_sha256=candidate.revision.source_sha256,
            state=SourceRevisionState.READY,
        ),
        candidate.evidence,
    )
    return KnowledgeService(
        projection,
        _ScratchArtifacts(artifacts, root),
        parser=resolved_parser,
    ).resolve_index_input(candidate.source_revision_id)
