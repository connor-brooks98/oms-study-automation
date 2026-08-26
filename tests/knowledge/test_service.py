from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from oms_hub.artifacts import ArtifactRole
from oms_hub.db import Database
from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedAsset,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
)
from oms_hub.knowledge.backfill import scope_ids
from oms_hub.knowledge.ids import sha256_file
from oms_hub.knowledge.ids import source_revision_id as make_revision_id
from oms_hub.knowledge.models import (
    EvidenceLocator,
    EvidenceLocatorKind,
    EvidenceUnit,
    SourceRevision,
    SourceRevisionState,
)
from oms_hub.knowledge.normalization import (
    CourseRevisionInput,
    normalize_course_revision,
    render_index_markdown,
)
from oms_hub.knowledge.repository import KnowledgeRepository
from oms_hub.knowledge.service import (
    KnowledgeIntegrityError,
    KnowledgeNotFoundError,
    KnowledgeService,
)
from oms_hub.providers.contracts import AuthorityClass, RetrievalScope, TruthMode


def _document(
    path: Path,
    source_revision_id: str,
    *,
    slide_number: int = 1,
    segments: tuple[ParsedSegment, ...] | None = None,
    assets: tuple[ParsedAsset, ...] = (),
) -> ParsedDocument:
    return ParsedDocument(
        source_id="legacy-study-revision:7",
        source_sha256=sha256_file(path),
        source_format="pptx",
        parser_name="legacy-pptx",
        parser_version="1",
        segments=segments
        or (
            ParsedSegment(
                f"slide-{slide_number}-block-1",
                SegmentKind.PARAGRAPH,
                "Trusted slide text",
                DocumentLocator(
                    f"slide {slide_number}",
                    slide_number=slide_number,
                    block_index=1,
                ),
            ),
        ),
        assets=assets,
        warnings=(),
    )


class _Parser:
    def __init__(self, document: ParsedDocument):
        self.document = document

    def parse(self, snapshot: object, asset_root: Path) -> ParsedDocument:
        del snapshot, asset_root
        return self.document


