"""Resumable source-revision indexing through Gemini Files and File Search."""

from __future__ import annotations

import hashlib
import warnings
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image

from oms_hub.artifacts import ArtifactRole
from oms_hub.indexing.models import (
    IndexState,
    ProviderDocument,
    StoreKey,
    validate_transition,
)
from oms_hub.indexing.repository import IndexRepository
from oms_hub.knowledge.models import SourceRevisionState
from oms_hub.providers.contracts import AuthorityClass, EvidenceRef
from oms_hub.providers.gemini.errors import GeminiProviderError

if TYPE_CHECKING:
    from oms_hub.knowledge.service import IndexInputView

_CLEANUP_PREFIX = "cleanup:"
_SAFE_ERROR_CATEGORIES = frozenset({"authentication", "contract", "provider", "quota", "transient"})
_IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png"})
_MAX_IMAGE_EDGE = 4096


class IndexingInputError(ValueError):
    """The canonical source cannot safely cross the provider boundary."""


@dataclass(frozen=True, slots=True)
class IndexManifestInput:
    input_key: str
    input_kind: str
    path: Path
    media_type: str
    sha256: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))


@dataclass(frozen=True, slots=True)
class IndexManifest:
    source_revision_id: str
    authority_class: AuthorityClass
    inputs: tuple[IndexManifestInput, ...]
    evidence: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "authority_class", AuthorityClass(self.authority_class))
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if not self.source_revision_id:
            raise ValueError("index manifest requires a source revision")
        input_keys = tuple(item.input_key for item in self.inputs)
        if len(input_keys) != len(set(input_keys)):
            raise ValueError("index manifest input keys must be unique")
        if not {"pdf", "normalized_markdown"} <= set(input_keys):
            raise ValueError("index manifest requires PDF and normalized Markdown inputs")
        evidence_by_id = {ref.evidence_id: ref for ref in self.evidence}
        if len(evidence_by_id) != len(self.evidence):
            raise ValueError("index manifest evidence IDs must be unique")
        if any(
            ref.source_revision_id != self.source_revision_id
            or ref.authority_class is not self.authority_class
            for ref in self.evidence
        ):
            raise ValueError("index manifest evidence crosses its source boundary")
        if any(
            evidence_id not in evidence_by_id
            for item in self.inputs
            for evidence_id in item.evidence_ids
        ):
            raise ValueError("index manifest input references unknown evidence")


@dataclass(frozen=True, slots=True)
class IndexResult:
    source_revision_id: str
    state: IndexState
    provider_document_name: str | None = None
    cleanup_warning: str | None = None


