from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from oms_hub.artifacts import ArtifactRole
from oms_hub.db import Database
from oms_hub.document_processing.domain import DocumentLocator
from oms_hub.indexing.models import IndexState, ProviderStore, StoreKey
from oms_hub.indexing.repository import IndexRepository
from oms_hub.indexing.service import (
    IndexingInputError,
    IndexingService,
    build_index_manifest,
)
from oms_hub.knowledge.models import (
    EvidenceLocator,
    EvidenceLocatorKind,
    EvidenceUnit,
    SourceRevisionState,
)
from oms_hub.knowledge.service import (
    CanonicalInputArtifact,
    IndexAssetView,
    IndexInputView,
)
from oms_hub.providers.contracts import AuthorityClass
from oms_hub.providers.gemini.file_search import (
    CompletedOperation,
    OperationRef,
    UploadedFileRef,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _evidence(revision_id: str, evidence_id: str, slide: int) -> EvidenceUnit:
    text = f"Evidence text for slide {slide}."
    return EvidenceUnit(
        evidence_id=evidence_id,
        source_revision_id=revision_id,
        authority_class=AuthorityClass.COURSE_MATERIAL,
        course_id="heme-lymph-0123456789abcdef01234567",
        exam_id="exam-2-0123456789abcdef01234567",
        lecture_id="lecture-13-0123456789abcdef01234567",
        locator=EvidenceLocator(EvidenceLocatorKind.SLIDE, str(slide)),
        normalized_text=text,
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )


def _view(tmp_path: Path) -> IndexInputView:
    revision_id = "sr_aaaaaaaaaaaaaaaaaaaaaaaaaa"
    paths = {
        "pptx": tmp_path / "lecture-13.pptx",
        "pdf": tmp_path / "lecture-13.pdf",
        "markdown": tmp_path / "lecture-13-normalized.md",
        "diagram": tmp_path / "diagram.png",
        "micrograph": tmp_path / "micrograph.jpg",
        "decorative": tmp_path / "decorative.png",
        "animated": tmp_path / "animated.gif",
        "oversized": tmp_path / "oversized.jpg",
        "unknown_size": tmp_path / "unknown-size.png",
    }
    for name in ("pptx", "markdown", "decorative", "animated", "oversized", "unknown_size"):
        paths[name].write_bytes(name.encode())
    paths["pdf"].write_bytes(b"%PDF-1.7\n%%EOF\n")
    Image.new("RGB", (64, 32), "white").save(paths["diagram"], format="PNG")
    Image.new("RGB", (48, 48), "white").save(paths["micrograph"], format="JPEG")

    evidence = (
        _evidence(revision_id, "ev_slide_1", 1),
        _evidence(revision_id, "ev_slide_2", 2),
    )
    assets = (
        IndexAssetView(
            asset_id="asset-diagram",
            path=paths["diagram"],
            media_type="image/png",
            sha256=_sha(paths["diagram"]),
            locator=DocumentLocator("slide 1", slide_number=1),
            width=64,
            height=32,
            visual_semantic=True,
            evidence_ids=("ev_slide_1",),
        ),
        IndexAssetView(
            asset_id="asset-micrograph",
            path=paths["micrograph"],
            media_type="image/jpeg",
            sha256=_sha(paths["micrograph"]),
            locator=DocumentLocator("slide 2", slide_number=2),
            width=48,
            height=48,
            visual_semantic=True,
            evidence_ids=("ev_slide_2",),
        ),
        IndexAssetView(
            asset_id="asset-decorative",
            path=paths["decorative"],
            media_type="image/png",
            sha256=_sha(paths["decorative"]),
            locator=DocumentLocator("slide 3", slide_number=3),
            width=800,
            height=600,
            visual_semantic=False,
            evidence_ids=(),
        ),
        IndexAssetView(
            asset_id="asset-animated",
            path=paths["animated"],
            media_type="image/gif",
            sha256=_sha(paths["animated"]),
            locator=DocumentLocator("slide 4", slide_number=4),
            width=800,
            height=600,
            visual_semantic=True,
            evidence_ids=(),
        ),
        IndexAssetView(
            asset_id="asset-oversized",
            path=paths["oversized"],
            media_type="image/jpeg",
            sha256=_sha(paths["oversized"]),
            locator=DocumentLocator("slide 5", slide_number=5),
            width=4097,
            height=4096,
            visual_semantic=True,
            evidence_ids=(),
        ),
        IndexAssetView(
            asset_id="asset-unknown-size",
            path=paths["unknown_size"],
            media_type="image/png",
            sha256=_sha(paths["unknown_size"]),
            locator=DocumentLocator("slide 6", slide_number=6),
            width=None,
            height=None,
            visual_semantic=True,
            evidence_ids=(),
        ),
    )
    return IndexInputView(
        source_document_id="opaque-source-document",
        source_revision_id=revision_id,
        source_family="legacy_slides",
        revision_state=SourceRevisionState.READY,
        authority_class=AuthorityClass.COURSE_MATERIAL,
        course_id="heme-lymph-0123456789abcdef01234567",
        exam_id="exam-2-0123456789abcdef01234567",
        lecture_id="lecture-13-0123456789abcdef01234567",
        pptx=CanonicalInputArtifact(
            artifact_id=f"{revision_id}:pptx",
            role=ArtifactRole.PPTX,
            path=paths["pptx"],
            sha256=_sha(paths["pptx"]),
            media_type=(
                "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            ),
        ),
        pdf=CanonicalInputArtifact(
            artifact_id=f"{revision_id}:pdf",
            role=ArtifactRole.PDF,
            path=paths["pdf"],
            sha256=_sha(paths["pdf"]),
            media_type="application/pdf",
        ),
        markdown=CanonicalInputArtifact(
            artifact_id=f"{revision_id}:normalized_markdown",
            role=ArtifactRole.CLEANED,
            path=paths["markdown"],
            sha256=_sha(paths["markdown"]),
            media_type="text/markdown",
        ),
        evidence_units=evidence,
        assets=assets,
    )


def test_manifest_contains_required_inputs_and_only_explicit_bounded_images(
    tmp_path: Path,
) -> None:
    view = _view(tmp_path)

    manifest = build_index_manifest(view)

    diagram_key = f"image.{view.assets[0].sha256}"
    micrograph_key = f"image.{view.assets[1].sha256}"
    assert manifest.source_revision_id == view.source_revision_id
    assert tuple(item.input_key for item in manifest.inputs) == (
        "pdf",
        "normalized_markdown",
        diagram_key,
        micrograph_key,
    )
    assert tuple(item.input_kind for item in manifest.inputs) == (
        "pdf",
        "markdown",
        "image",
        "image",
    )
    assert manifest.inputs[0].evidence_ids == ("ev_slide_1", "ev_slide_2")
    assert manifest.inputs[1].evidence_ids == ("ev_slide_1", "ev_slide_2")
    assert manifest.inputs[2].evidence_ids == ("ev_slide_1",)
    assert manifest.inputs[3].evidence_ids == ("ev_slide_2",)
    assert tuple(ref.evidence_id for ref in manifest.evidence) == (
        "ev_slide_1",
        "ev_slide_2",
    )


@pytest.mark.parametrize("input_name", ("pdf", "markdown"))
@pytest.mark.parametrize("failure", ("missing", "changed"))
def test_manifest_rejects_unverified_required_input(
    tmp_path: Path,
    input_name: str,
    failure: str,
) -> None:
    view = _view(tmp_path)
    artifact = getattr(view, input_name)
    if failure == "missing":
        artifact.path.unlink()
    else:
        artifact.path.write_bytes(b"changed after contract resolution")

    with pytest.raises(IndexingInputError):
        build_index_manifest(view)


class _Knowledge:
    def __init__(self, view: IndexInputView) -> None:
        self.view = view

    def resolve_index_input(self, source_revision_id: str) -> IndexInputView:
        assert source_revision_id == self.view.source_revision_id
        return self.view


class _Admin:
    def __init__(self, store: ProviderStore) -> None:
        self.store = store
        self.client_factory = SimpleNamespace(
            config=SimpleNamespace(maximum_document_bytes=1_000_000)
        )
        self.uploaded: list[tuple[Path, str]] = []

    async def ensure_store(self, key: StoreKey) -> ProviderStore:
        assert key == self.store.key
        return self.store

    async def upload_file(self, path: Path, display_name: str) -> UploadedFileRef:
        self.uploaded.append((path, display_name))
        return UploadedFileRef(f"files/{display_name}", path.stat().st_size)

    async def import_file(
        self,
        store_name: str,
        file_name: str,
        metadata: object,
        chunking: object,
    ) -> OperationRef:
        assert store_name == self.store.provider_store_name
        assert metadata
        expected_chunking = (
            {
                "white_space_config": {
                    "max_tokens_per_chunk": 700,
                    "max_overlap_tokens": 100,
                }
            }
            if file_name.endswith("lecture-13-normalized.md")
            else None
        )
        assert chunking == expected_chunking
        return OperationRef(f"operations/{Path(file_name).name}")

    async def wait_for_operation(self, operation_name: str) -> CompletedOperation:
        suffix = operation_name.removeprefix("operations/")
        return CompletedOperation(operation_name, f"documents/{suffix}")

    async def delete_file(self, file_name: str) -> None:
        assert file_name.startswith("files/")


def test_index_revision_persists_each_input_under_its_manifest_key(tmp_path: Path) -> None:
    view = _view(tmp_path)
    database = Database("sqlite://")
    database.create_schema()
    repository = IndexRepository(database)
    key = StoreKey.course(view.course_id, view.exam_id)
    store = repository.create_store(
        ProviderStore(
            store_key=key,
            provider="gemini",
            provider_store_name="fileSearchStores/course-1",
            embedding_model="models/gemini-embedding-2",
            authority_namespace=key.authority_namespace,
            course_id=key.course_id,
            exam_id=key.exam_id,
        )
    )
    admin = _Admin(store)
    service = IndexingService(repository, _Knowledge(view), admin)

    result = asyncio.run(service.index_revision(view.source_revision_id))
    documents = repository.list_documents(store)

    assert result.state is IndexState.READY
    assert tuple(document.input_key for document in documents) == tuple(
        sorted(
            (
                "pptx",
                "pdf",
                "normalized_markdown",
                f"image.{view.assets[0].sha256}",
                f"image.{view.assets[1].sha256}",
            )
        )
    )
    assert {document.input_kind for document in documents} == {
        "pptx",
        "pdf",
        "markdown",
        "image",
    }
    assert all(document.input_sha256 for document in documents)
    assert {path for path, _ in admin.uploaded} == {
        view.pptx.path,
        *(item.path for item in build_index_manifest(view).inputs),
    }


def test_changed_selected_image_fails_before_provider_access(tmp_path: Path) -> None:
    view = _view(tmp_path)
    selected = view.assets[0]
    selected_path = selected.path
    assert selected_path is not None
    selected_path.write_bytes(b"changed after contract resolution")

    with pytest.raises(IndexingInputError):
        build_index_manifest(view)


def test_duplicate_selected_image_hashes_share_one_provider_input(tmp_path: Path) -> None:
    view = _view(tmp_path)
    first = view.assets[0]
    assert first.path is not None
    duplicate_path = tmp_path / "same-diagram.png"
    duplicate_path.write_bytes(first.path.read_bytes())
    duplicate = replace(
        first,
        asset_id="asset-diagram-copy",
        path=duplicate_path,
        evidence_ids=("ev_slide_2",),
    )

    manifest = build_index_manifest(replace(view, assets=(*view.assets, duplicate)))
    image = next(item for item in manifest.inputs if item.input_key.endswith(first.sha256))

    assert image.evidence_ids == ("ev_slide_1", "ev_slide_2")


@pytest.mark.parametrize(
    "case",
    (
        "pdf_role",
        "pdf_media_type",
        "pdf_content",
        "markdown_role",
        "markdown_media_type",
        "markdown_encoding",
    ),
)
def test_manifest_rejects_false_required_media_contract(tmp_path: Path, case: str) -> None:
    view = _view(tmp_path)
    if case == "pdf_role":
        view = replace(view, pdf=replace(view.pdf, role=ArtifactRole.CLEANED))
    elif case == "pdf_media_type":
        view = replace(view, pdf=replace(view.pdf, media_type="text/plain"))
    elif case == "pdf_content":
        view.pdf.path.write_bytes(b"not a PDF")
        view = replace(view, pdf=replace(view.pdf, sha256=_sha(view.pdf.path)))
    elif case == "markdown_role":
        view = replace(view, markdown=replace(view.markdown, role=ArtifactRole.PDF))
    elif case == "markdown_media_type":
        view = replace(view, markdown=replace(view.markdown, media_type="text/plain"))
    else:
        view.markdown.path.write_bytes(b"\xff\xfe")
        view = replace(view, markdown=replace(view.markdown, sha256=_sha(view.markdown.path)))

    with pytest.raises(IndexingInputError):
        build_index_manifest(view)


@pytest.mark.parametrize("case", ("format", "dimensions", "content"))
def test_manifest_verifies_selected_image_bytes_and_dimensions(
    tmp_path: Path,
    case: str,
) -> None:
    view = _view(tmp_path)
    selected = view.assets[0]
    selected_path = selected.path
    assert selected_path is not None
    if case == "format":
        selected = replace(selected, media_type="image/jpeg")
    elif case == "dimensions":
        selected = replace(selected, width=selected.width + 1 if selected.width else 1)
    else:
        selected_path.write_bytes(b"not an image")
        selected = replace(selected, sha256=_sha(selected_path))
    view = replace(view, assets=(selected, *view.assets[1:]))

    with pytest.raises(IndexingInputError):
        build_index_manifest(view)
