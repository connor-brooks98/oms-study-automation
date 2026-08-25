from __future__ import annotations

import ast
import hashlib
import inspect
from pathlib import Path

from oms_hub.artifacts import ArtifactRole
from oms_hub.indexing.models import StoreKey
from oms_hub.knowledge.models import SourceRevisionState
from oms_hub.knowledge.service import CanonicalInputArtifact, IndexInputView
from oms_hub.providers.contracts import AuthorityClass


def _provider_indexing_input(view: IndexInputView) -> dict[str, object]:
    store_key = StoreKey.course(view.course_id, view.exam_id)
    return {
        "store_key": store_key,
        "path": view.pptx.path,
        "display_name": view.pptx.path.name,
        "metadata": [
            {"key": "authority_class", "string_value": view.authority_class.value},
            {"key": "course_id", "string_value": view.course_id},
            {"key": "exam_id", "string_value": view.exam_id},
            {"key": "lecture_id", "string_value": view.lecture_id},
            {"key": "source_revision_id", "string_value": view.source_revision_id},
        ],
    }


def test_sol2_consumer_uses_only_index_input_view(tmp_path: Path) -> None:
    revision_id = "sr_aaaaaaaaaaaaaaaaaaaaaaaaaa"
    pptx = tmp_path / "lecture.pptx"
    pdf = tmp_path / "lecture.pdf"
    pptx.write_bytes(b"pptx")
    pdf.write_bytes(b"pdf")
    view = IndexInputView(
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
            path=pptx,
            sha256=hashlib.sha256(pptx.read_bytes()).hexdigest(),
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation"
            ),
        ),
        pdf=CanonicalInputArtifact(
            artifact_id=f"{revision_id}:pdf",
            role=ArtifactRole.PDF,
            path=pdf,
            sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),
            media_type="application/pdf",
        ),
        evidence_units=(),
        assets=(),
    )

    provider_input = _provider_indexing_input(view)
    store_key = provider_input["store_key"]

    assert isinstance(store_key, StoreKey)
    assert StoreKey.parse(store_key.value) == store_key
    assert provider_input["path"] == pptx
    assert provider_input["metadata"] == [
        {"key": "authority_class", "string_value": "course_material"},
        {"key": "course_id", "string_value": view.course_id},
        {"key": "exam_id", "string_value": view.exam_id},
        {"key": "lecture_id", "string_value": view.lecture_id},
        {"key": "source_revision_id", "string_value": revision_id},
    ]
    assert "opaque-source-document" not in repr(provider_input)

    consumer_source = inspect.getsource(_provider_indexing_input)
    assert "source_document_id" not in consumer_source
    assert "legacy-study-revision" not in consumer_source

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    called_names.update(
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    )
    assert not any(
        module == forbidden or module.startswith(f"{forbidden}.")
        for module in imported_modules
        for forbidden in ("oms_hub.ingestion", "oms_hub.repositories")
    )
    assert called_names.isdisjoint(
        {
            "IngestionRepository",
            "CatalogRepository",
            "get_study_revision",
            "list_current_revisions",
            "list_lectures",
            "get_lecture",
        }
    )
