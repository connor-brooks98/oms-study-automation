from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from oms_hub.artifacts import ArtifactRole
from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
)
from oms_hub.document_processing.shadow import LegacyPptxProcessor
from oms_hub.files.atomic import sha256_file
from oms_hub.ingestion.domain import StudyRevision, UploadKind
from oms_hub.knowledge.backfill import scope_ids
from oms_hub.knowledge.models import SourceRevisionState
from oms_hub.providers.contracts import AuthorityClass
from oms_hub.source_trust_schema29 import project_schema29_index_input


class _Parser:
    def __init__(self, document: ParsedDocument) -> None:
        self.document = document

    def parse(self, snapshot: object, asset_root: Path) -> ParsedDocument:
        del snapshot, asset_root
        return self.document


class _Ingestion:
    def __init__(self, revision: StudyRevision) -> None:
        self.revision = revision

    def get_study_revision(self, revision_id: int) -> StudyRevision | None:
        return self.revision if revision_id == self.revision.id else None


class _Catalog:
    def get_lecture(self, lecture_id: int) -> SimpleNamespace | None:
        if lecture_id != 13:
            return None
        return SimpleNamespace(
            id=13,
            subject="Synthetic Hematology",
            exam_number=2,
            lecture_number=13,
        )


class _Artifacts:
    def __init__(self, revision: StudyRevision, catalog: _Catalog) -> None:
        self.repository = _Ingestion(revision)
        self.catalog = catalog
        self.revision = revision

    def resolve(self, revision_id: int, role: ArtifactRole) -> SimpleNamespace:
        assert revision_id == self.revision.id
        path = (
            self.revision.canonical_source_path
            if role is ArtifactRole.PPTX
            else self.revision.canonical_derived_path
        )
        assert path is not None
        return SimpleNamespace(
            path=path,
            sha256=sha256_file(path),
            media_type=(
                "application/vnd.openxmlformats-officedocument.presentationml.presentation"
                if role is ArtifactRole.PPTX
                else "application/pdf"
            ),
        )


def _source(tmp_path: Path) -> tuple[StudyRevision, _Catalog, _Parser, _Artifacts]:
    pptx = tmp_path / "synthetic.pptx"
    pdf = tmp_path / "synthetic.pdf"
    pptx.write_bytes(b"synthetic-pptx")
    pdf.write_bytes(b"synthetic-pdf")
    source_sha256 = sha256_file(pptx)
    revision = StudyRevision(
        id=29,
        upload_item_id="synthetic-upload",
        lecture_id=13,
        kind=UploadKind.SLIDES,
        source_sha256=source_sha256,
        immutable_source_path=pptx,
        derived_sha256=sha256_file(pdf),
        immutable_derived_path=pdf,
        canonical_source_path=pptx,
        canonical_derived_path=pdf,
        icloud_path=None,
        prompt_sha256=None,
        state="current",
        current=True,
    )
    document = ParsedDocument(
        source_id="legacy-study-revision:29",
        source_sha256=source_sha256,
        source_format="pptx",
        parser_name=LegacyPptxProcessor.name,
        parser_version=LegacyPptxProcessor.version,
        segments=(
            ParsedSegment(
                key="slide-1-block-1",
                kind=SegmentKind.PARAGRAPH,
                text="Synthetic factor deficiency",
                locator=DocumentLocator("slide 1", slide_number=1, block_index=1),
            ),
        ),
        assets=(),
        warnings=(),
    )
    catalog = _Catalog()
    return revision, catalog, _Parser(document), _Artifacts(revision, catalog)


def test_schema29_projects_complete_cp0002_view_without_source_trust_tables(
    tmp_path: Path,
) -> None:
    revision, catalog, parser, artifacts = _source(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    view = project_schema29_index_input(
        "29",
        schema_version=29,
        ingestion=_Ingestion(revision),
        catalog=catalog,
        artifacts=artifacts,
        materialization_root=scratch,
        parser=parser,
    )

    assert view.source_document_id == "legacy-study-revision:29"
    assert view.source_revision_id.startswith("sr_")
    assert view.source_family == "legacy_slides"
    assert view.revision_state is SourceRevisionState.READY
    assert view.authority_class is AuthorityClass.COURSE_MATERIAL
    assert (view.course_id, view.exam_id, view.lecture_id) == scope_ids(
        "Synthetic Hematology", 2, 13
    )
    assert view.pptx.path == revision.canonical_source_path
    assert view.pdf.path == revision.canonical_derived_path
    assert view.markdown.path.is_relative_to(scratch)
    assert view.markdown.path.read_text(encoding="utf-8").endswith("\n")
    assert len(view.evidence_units) == 1
    assert view.evidence_units[0].normalized_text == "Synthetic factor deficiency"
    assert view.assets == ()


def test_projection_rejects_non_schema29_before_reading_sources(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    with pytest.raises(ValueError, match="schema 29"):
        project_schema29_index_input(
            "29",
            schema_version=25,
            ingestion=None,
            catalog=None,
            artifacts=None,
            materialization_root=scratch,
        )

    assert list(scratch.iterdir()) == []