class IndexingService:
    def __init__(
        self,
        repository: IndexRepository,
        knowledge_service: Any,
        admin: Any,
    ) -> None:
        self.repository = repository
        self.knowledge_service = knowledge_service
        self.admin = admin

    async def index_revision(self, source_revision_id: str) -> IndexResult:
        view = self.knowledge_service.resolve_index_input(source_revision_id)
        if view.source_revision_id != source_revision_id:
            raise IndexingInputError("resolved source revision does not match request")
        key, path, metadata = self._provider_input(view)
        manifest = build_index_manifest(view)
        all_evidence_ids = tuple(ref.evidence_id for ref in manifest.evidence)
        inputs = (
            IndexManifestInput(
                input_key="pptx",
                input_kind="pptx",
                path=path,
                media_type=view.pptx.media_type,
                sha256=view.pptx.sha256,
                evidence_ids=all_evidence_ids,
            ),
            *manifest.inputs,
        )
        maximum_bytes = self.admin.client_factory.config.maximum_document_bytes
        if any(item.path.stat().st_size > maximum_bytes for item in inputs):
            raise IndexingInputError("canonical source exceeds the provider size limit")
        store = self.repository.get_current_store(key)
        if store is not None:
            current = tuple(
                self.repository.get_document_by_source_revision(
                    store.id,
                    source_revision_id,
                    input_key=item.input_key,
                )
                for item in inputs
            )
            if all(
                document is not None and document.state is IndexState.READY for document in current
            ):
                cleanup_warning = None
                cleaned: list[ProviderDocument] = []
                for document in current:
                    assert document is not None
                    if (document.last_error_category or "").startswith(_CLEANUP_PREFIX):
                        document, warning = await self._cleanup(document)
                        cleanup_warning = cleanup_warning or warning
                    cleaned.append(document)
                if cleaned[0].last_error_category is not None and not cleaned[
                    0
                ].last_error_category.startswith(_CLEANUP_PREFIX):
                    cleaned[0] = self._save(
                        cleaned[0],
                        state=IndexState.READY,
                        last_error_category=None,
                    )
                return IndexResult(
                    source_revision_id,
                    IndexState.READY,
                    cleaned[0].provider_document_name,
                    cleanup_warning,
                )
            if any(
                document is not None and document.state is IndexState.TERMINAL_FAILURE
                for document in current
            ):
                return IndexResult(source_revision_id, IndexState.TERMINAL_FAILURE)

        store = await self.admin.ensure_store(key)
        documents: list[ProviderDocument] = []
        cleanup_warning = None
        for item in inputs:
            document = await self._index_input(
                store.id,
                store.provider_store_name,
                source_revision_id,
                item,
                metadata,
            )
            documents.append(document)
            if document.state in {
                IndexState.RETRYABLE_FAILURE,
                IndexState.TERMINAL_FAILURE,
            }:
                self._mirror_anchor_error(store.id, source_revision_id, document)
                return IndexResult(source_revision_id, document.state)
            if (document.last_error_category or "").startswith(_CLEANUP_PREFIX):
                document, warning = await self._cleanup(document)
                documents[-1] = document
                cleanup_warning = cleanup_warning or warning

        if documents[0].last_error_category is not None and not documents[
            0
        ].last_error_category.startswith(_CLEANUP_PREFIX):
            documents[0] = self._save(
                documents[0],
                state=IndexState.READY,
                last_error_category=None,
            )

        return IndexResult(
            source_revision_id,
            IndexState.READY,
            documents[0].provider_document_name,
            cleanup_warning,
        )

    async def _index_input(
        self,
        store_id: str,
        provider_store_name: str,
        source_revision_id: str,
        item: IndexManifestInput,
        metadata: list[dict[str, str]],
    ) -> ProviderDocument:
        provider_metadata = [
            *metadata,
            {"key": "input_key", "string_value": item.input_key},
            {"key": "input_kind", "string_value": item.input_kind},
            {"key": "input_sha256", "string_value": item.sha256},
        ]
        document = self.repository.get_document_by_source_revision(
            store_id,
            source_revision_id,
            input_key=item.input_key,
        )
        if document is None:
            document = self.repository.upsert_document(
                ProviderDocument(
                    store_id=store_id,
                    provider="gemini",
                    provider_document_id=None,
                    source_revision_id=source_revision_id,
                    input_key=item.input_key,
                    input_kind=item.input_kind,
                    input_sha256=item.sha256,
                    input_byte_count=item.path.stat().st_size,
                    metadata=provider_metadata,
                    state=IndexState.UPLOADING_FILE,
                )
            )
        elif document.state is IndexState.NOT_INDEXED:
            document = self.repository.upsert_document(
                replace(
                    document,
                    input_kind=item.input_kind,
                    input_sha256=item.sha256,
                    input_byte_count=item.path.stat().st_size,
                    metadata=provider_metadata,
                )
            )
        if document.state is IndexState.READY:
            return document
        if document.state is IndexState.TERMINAL_FAILURE:
            return document
        try:
            if document.provider_file_name is None:
                document = self._save(document, state=IndexState.UPLOADING_FILE)
                uploaded = await self.admin.upload_file(item.path, item.path.name)
                document = self._save(
                    document,
                    state=IndexState.FILE_UPLOADED,
                    provider_file_name=uploaded.name,
                )

            if document.provider_operation_name is None:
                file_name = document.provider_file_name
                assert file_name is not None
                chunking = (
                    {
                        "white_space_config": {
                            "max_tokens_per_chunk": 700,
                            "max_overlap_tokens": 100,
                        }
                    }
                    if item.input_key == "normalized_markdown"
                    else None
                )
                operation = await self.admin.import_file(
                    provider_store_name,
                    file_name,
                    provider_metadata,
                    chunking,
                )
                document = self._save(
                    document,
                    state=IndexState.IMPORTING,
                    provider_operation_name=operation.name,
                )
            elif document.state is not IndexState.IMPORTING:
                document = self._save(document, state=IndexState.IMPORTING)

            operation_name = document.provider_operation_name
            assert operation_name is not None
            completed = await self.admin.wait_for_operation(operation_name)
            document = self._save(
                document,
                state=IndexState.READY,
                provider_document_id=completed.document_name,
                provider_document_name=completed.document_name,
                last_error_category=f"{_CLEANUP_PREFIX}pending",
            )
        except GeminiProviderError as error:
            failed_state = (
                IndexState.RETRYABLE_FAILURE if error.retryable else IndexState.TERMINAL_FAILURE
            )
            document = self._save(
                document,
                state=failed_state,
                retry_count=document.retry_count + 1,
                last_error_category=error.category,
            )
        return document

    def _mirror_anchor_error(
        self,
        store_id: str,
        source_revision_id: str,
        failed: ProviderDocument,
    ) -> None:
        if failed.input_key == "pptx":
            return
        anchor = self.repository.get_document_by_source_revision(store_id, source_revision_id)
        if anchor is not None:
            self._save(
                anchor,
                state=anchor.state,
                last_error_category=failed.last_error_category,
            )

    async def _cleanup(
        self,
        document: ProviderDocument,
    ) -> tuple[ProviderDocument, str | None]:
        if document.provider_file_name is None:
            return document, None
        try:
            await self.admin.delete_file(document.provider_file_name)
        except GeminiProviderError as error:
            category = error.category if error.category in _SAFE_ERROR_CATEGORIES else "provider"
            return (
                self._save(
                    document,
                    state=IndexState.READY,
                    last_error_category=f"{_CLEANUP_PREFIX}{category}",
                ),
                category,
            )
        return self._save(document, state=IndexState.READY, last_error_category=None), None

    def _provider_input(
        self,
        view: IndexInputView,
    ) -> tuple[StoreKey, Path, list[dict[str, str]]]:
        if view.source_revision_id == "" or view.revision_state is not SourceRevisionState.READY:
            raise IndexingInputError("source revision is not READY")
        if view.authority_class is not AuthorityClass.COURSE_MATERIAL:
            raise IndexingInputError("source authority does not match a course store")
        try:
            key = StoreKey.course(view.course_id, view.exam_id)
        except ValueError as error:
            raise IndexingInputError("source scope cannot form a course store key") from error
        if key.authority_namespace != view.authority_class.value:
            raise IndexingInputError("source authority does not match the store namespace")
        path = view.pptx.path
        if not path.is_file():
            raise IndexingInputError("canonical source path is missing")
        size = path.stat().st_size
        if size > self.admin.client_factory.config.maximum_document_bytes:
            raise IndexingInputError("canonical source exceeds the provider size limit")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != view.pptx.sha256:
            raise IndexingInputError("canonical source hash does not match")
        metadata = [
            {"key": "authority_class", "string_value": view.authority_class.value},
            {"key": "course_id", "string_value": view.course_id},
            {"key": "exam_id", "string_value": view.exam_id},
            {"key": "lecture_id", "string_value": view.lecture_id},
            {"key": "source_revision_id", "string_value": view.source_revision_id},
        ]
        return key, path, metadata

    def _save(
        self,
        document: ProviderDocument,
        *,
        state: IndexState,
        **changes: Any,
    ) -> ProviderDocument:
        if state is not document.state:
            validate_transition(document.state, state)
        return self.repository.upsert_document(
            replace(
                document,
                state=state,
                updated_at=datetime.now(UTC).isoformat(),
                **changes,
            )
        )