class _Artifacts:
    def __init__(self, pptx: Path, pdf: Path):
        self.pptx = pptx
        self.pdf = pdf
        self.legacy = SimpleNamespace(
            id=7,
            kind="slides",
            lecture_id=7,
            source_sha256=sha256_file(pptx),
            immutable_source_path=pptx,
            derived_sha256=sha256_file(pdf),
            immutable_derived_path=pdf,
            canonical_derived_path=pdf,
            current=True,
        )
        self.repository = SimpleNamespace(
            get_study_revision=lambda revision_id: self.legacy
        )
        self.catalog = SimpleNamespace(
            get_lecture=lambda lecture_id: SimpleNamespace(
                id=lecture_id,
                subject="Heme",
                exam_number=2,
                lecture_number=7,
            )
        )
        self.fail_pdf = False

    def resolve(self, revision_id: int, role: ArtifactRole) -> SimpleNamespace:
        if role is ArtifactRole.PDF and self.fail_pdf:
            from oms_hub.artifacts import ArtifactNotFound

            raise ArtifactNotFound("missing PDF")
        path = self.pptx if role is ArtifactRole.PPTX else self.pdf
        return SimpleNamespace(
            revision_id=revision_id,
            role=role,
            path=path,
            media_type=(
                "application/pdf"
                if role is ArtifactRole.PDF
                else "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            ),
        )


class _Knowledge:
    def __init__(self, revision: SourceRevision, evidence: tuple[EvidenceUnit, ...]):
        self.revision = revision
        self.evidence = evidence
        self.database = None
        self.source_authority = AuthorityClass.COURSE_MATERIAL

    def get_revision(self, revision_id: str) -> SourceRevision | None:
        return self.revision if revision_id == self.revision.source_revision_id else None

    def list_evidence(self, revision_id: str) -> tuple[EvidenceUnit, ...]:
        return self.evidence if revision_id == self.revision.source_revision_id else ()


def _service(
    tmp_path: Path,
    *,
    state: SourceRevisionState = SourceRevisionState.READY,
) -> tuple[KnowledgeService, str, tuple[EvidenceUnit, ...], tuple[str, str, str]]:

    pptx = tmp_path / "deck.pptx"
    pdf = tmp_path / "deck.pdf"
    pptx.write_bytes(b"pptx")
    pdf.write_bytes(b"pdf")
    source_id = "legacy-study-revision:7"
    revision_id = make_revision_id(source_id, sha256_file(pptx))
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


def test_resolve_index_input_returns_frozen_verified_opaque_view(tmp_path: Path) -> None:
    service, revision_id, evidence, scope = _service(tmp_path)

    view = service.resolve_index_input(revision_id)

    assert view.source_document_id == "legacy-study-revision:7"
    assert view.pptx.artifact_id == f"{revision_id}:pptx"
    assert view.pdf.artifact_id == f"{revision_id}:pdf"
    assert view.pptx.path == tmp_path / "deck.pptx"
    assert view.pdf.sha256 == sha256_file(tmp_path / "deck.pdf")
    assert view.markdown.path.is_file()
    assert view.markdown.path.read_text(encoding="utf-8") == render_index_markdown(evidence)
    assert view.markdown.sha256 == sha256_file(view.markdown.path)
    assert service.resolve_index_input(revision_id).markdown == view.markdown
    assert view.evidence_units == evidence
    assert (view.course_id, view.exam_id, view.lecture_id) == scope
    with pytest.raises(FrozenInstanceError):
        view.course_id = "changed"  # type: ignore[misc]


def test_index_asset_contract_keeps_explicit_semantics_dimensions_and_evidence(
    tmp_path: Path,
) -> None:
    asset_path = tmp_path / "figure.png"
    asset_path.write_bytes(b"image")
    pptx = tmp_path / "deck.pptx"
    pdf = tmp_path / "deck.pdf"
    pptx.write_bytes(b"pptx")
    pdf.write_bytes(b"pdf")
    source_id = "legacy-study-revision:7"
    revision_id = make_revision_id(source_id, sha256_file(pptx))
    asset = ParsedAsset(
        key="figure-1",
        path=asset_path,
        media_type="image/png",
        sha256=sha256_file(asset_path),
        locator=DocumentLocator("slide 1 figure 1", slide_number=1),
        width=640,
        height=480,
        visual_semantic=True,
    )
    segment = ParsedSegment(
        "slide-1-figure-1",
        SegmentKind.IMAGE,
        "Diagnostic morphology",
        asset.locator,
        asset_keys=(asset.key,),
    )
    document = _document(
        pptx,
        revision_id,
        segments=(segment,),
        assets=(asset,),
    )
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
    service = KnowledgeService(
        _Knowledge(
            SourceRevision(
                source_id,
                revision_id,
                sha256_file(pptx),
                SourceRevisionState.READY,
            ),
            evidence,
        ),
        _Artifacts(pptx, pdf),
        parser=_Parser(document),
    )

    view = service.resolve_index_input(revision_id)

    assert len(view.assets) == 1
    assert view.assets[0].visual_semantic is True
    assert (view.assets[0].width, view.assets[0].height) == (640, 480)
    assert view.assets[0].evidence_ids == (evidence[0].evidence_id,)


def test_resolve_index_input_fails_closed_for_unsupported_state(tmp_path: Path) -> None:
    service, revision_id, _, _ = _service(tmp_path, state=SourceRevisionState.FAILED)

    with pytest.raises(Exception, match="state"):
        service.resolve_index_input(revision_id)


def test_resolve_evidence_maps_only_positive_slide_to_pdf_preview(tmp_path: Path) -> None:
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


def test_scope_sources_validates_scope_and_is_deterministic(tmp_path: Path) -> None:
    service, revision_id, evidence, scope = _service(tmp_path)
    service.knowledge.database = SimpleNamespace(engine=None)
    # A repository-backed scope query is exercised by route/integration tests;
    # this unit verifies the public policy boundary and ordering contract.
    cast(Any, service)._scope_rows = evidence + evidence

    result = service.get_scope_sources(
        RetrievalScope(scope[0], scope[1], (scope[2],), TruthMode.COURSE_ONLY)
    )

    assert result.revisions[0].source_revision_id == revision_id
    assert result.revisions[0].upload_eligible is True
    with pytest.raises(Exception, match="RetrievalScope"):
        service.get_scope_sources(cast(Any, "not-a-scope"))


def test_mark_dependents_stale_is_fail_closed_without_mutation(tmp_path: Path) -> None:
    service, revision_id, _, _ = _service(tmp_path)

    with pytest.raises(Exception, match="provenance"):
        service.mark_dependents_stale(revision_id)


@pytest.mark.parametrize(
    "state, eligible",
    [(SourceRevisionState.STALE, False), (SourceRevisionState.RETIRED, False)],
)
def test_resolve_index_input_allows_reconcilable_states(
    tmp_path: Path, state: SourceRevisionState, eligible: bool
) -> None:
    service, revision_id, _, _ = _service(tmp_path, state=state)
    assert service.get_revision_view(revision_id).upload_eligible is eligible
    assert service.resolve_index_input(revision_id).revision_state is state


def test_resolve_index_input_rejects_legacy_metadata_mismatch(tmp_path: Path) -> None:
    service, revision_id, _, _ = _service(tmp_path)
    service.artifacts.repository.get_study_revision = lambda revision_id: SimpleNamespace(
        id=revision_id,
        kind="transcripts",
        source_sha256="0" * 64,
        immutable_source_path=tmp_path / "missing.pptx",
        derived_sha256=None,
        immutable_derived_path=None,
        canonical_derived_path=None,
        current=True,
    )
    with pytest.raises(Exception, match="legacy"):
        service.resolve_index_input(revision_id)


def test_resolve_index_input_rejects_source_authority_mismatch(tmp_path: Path) -> None:
    service, revision_id, _, _ = _service(tmp_path)
    service.knowledge.source_authority = AuthorityClass.PUBLISHED_JOURNAL
    with pytest.raises(Exception, match="authority"):
        service.resolve_index_input(revision_id)


def test_scope_sources_uses_joined_database_rows_and_keeps_all_lifecycle_states(
    tmp_path: Path,
) -> None:
    service, revision_id, evidence, scope = _service(tmp_path)
    database = Database(f"sqlite:///{tmp_path / 'scope.db'}")
    repository = KnowledgeRepository(database)
    repository.initialize()
    repository.create_source("legacy-study-revision:7", AuthorityClass.COURSE_MATERIAL)
    repository.create_revision(
        source_document_id="legacy-study-revision:7",
        source_revision_id=revision_id,
        file_sha256=service.knowledge.revision.file_sha256,
        state=SourceRevisionState.STALE,
    )
    repository.put_evidence_units(revision_id, evidence)
    service.knowledge = repository
    result = service.get_scope_sources(
        RetrievalScope(scope[0], scope[1], (scope[2],), TruthMode.COURSE_ONLY)
    )
    assert result.revisions == (
        service.get_revision_view(revision_id),
    )
    database.close()


def test_reparse_and_persisted_evidence_mismatches_fail_closed(tmp_path: Path) -> None:
    service, revision_id, evidence, _ = _service(tmp_path)
    service.parser = _Parser(
        replace(_document(tmp_path / "deck.pptx", revision_id), parser_version="2")
    )
    with pytest.raises(Exception, match="identity"):
        service.resolve_index_input(revision_id)
    service.parser = _Parser(_document(tmp_path / "deck.pptx", revision_id))
    service.knowledge.evidence = (replace(evidence[0], normalized_text="tampered"),)
    with pytest.raises(Exception, match="evidence"):
        service.resolve_index_input(revision_id)


def test_non_reconcilable_evidence_revision_is_rejected(tmp_path: Path) -> None:
    service, _, evidence, _ = _service(tmp_path, state=SourceRevisionState.FAILED)
    with pytest.raises(Exception, match="reconcilable"):
        service.resolve_evidence(evidence[0].evidence_id)


def test_legacy_lookup_keyerror_is_typed_and_read_only(tmp_path: Path) -> None:
    service, revision_id, evidence, _ = _service(tmp_path)
    before = (service.knowledge.revision, service.knowledge.evidence)

    def missing(_: int) -> object:
        raise KeyError(7)

    service.artifacts.repository.get_study_revision = missing
    with pytest.raises(KnowledgeNotFoundError):
        service.resolve_index_input(revision_id)
    assert (service.knowledge.revision, service.knowledge.evidence) == before
    assert evidence == service.knowledge.evidence


def test_pdf_artifact_failure_is_typed_integrity_error(tmp_path: Path) -> None:
    service, revision_id, _, _ = _service(tmp_path)
    service.artifacts.fail_pdf = True
    with pytest.raises(KnowledgeIntegrityError):
        service.resolve_index_input(revision_id)


@pytest.mark.parametrize("field", ["source_sha256", "derived_sha256"])
def test_legacy_file_hash_mismatch_is_fail_closed(tmp_path: Path, field: str) -> None:
    service, revision_id, _, _ = _service(tmp_path)
    legacy = service.artifacts.repository.get_study_revision(7)
    setattr(legacy, field, "0" * 64)
    with pytest.raises(KnowledgeIntegrityError):
        service.resolve_index_input(revision_id)


def test_catalog_scope_mismatch_is_fail_closed(tmp_path: Path) -> None:
    service, revision_id, _, _ = _service(tmp_path)
    service.artifacts.catalog.get_lecture = lambda lecture_id: SimpleNamespace(
        id=lecture_id,
        subject="Different course",
        exam_number=2,
        lecture_number=7,
    )
    with pytest.raises(KnowledgeIntegrityError, match="scope"):
        service.resolve_index_input(revision_id)


def test_slide_three_resolves_to_authenticated_pdf_page_three(tmp_path: Path) -> None:
    pptx = tmp_path / "deck.pptx"
    pdf = tmp_path / "deck.pdf"
    pptx.write_bytes(b"pptx")
    pdf.write_bytes(b"pdf")
    source_id = "legacy-study-revision:7"
    revision_id = make_revision_id(source_id, sha256_file(pptx))
    document = _document(pptx, revision_id, slide_number=3)
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
    service, _, _, _ = _service(tmp_path)
    service.knowledge.revision = SourceRevision(
        source_id, revision_id, sha256_file(pptx), SourceRevisionState.READY
    )
    service.knowledge.evidence = evidence
    service.parser = _Parser(document)
    preview = service.resolve_evidence(evidence[0].evidence_id)
    assert preview.preview == type(preview.preview)("7", 3)


def test_assets_are_sorted_namespaced_and_verified(tmp_path: Path) -> None:
    service, revision_id, evidence, _ = _service(tmp_path)
    assets = (
        ParsedAsset("z-image", None, "image/png", "a" * 64, DocumentLocator("z")),
        ParsedAsset("a-image", None, "image/jpeg", "b" * 64, DocumentLocator("a")),
    )
    service.parser = _Parser(_document(tmp_path / "deck.pptx", revision_id, assets=assets))
    view = service.resolve_index_input(revision_id)
    assert [asset.asset_id for asset in view.assets] == [
        f"{revision_id}:a-image",
        f"{revision_id}:z-image",
    ]
    assert [asset.sha256 for asset in view.assets] == ["b" * 64, "a" * 64]
    assert evidence == view.evidence_units


def test_asset_reference_and_figure_provenance_fail_closed(tmp_path: Path) -> None:
    service, revision_id, evidence, _ = _service(tmp_path)
    service.knowledge.evidence = (replace(evidence[0], image_asset_id="missing"),)
    with pytest.raises(KnowledgeIntegrityError):
        service.resolve_index_input(revision_id)

    figure = ParsedSegment(
        "figure-1",
        SegmentKind.IMAGE,
        "Figure text",
        DocumentLocator("figure", slide_number=1),
    )
    figure_doc = _document(tmp_path / "deck.pptx", revision_id, segments=(figure,))
    figure_evidence = normalize_course_revision(
        CourseRevisionInput(
            source_revision_id=revision_id,
            course_id=evidence[0].course_id or "",
            exam_id=evidence[0].exam_id,
            lecture_id=evidence[0].lecture_id,
            parsed_document=figure_doc,
        )
    )
    service.knowledge.evidence = figure_evidence
    service.parser = _Parser(figure_doc)
    with pytest.raises(KnowledgeIntegrityError, match="provenance"):
        service.resolve_index_input(revision_id)


def test_asset_file_checksum_mismatch_fails_closed(tmp_path: Path) -> None:
    service, revision_id, _, _ = _service(tmp_path)
    asset_path = tmp_path / "asset.png"
    asset_path.write_bytes(b"asset")
    asset = ParsedAsset(
        "asset-a", asset_path, "image/png", "0" * 64, DocumentLocator("asset")
    )
    service.parser = _Parser(_document(tmp_path / "deck.pptx", revision_id, assets=(asset,)))
    with pytest.raises(KnowledgeIntegrityError, match="checksum"):
        service.resolve_index_input(revision_id)


def test_duplicate_asset_identity_is_rejected_by_document_contract(tmp_path: Path) -> None:
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"pptx")
    duplicate = ParsedAsset("same", None, "image/png", "a" * 64, DocumentLocator("x"))
    with pytest.raises(ValueError, match="asset keys must be unique"):
        _document(path, "sr_unused", assets=(duplicate, duplicate))
