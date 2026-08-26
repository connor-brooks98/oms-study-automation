from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from oms_hub.artifacts import ArtifactRole
from oms_hub.db import Database
from oms_hub.indexing.models import IndexJob, IndexState, ProviderStore, StoreKey
from oms_hub.indexing.repository import IndexRepository
from oms_hub.indexing.service import IndexingInputError, IndexingService
from oms_hub.indexing.worker import IndexWorker
from oms_hub.knowledge.models import SourceRevisionState
from oms_hub.knowledge.service import CanonicalInputArtifact, IndexInputView
from oms_hub.providers.contracts import AuthorityClass
from oms_hub.providers.gemini.errors import GeminiContractError, GeminiTransientError
from oms_hub.providers.gemini.file_search import (
    CompletedOperation,
    OperationRef,
    UploadedFileRef,
)


class FakeKnowledgeService:
    def __init__(self, view: IndexInputView) -> None:
        self.view = view
        self.calls: list[str] = []

    def resolve_index_input(self, source_revision_id: str) -> IndexInputView:
        self.calls.append(source_revision_id)
        return self.view


class FakeAdmin:
    def __init__(self, store: ProviderStore, *, maximum_document_bytes: int = 100) -> None:
        self.store = store
        self.client_factory = SimpleNamespace(
            config=SimpleNamespace(maximum_document_bytes=maximum_document_bytes)
        )
        self.ensure_calls: list[StoreKey] = []
        self.upload_calls: list[tuple[Path, str]] = []
        self.import_calls: list[tuple[str, str, object, object]] = []
        self.wait_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.import_failures: list[BaseException | None] = []
        self.wait_failures: list[BaseException | None] = []
        self.delete_error: BaseException | None = None

    async def ensure_store(self, key: StoreKey) -> ProviderStore:
        self.ensure_calls.append(key)
        return self.store

    async def upload_file(self, path: Path, display_name: str) -> UploadedFileRef:
        self.upload_calls.append((path, display_name))
        return UploadedFileRef(f"files/{display_name}", path.stat().st_size)

    async def import_file(
        self,
        store_name: str,
        file_name: str,
        metadata: object,
        chunking: object,
    ) -> OperationRef:
        self.import_calls.append((store_name, file_name, metadata, chunking))
        failure = self.import_failures.pop(0) if self.import_failures else None
        if failure is not None:
            raise failure
        return OperationRef(f"operations/{Path(file_name).name}")

    async def wait_for_operation(self, operation_name: str) -> CompletedOperation:
        self.wait_calls.append(operation_name)
        failure = self.wait_failures.pop(0) if self.wait_failures else None
        if failure is not None:
            raise failure
        suffix = operation_name.removeprefix("operations/")
        return CompletedOperation(
            operation_name,
            f"fileSearchStores/course-1/documents/{suffix}",
        )

    async def delete_file(self, file_name: str) -> None:
        self.delete_calls.append(file_name)
        if self.delete_error is not None:
            raise self.delete_error

    async def delete_document(self, provider_document_id: str) -> None:
        self.delete_calls.append(provider_document_id)

    async def delete_remote_document(self, provider_document_id: str) -> None:
        await self.delete_document(provider_document_id)


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def source_view(tmp_path: Path) -> IndexInputView:
    revision_id = "sr_aaaaaaaaaaaaaaaaaaaaaaaaaa"
    pptx = tmp_path / "lecture.pptx"
    pdf = tmp_path / "lecture.pdf"
    markdown = tmp_path / "lecture.md"
    pptx.write_bytes(b"pptx bytes")
    pdf.write_bytes(b"%PDF-1.7\n%%EOF\n")
    markdown.write_text("normalized evidence\n", encoding="utf-8")
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
            path=pptx,
            sha256=hashlib.sha256(pptx.read_bytes()).hexdigest(),
            media_type=(
                "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            ),
        ),
        pdf=CanonicalInputArtifact(
            artifact_id=f"{revision_id}:pdf",
            role=ArtifactRole.PDF,
            path=pdf,
            sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),
            media_type="application/pdf",
        ),
        markdown=CanonicalInputArtifact(
            artifact_id=f"{revision_id}:normalized_markdown",
            role=ArtifactRole.CLEANED,
            path=markdown,
            sha256=hashlib.sha256(markdown.read_bytes()).hexdigest(),
            media_type="text/markdown",
        ),
        evidence_units=(),
        assets=(),
    )


