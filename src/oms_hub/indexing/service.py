"""Resumable source-revision indexing through Gemini Files and File Search."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from oms_hub.indexing.models import (
    IndexState,
    ProviderDocument,
    StoreKey,
    validate_transition,
)
from oms_hub.indexing.repository import IndexRepository
from oms_hub.knowledge.models import SourceRevisionState
from oms_hub.providers.contracts import AuthorityClass
from oms_hub.providers.gemini.errors import GeminiProviderError

if TYPE_CHECKING:
    from oms_hub.knowledge.service import IndexInputView


class IndexingInputError(ValueError):
    """The canonical source cannot safely cross the provider boundary."""


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
        key, path, metadata = self._provider_input(view)
        store = self.repository.get_current_store(key)
        if store is not None:
            current = self.repository.get_document_by_source_revision(
                store.id, source_revision_id
            )
            if current is not None and current.state is IndexState.READY:
                return IndexResult(
                    source_revision_id,
                    IndexState.READY,
                    current.provider_document_name,
                )
            if current is not None and current.state is IndexState.TERMINAL_FAILURE:
                return IndexResult(source_revision_id, IndexState.TERMINAL_FAILURE)

        store = await self.admin.ensure_store(key)
        document = self.repository.get_document_by_source_revision(
            store.id, source_revision_id
        )
        if document is None:
            document = self.repository.upsert_document(
                ProviderDocument(
                    store_id=store.id,
                    provider="gemini",
                    provider_document_id=None,
                    source_revision_id=source_revision_id,
                    input_byte_count=path.stat().st_size,
                    metadata=metadata,
                    state=IndexState.UPLOADING_FILE,
                )
            )
        try:
            if document.provider_file_name is None:
                document = self._save(document, state=IndexState.UPLOADING_FILE)
                uploaded = await self.admin.upload_file(path, path.name)
                document = self._save(
                    document,
                    state=IndexState.FILE_UPLOADED,
                    provider_file_name=uploaded.name,
                )

            if document.provider_operation_name is None:
                file_name = document.provider_file_name
                assert file_name is not None
                operation = await self.admin.import_file(
                    store.provider_store_name,
                    file_name,
                    metadata,
                    None,
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
                last_error_category=None,
            )
        except GeminiProviderError as error:
            failed_state = (
                IndexState.RETRYABLE_FAILURE
                if error.retryable
                else IndexState.TERMINAL_FAILURE
            )
            document = self._save(
                document,
                state=failed_state,
                retry_count=document.retry_count + 1,
                last_error_category=error.category,
            )
            return IndexResult(source_revision_id, document.state)

        cleanup_warning = None
        try:
            assert document.provider_file_name is not None
            await self.admin.delete_file(document.provider_file_name)
        except GeminiProviderError as error:
            cleanup_warning = error.category
        return IndexResult(
            source_revision_id,
            document.state,
            document.provider_document_name,
            cleanup_warning,
        )

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


__all__ = ["IndexResult", "IndexingInputError", "IndexingService"]