def build_index_manifest(view: IndexInputView) -> IndexManifest:
    if not view.source_revision_id or view.revision_state is not SourceRevisionState.READY:
        raise IndexingInputError("source revision is not READY")
    if view.authority_class is not AuthorityClass.COURSE_MATERIAL:
        raise IndexingInputError("source authority is not course material")

    evidence = tuple(
        EvidenceRef(
            evidence_id=unit.evidence_id,
            source_revision_id=unit.source_revision_id,
            authority_class=unit.authority_class,
            locator_kind=unit.locator.kind.value,
            locator_value=unit.locator.value,
            excerpt=unit.normalized_text,
            checksum=unit.content_sha256,
        )
        for unit in view.evidence_units
    )
    evidence_ids = tuple(ref.evidence_id for ref in evidence)
    known_evidence = set(evidence_ids)
    pdf = _artifact_input(
        input_key="pdf",
        input_kind="pdf",
        path=view.pdf.path,
        media_type=view.pdf.media_type,
        sha256=view.pdf.sha256,
        evidence_ids=evidence_ids,
    )
    markdown = _artifact_input(
        input_key="normalized_markdown",
        input_kind="markdown",
        path=view.markdown.path,
        media_type=view.markdown.media_type,
        sha256=view.markdown.sha256,
        evidence_ids=evidence_ids,
    )
    _validate_pdf_input(view.pdf.role, pdf)
    _validate_markdown_input(view.markdown.role, markdown)
    inputs = [pdf, markdown]
    selected: dict[str, IndexManifestInput] = {}
    for asset in view.assets:
        if (
            not asset.visual_semantic
            or asset.media_type not in _IMAGE_MEDIA_TYPES
            or asset.path is None
            or asset.width is None
            or asset.height is None
            or not (1 <= asset.width <= _MAX_IMAGE_EDGE)
            or not (1 <= asset.height <= _MAX_IMAGE_EDGE)
        ):
            continue
        unknown_evidence = set(asset.evidence_ids) - known_evidence
        if unknown_evidence:
            raise IndexingInputError("visual asset references unknown evidence")
        item = _artifact_input(
            input_key=f"image.{asset.sha256}",
            input_kind="image",
            path=asset.path,
            media_type=asset.media_type,
            sha256=asset.sha256,
            evidence_ids=tuple(sorted(set(asset.evidence_ids))),
        )
        _validate_image_input(asset.width, asset.height, item)
        previous = selected.get(item.input_key)
        if previous is not None:
            item = replace(
                previous,
                evidence_ids=tuple(sorted(set(previous.evidence_ids + item.evidence_ids))),
            )
        selected[item.input_key] = item
    inputs.extend(selected.values())
    try:
        return IndexManifest(
            source_revision_id=view.source_revision_id,
            authority_class=view.authority_class,
            inputs=tuple(inputs),
            evidence=evidence,
        )
    except ValueError as error:
        raise IndexingInputError(str(error)) from error