def service_bundle(
    tmp_path: Path,
    *,
    view: IndexInputView | None = None,
    maximum_document_bytes: int = 100,
) -> tuple[IndexingService, IndexRepository, FakeAdmin, IndexInputView]:
    resolved_view = view or source_view(tmp_path)
    database = Database("sqlite://")
    database.create_schema()
    repository = IndexRepository(database)
    key = StoreKey.course(resolved_view.course_id, resolved_view.exam_id)
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
    admin = FakeAdmin(store, maximum_document_bytes=maximum_document_bytes)
    service = IndexingService(repository, FakeKnowledgeService(resolved_view), admin)
    return service, repository, admin, resolved_view


def test_expired_index_worker_stops_before_another_provider_call_or_document_write(
    tmp_path: Path,
) -> None:
    view = source_view(tmp_path)
    database_url = f"sqlite:///{tmp_path / 'lease.db'}"
    first_database = Database(database_url)
    first_database.create_schema()
    first_repository = IndexRepository(first_database)
    key = StoreKey.course(view.course_id, view.exam_id)
    store = first_repository.create_store(
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
    job = first_repository.save_job(
        IndexJob(store_id=store.id, source_revision_id=view.source_revision_id)
    )
    second_repository = IndexRepository(Database(database_url))
    clock = [datetime(2026, 8, 26, 12, 0, tzinfo=UTC)]
    successor: list[IndexJob] = []

    class ReplacingAdmin(FakeAdmin):
        async def upload_file(self, path: Path, display_name: str) -> UploadedFileRef:
            uploaded = await super().upload_file(path, display_name)
            if len(self.upload_calls) == 1:
                clock[0] += timedelta(seconds=2)
                replacement = second_repository.claim_job(
                    job.id,
                    "worker-b",
                    clock[0],
                    lease_seconds=60,
                )
                assert replacement is not None
                successor.append(replacement)
            return uploaded

    admin = ReplacingAdmin(store)
    service = IndexingService(first_repository, FakeKnowledgeService(view), admin)
    worker = IndexWorker(
        first_repository,
        service,
        admin=admin,
        worker_id="worker-a",
        lease_seconds=1,
        now=lambda: clock[0],
    )

    worker.run_once()

    anchor = first_repository.get_document_by_source_revision(store.id, view.source_revision_id)
    stored_job = second_repository.get_job(job.id)
    assert len(successor) == 1
    assert admin.upload_calls == [(view.pptx.path, "lecture.pptx")]
    assert admin.import_calls == []
    assert admin.wait_calls == []
    assert anchor is not None and anchor.state is IndexState.UPLOADING_FILE
    assert anchor.provider_file_name is None
    assert stored_job is not None and stored_job.lease_owner == "worker-b"
    assert stored_job.state is IndexState.NOT_INDEXED


def test_course_revision_uploads_imports_and_persists_ready_document(tmp_path: Path) -> None:
    service, repository, admin, view = service_bundle(tmp_path)

    result = run(service.index_revision(view.source_revision_id))
    document = repository.get_document_by_source_revision(admin.store.id, view.source_revision_id)

    assert result.state is IndexState.READY
    assert result.cleanup_warning is None
    assert admin.upload_calls == [
        (view.pptx.path, "lecture.pptx"),
        (view.pdf.path, "lecture.pdf"),
        (view.markdown.path, "lecture.md"),
    ]
    expected_metadata = [
        {"key": "authority_class", "string_value": "course_material"},
        {"key": "course_id", "string_value": view.course_id},
        {"key": "exam_id", "string_value": view.exam_id},
        {"key": "lecture_id", "string_value": view.lecture_id},
        {"key": "source_revision_id", "string_value": view.source_revision_id},
    ]
    expected_chunking = {
        "white_space_config": {
            "max_tokens_per_chunk": 700,
            "max_overlap_tokens": 100,
        }
    }
    assert admin.import_calls == [
        (
            admin.store.provider_store_name,
            f"files/{path.name}",
            [
                *expected_metadata,
                {"key": "input_key", "string_value": input_key},
                {"key": "input_kind", "string_value": input_kind},
                {"key": "input_sha256", "string_value": sha256},
            ],
            expected_chunking if path == view.markdown.path else None,
        )
        for path, input_key, input_kind, sha256 in (
            (view.pptx.path, "pptx", "pptx", view.pptx.sha256),
            (view.pdf.path, "pdf", "pdf", view.pdf.sha256),
            (
                view.markdown.path,
                "normalized_markdown",
                "markdown",
                view.markdown.sha256,
            ),
        )
    ]
    assert admin.wait_calls == [
        "operations/lecture.pptx",
        "operations/lecture.pdf",
        "operations/lecture.md",
    ]
    assert admin.delete_calls == [
        "files/lecture.pptx",
        "files/lecture.pdf",
        "files/lecture.md",
    ]
    assert document is not None
    assert document.provider_file_name == "files/lecture.pptx"
    assert document.provider_operation_name == "operations/lecture.pptx"
    assert document.provider_document_id == result.provider_document_name
    assert document.state is IndexState.READY


def test_retry_after_upload_resumes_at_import_without_reupload(tmp_path: Path) -> None:
    service, repository, admin, view = service_bundle(tmp_path)
    admin.import_failures = [GeminiTransientError("temporary"), None]

    first = run(service.index_revision(view.source_revision_id))
    persisted = repository.get_document_by_source_revision(admin.store.id, view.source_revision_id)
    second = run(service.index_revision(view.source_revision_id))

    assert first.state is IndexState.RETRYABLE_FAILURE
    assert persisted is not None
    assert persisted.provider_file_name == "files/lecture.pptx"
    assert persisted.provider_document_id is None
    assert second.state is IndexState.READY
    assert len(admin.upload_calls) == 3
    assert len(admin.import_calls) == 4


def test_retry_after_timeout_polls_persisted_operation_before_new_import(
    tmp_path: Path,
) -> None:
    service, repository, admin, view = service_bundle(tmp_path)
    admin.wait_failures = [GeminiTransientError("operation timed out"), None]

    first = run(service.index_revision(view.source_revision_id))
    persisted = repository.get_document_by_source_revision(admin.store.id, view.source_revision_id)
    second = run(service.index_revision(view.source_revision_id))

    assert first.state is IndexState.RETRYABLE_FAILURE
    assert persisted is not None
    assert persisted.provider_operation_name == "operations/lecture.pptx"
    assert second.state is IndexState.READY
    assert len(admin.upload_calls) == 3
    assert len(admin.import_calls) == 3
    assert admin.wait_calls[:2] == [
        "operations/lecture.pptx",
        "operations/lecture.pptx",
    ]


def test_multimodal_retry_surfaces_category_through_worker_compatibility_anchor(
    tmp_path: Path,
) -> None:
    service, repository, admin, view = service_bundle(tmp_path)
    admin.import_failures = [None, GeminiTransientError("temporary PDF failure"), None, None]

    first = run(service.index_revision(view.source_revision_id))
    anchor = repository.get_document_by_source_revision(admin.store.id, view.source_revision_id)
    failed_pdf = repository.get_document_by_source_revision(
        admin.store.id,
        view.source_revision_id,
        input_key="pdf",
    )

    assert first.state is IndexState.RETRYABLE_FAILURE
    assert failed_pdf is not None and failed_pdf.last_error_category == "transient"
    assert anchor is not None and anchor.last_error_category == "transient"

    second = run(service.index_revision(view.source_revision_id))
    anchor = repository.get_document_by_source_revision(admin.store.id, view.source_revision_id)

    assert second.state is IndexState.READY
    assert anchor is not None and anchor.last_error_category is None
    assert admin.upload_calls == [
        (view.pptx.path, "lecture.pptx"),
        (view.pdf.path, "lecture.pdf"),
        (view.markdown.path, "lecture.md"),
    ]
    assert admin.delete_calls == [
        "files/lecture.pptx",
        "files/lecture.pdf",
        "files/lecture.md",
    ]


def test_resolver_revision_mismatch_is_rejected_without_provider_or_document_mutation(
    tmp_path: Path,
) -> None:
    service, repository, admin, view = service_bundle(tmp_path)
    other_revision = "sr_bbbbbbbbbbbbbbbbbbbbbbbbbb"

    with pytest.raises(IndexingInputError, match="revision"):
        run(service.index_revision(other_revision))

    assert view.source_revision_id != other_revision
    assert admin.ensure_calls == []
    assert admin.upload_calls == []
    assert admin.import_calls == []
    assert repository.list_documents(admin.store) == []


@pytest.mark.parametrize(
    "case",
    ("missing", "hash", "stale", "retired", "oversized", "authority"),
)
def test_invalid_source_is_rejected_before_any_provider_call(
    tmp_path: Path,
    case: str,
) -> None:
    view = source_view(tmp_path)
    maximum = 100
    if case == "missing":
        view.pptx.path.unlink()
    elif case == "hash":
        view = replace(view, pptx=replace(view.pptx, sha256="0" * 64))
    elif case == "stale":
        view = replace(view, revision_state=SourceRevisionState.STALE)
    elif case == "retired":
        view = replace(view, revision_state=SourceRevisionState.RETIRED)
    elif case == "oversized":
        maximum = 1
    else:
        view = replace(view, authority_class=AuthorityClass.PUBLISHED_JOURNAL)
    service, _, admin, _ = service_bundle(tmp_path, view=view, maximum_document_bytes=maximum)

    with pytest.raises(IndexingInputError):
        run(service.index_revision(view.source_revision_id))

    assert admin.ensure_calls == []
    assert admin.upload_calls == []
    assert admin.import_calls == []
    assert admin.wait_calls == []


def test_ready_is_idempotent_and_cleanup_failure_is_only_a_warning(tmp_path: Path) -> None:
    service, repository, admin, view = service_bundle(tmp_path)
    admin.delete_error = GeminiTransientError("cleanup unavailable")

    first = run(service.index_revision(view.source_revision_id))
    calls = (
        len(admin.ensure_calls),
        len(admin.upload_calls),
        len(admin.import_calls),
        len(admin.wait_calls),
    )
    pending_cleanup = repository.get_document_by_source_revision(
        admin.store.id, view.source_revision_id
    )
    admin.delete_error = None
    second = run(service.index_revision(view.source_revision_id))
    document = repository.get_document_by_source_revision(admin.store.id, view.source_revision_id)

    assert first.state is IndexState.READY
    assert first.cleanup_warning == "transient"
    assert pending_cleanup is not None
    assert pending_cleanup.last_error_category == "cleanup:transient"
    assert second.state is IndexState.READY
    assert second.cleanup_warning is None
    assert calls == (
        len(admin.ensure_calls),
        len(admin.upload_calls),
        len(admin.import_calls),
        len(admin.wait_calls),
    )
    assert admin.delete_calls == [
        "files/lecture.pptx",
        "files/lecture.pdf",
        "files/lecture.md",
        "files/lecture.pptx",
        "files/lecture.pdf",
        "files/lecture.md",
    ]
    assert document is not None and document.state is IndexState.READY
    assert document.last_error_category is None


def test_nonretryable_provider_failure_persists_terminal_state(tmp_path: Path) -> None:
    service, repository, admin, view = service_bundle(tmp_path)
    admin.import_failures = [GeminiContractError("contract changed")]

    result = run(service.index_revision(view.source_revision_id))
    document = repository.get_document_by_source_revision(admin.store.id, view.source_revision_id)
    calls = (len(admin.ensure_calls), len(admin.upload_calls), len(admin.import_calls))
    repeated = run(service.index_revision(view.source_revision_id))

    assert result.state is IndexState.TERMINAL_FAILURE
    assert repeated.state is IndexState.TERMINAL_FAILURE
    assert calls == (len(admin.ensure_calls), len(admin.upload_calls), len(admin.import_calls))
    assert document is not None
    assert document.state is IndexState.TERMINAL_FAILURE
    assert document.last_error_category == "contract"
