from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from oms_hub.artifacts import ArtifactRole
from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedAsset,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
)
from oms_hub.knowledge.backfill import scope_ids
from oms_hub.knowledge.ids import sha256_file
from oms_hub.knowledge.models import (
    EvidenceLocator,
    EvidenceLocatorKind,
    EvidenceUnit,
    SourceRevision,
    SourceRevisionState,
)
from oms_hub.knowledge.normalization import CourseRevisionInput, normalize_course_revision
from oms_hub.providers.contracts import AuthorityClass, RetrievalScope, TruthMode


def _document(path: Path, source_revision_id: str) -> ParsedDocument:
    return ParsedDocument(
        source_id="legacy-study-revision:7",
        source_sha256=sha256_file(path),
        source_format="pptx",
        parser_name="legacy-pptx",
        parser_version="1",
        segments=(
            ParsedSegment(
                "slide-1-block-1",
                SegmentKind.PARAGRAPH,
                "Trusted slide text",
                DocumentLocator("slide 1", slide_number=1, block_index=1),
            ),
        ),
        assets=(),
        warnings=(),
    )


class _Parser:
    def __init__(self, document: ParsedDocument):
        self.document = document

    def parse(self, snapshot, asset_root):
        return self.document


class _Artifacts:
    def __init__(self, pptx: Path, pdf: Path):
        self.pptx = pptx
        self.pdf = pdf

    def resolve(self, revision_id: int, role: ArtifactRole):
        path = self.pptx if role is ArtifactRole.PPTX else self.pdf
        return SimpleNamespace(
            revision_id=revision_id,
            role=role,
            path=path,
            media_type="application/pdf" if role is ArtifactRole.PDF else "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )


class _Knowledge:
    def __init__(self, revision: SourceRevision, evidence: tuple[EvidenceUnit, ...]):
        self.revision = revision
        self.evidence = evidence
        self.database = None

    def get_revision(self, revision_id):
        return self.revision if revision_id == self.revision.source_revision_id else None

    def list_evidence(self, revision_id):
        return self.evidence if revision_id == self.revision.source_revision_id else ()


def _service(tmp_path: Path, *, state: SourceRevisionState = SourceRevisionState.READY):
    from oms_hub.knowledge.service import KnowledgeService

    pptx = tmp_path / "deck.pptx"
    pdf = tmp_path / "deck.pdf"
    pptx.write_bytes(b"pptx")
    pdf.write_bytes(b"pdf")
    source_id = "legacy-study-revision:7"
    revision_id = "sr_test"
    document = _document(pptx, revision_id)
    course_id, exam_id, lecture_id = scope_ids("Heme", 2, 7)
    evidence = normalize_course_revision(
        CourseRevisionInput(
            source_revision_id=revision_id,
            course_id=course_id,
            exam_id=exam_id,
            lecture_id=lecture_id,
            parsed_document=document,
        )
    )
    revision = SourceRevision(source_id, revision_id, sha256_file(pptx), state)
    service = KnowledgeService(
        _Knowledge(revision, evidence),
        _Artifacts(pptx, pdf),
        parser=_Parser(document),
    )
    return service, revision_id, evidence, (course_id, exam_id, lecture_id)


def test_resolve_index_input_returns_frozen_verified_opaque_view(tmp_path: Path):
    service, revision_id, evidence, scope = _service(tmp_path)

    view = service.resolve_index_input(revision_id)

    assert view.source_document_id == "legacy-study-revision:7"
    assert view.pptx.artifact_id == f"{revision_id}:pptx"
    assert view.pdf.artifact_id == f"{revision_id}:pdf"
    assert view.pptx.path == tmp_path / "deck.pptx"
    assert view.pdf.sha256 == sha256_file(tmp_path / "deck.pdf")
    assert view.evidence_units == evidence
    assert (view.course_id, view.exam_id, view.lecture_id) == scope
    with pytest.raises(FrozenInstanceError):
        view.course_id = "changed"


def test_resolve_index_input_fails_closed_for_unsupported_state(tmp_path: Path):
    service, revision_id, _, _ = _service(tmp_path, state=SourceRevisionState.FAILED)

    with pytest.raises(Exception, match="state"):
        service.resolve_index_input(revision_id)


def test_resolve_evidence_maps_only_positive_slide_to_pdf_preview(tmp_path: Path):
    service, _, evidence, _ = _service(tmp_path)

    view = service.resolve_evidence(evidence[0].evidence_id)

    assert view.excerpt == "Trusted slide text"
    assert view.preview.page_number == 1
    assert view.preview.artifact_id == "7"
    assert not hasattr(view.preview, "path")

    bad = EvidenceUnit(
        "ev_note",
        evidence[0].source_revision_id,
        AuthorityClass.COURSE_MATERIAL,
        evidence[0].course_id,
        evidence[0].exam_id,
        evidence[0].lecture_id,
        EvidenceLocator(EvidenceLocatorKind.SPEAKER_NOTE, "1"),
        "note",
        "".join(__import__("hashlib").sha256(b"note").hexdigest()),
    )
    service.knowledge.evidence = evidence + (bad,)
    with pytest.raises(Exception, match="preview"):
        service.resolve_evidence("ev_note")


def test_scope_sources_validates_scope_and_is_deterministic(tmp_path: Path):
    service, revision_id, evidence, scope = _service(tmp_path)
    service.knowledge.database = SimpleNamespace(engine=None)
    # A repository-backed scope query is exercised by route/integration tests;
    # this unit verifies the public policy boundary and ordering contract.
    service._scope_rows = evidence + evidence

    result = service.get_scope_sources(
        RetrievalScope(scope[0], scope[1], (scope[2],), TruthMode.COURSE_ONLY)
    )

    assert result.evidence == tuple(sorted(evidence, key=lambda unit: unit.evidence_id))
    with pytest.raises(Exception, match="RetrievalScope"):
        service.get_scope_sources("not-a-scope")


def test_mark_dependents_stale_is_fail_closed_without_mutation(tmp_path: Path):
    service, revision_id, _, _ = _service(tmp_path)

    with pytest.raises(Exception, match="provenance"):
        service.mark_dependents_stale(revision_id)