def _artifact_input(
    *,
    input_key: str,
    input_kind: str,
    path: Path,
    media_type: str,
    sha256: str,
    evidence_ids: tuple[str, ...],
) -> IndexManifestInput:
    path = Path(path)
    if not path.is_file():
        raise IndexingInputError("index input path is missing")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != sha256:
        raise IndexingInputError("index input hash does not match")
    return IndexManifestInput(
        input_key=input_key,
        input_kind=input_kind,
        path=path,
        media_type=media_type,
        sha256=sha256,
        evidence_ids=evidence_ids,
    )


def _validate_pdf_input(role: ArtifactRole, item: IndexManifestInput) -> None:
    if role is not ArtifactRole.PDF or item.media_type != "application/pdf":
        raise IndexingInputError("canonical PDF role or media type is invalid")
    with item.path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise IndexingInputError("canonical PDF content is invalid")


def _validate_markdown_input(role: ArtifactRole, item: IndexManifestInput) -> None:
    if role is not ArtifactRole.CLEANED or item.media_type != "text/markdown":
        raise IndexingInputError("normalized Markdown role or media type is invalid")
    try:
        item.path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise IndexingInputError("normalized Markdown is not UTF-8") from error


def _validate_image_input(
    width: int,
    height: int,
    item: IndexManifestInput,
) -> None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(item.path) as image:
                actual_media_type = {
                    "JPEG": "image/jpeg",
                    "PNG": "image/png",
                }.get(image.format or "")
                if actual_media_type != item.media_type:
                    raise IndexingInputError("visual asset media type does not match its bytes")
                if image.size != (width, height) or any(
                    edge < 1 or edge > _MAX_IMAGE_EDGE for edge in image.size
                ):
                    raise IndexingInputError("visual asset dimensions do not match its bytes")
                image.verify()
    except IndexingInputError:
        raise
    except (OSError, ValueError, Image.DecompressionBombWarning) as error:
        raise IndexingInputError("visual asset content is invalid") from error


__all__ = [
    "IndexManifest",
    "IndexManifestInput",
    "IndexResult",
    "IndexingInputError",
    "IndexingService",
    "build_index_manifest",
]
