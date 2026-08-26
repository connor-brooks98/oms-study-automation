"""Read-only index reconciliation and durable revision lifecycle requests."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from oms_hub.indexing.models import IndexJob, IndexState, ProviderDocument, ProviderStore
from oms_hub.indexing.repository import IndexRepository
from oms_hub.knowledge.models import SourceRevisionState
from oms_hub.providers.contracts import RetrievalScope, TruthMode
from oms_hub.providers.gemini.errors import GeminiProviderError
from oms_hub.providers.gemini.file_search import RemoteDocumentObservation


class FindingKind(StrEnum):
    DUPLICATE_REMOTE = "duplicate_remote"
    IDENTITY_MISMATCH = "identity_mismatch"
    INVALID_REMOTE = "invalid_remote"
    LOCAL_MISSING_REMOTE = "local_missing_remote"
    REMOTE_MISSING_LOCAL = "remote_missing_local"
    STALE_SOURCE = "stale_source"
    STORE_MISSING = "store_missing"


@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    kind: FindingKind
    source_revision_id: str | None = None
    input_key: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    store_id: str
    applied: bool
    findings: tuple[ReconciliationFinding, ...]
    repaired_input_count: int = 0
    indexed_byte_count: int = 0
    index_token_count: None = None
    query_token_count: None = None
    estimated_cost: None = None


@dataclass(frozen=True, slots=True)
class IndexHealth:
    provider: str
    configured: bool
    sdk_version: str
    model: str
    embedding_model: str
    ready: bool
    last_contract_smoke: None
    store_count: int
    ready_document_count: int
    failed_document_count: int
    indexed_byte_count: int
    index_token_count: None
    query_token_count: None
    estimated_cost: None


@dataclass(frozen=True, slots=True)
class RevisionIndexView:
    source_revision_id: str
    store_id: str
    state: IndexState
    input_count: int
    indexed_byte_count: int


class ReconciliationConflict(RuntimeError):
    pass


class IndexReconciler:
    def __init__(
        self,
        repository: IndexRepository,
        knowledge_service: Any,
        admin: Any,
        *,
        now: Callable[[], datetime] | None = None,
        lease_seconds: int | None = None,
    ) -> None:
        self.repository = repository
        self.knowledge_service = knowledge_service
        self.admin = admin
        self.now = now or (lambda: datetime.now(UTC))
        config = getattr(getattr(admin, "client_factory", None), "config", None)
        if lease_seconds is None:
            lease_seconds = (
                int(getattr(config, "request_timeout_seconds", 120))
                + int(getattr(config, "operation_timeout_seconds", 900))
                + 30
            )
        self.lease_seconds = lease_seconds

    async def reconcile_store(
        self,
        store_id: str,
        *,
        apply: bool = False,
    ) -> ReconciliationReport:
        store = self.repository.get_store_by_id(store_id)
        if store is None:
            raise KeyError(store_id)
        local = self.repository.list_documents(store)
        indexed_bytes = sum(
            item.input_byte_count or 0 for item in local if item.state is IndexState.READY
        )
        try:
            remote = await self.admin.snapshot_documents(store)
        except GeminiProviderError as error:
            if error.provider_status_code != 404:
                raise
            return ReconciliationReport(
                store_id,
                apply,
                (ReconciliationFinding(FindingKind.STORE_MISSING),),
                indexed_byte_count=indexed_bytes,
            )

        findings, local_missing = self._findings(local, remote)
        repaired = 0
        if apply:
            for document in local_missing:
                if (
                    self._revision_state(document.source_revision_id)
                    is not SourceRevisionState.READY
                ):
                    continue
                if self.repository.reset_missing_remote_input(
                    document.id,
                    self.now(),
                    lease_seconds=self.lease_seconds,
                ):
                    repaired += 1
            stale_revisions = {
                item.source_revision_id
                for item in findings
                if item.kind is FindingKind.STALE_SOURCE and item.source_revision_id is not None
            }
            for revision_id in sorted(stale_revisions):
                self._schedule_operation(store.id, revision_id, "delete")
        return ReconciliationReport(
            store.id,
            apply,
            tuple(findings),
            repaired_input_count=repaired,
            indexed_byte_count=indexed_bytes,
        )

    def rebuild_revision(self, revision_id: str) -> IndexJob:
        store = self._one_current_store(revision_id)
        return self._schedule_operation(store.id, revision_id, "rebuild")

    def delete_revision(self, revision_id: str) -> IndexJob:
        store = self._one_current_store(revision_id)
        return self._schedule_operation(store.id, revision_id, "delete")

    def revision_status(self, revision_id: str) -> RevisionIndexView:
        store = self._one_current_store(revision_id)
        documents = [
            item
            for item in self.repository.list_documents(store)
            if item.source_revision_id == revision_id
        ]
        job = self.repository.get_job_by_revision(store.id, revision_id)
        if job is not None:
            state = job.state
        elif documents and all(item.state is IndexState.READY for item in documents):
            state = IndexState.READY
        else:
            state = IndexState.NOT_INDEXED
        return RevisionIndexView(
            revision_id,
            store.id,
            state,
            len(documents),
            sum(item.input_byte_count or 0 for item in documents),
        )

    def health(self) -> IndexHealth:
        stores = self.repository.list_stores(current_only=True)
        documents = [item for store in stores for item in self.repository.list_documents(store)]
        ready = [item for item in documents if item.state is IndexState.READY]
        failed = [
            item
            for item in documents
            if item.state in {IndexState.RETRYABLE_FAILURE, IndexState.TERMINAL_FAILURE}
        ]
        config = getattr(getattr(self.admin, "client_factory", None), "config", None)
        configured = config is not None
        return IndexHealth(
            provider="gemini",
            configured=configured,
            sdk_version=str(getattr(config, "sdk_version", "unavailable")),
            model=str(getattr(config, "file_search_model", "unavailable")),
            embedding_model=str(getattr(config, "embedding_model", "unavailable")),
            ready=configured,
            last_contract_smoke=None,
            store_count=len(stores),
            ready_document_count=len(ready),
            failed_document_count=len(failed),
            indexed_byte_count=sum(item.input_byte_count or 0 for item in ready),
            index_token_count=None,
            query_token_count=None,
            estimated_cost=None,
        )

    def _findings(
        self,
        local: list[ProviderDocument],
        remote: tuple[RemoteDocumentObservation, ...],
    ) -> tuple[list[ReconciliationFinding], list[ProviderDocument]]:
        findings: list[ReconciliationFinding] = []
        valid_remote: dict[tuple[str, str], list[RemoteDocumentObservation]] = defaultdict(list)
        for item in remote:
            if (
                item.validation_error is not None
                or item.source_revision_id is None
                or item.input_key is None
                or item.input_kind is None
                or item.input_sha256 is None
            ):
                findings.append(ReconciliationFinding(FindingKind.INVALID_REMOTE))
                continue
            valid_remote[(item.source_revision_id, item.input_key)].append(item)

        local_by_key = {(item.source_revision_id, item.input_key): item for item in local}
        duplicate_keys = {key for key, items in valid_remote.items() if len(items) > 1}
        for revision_id, input_key in duplicate_keys:
            findings.append(
                ReconciliationFinding(FindingKind.DUPLICATE_REMOTE, revision_id, input_key)
            )
        local_missing: list[ProviderDocument] = []
        for key, document in local_by_key.items():
            if (
                document.state is IndexState.READY
                and self._revision_state(document.source_revision_id)
                in {SourceRevisionState.STALE, SourceRevisionState.RETIRED}
            ):
                findings.append(
                    ReconciliationFinding(
                        FindingKind.STALE_SOURCE,
                        document.source_revision_id,
                        document.input_key,
                    )
                )
            if key in duplicate_keys:
                continue
            observations = valid_remote.get(key)
            if not observations:
                findings.append(
                    ReconciliationFinding(
                        FindingKind.LOCAL_MISSING_REMOTE,
                        document.source_revision_id,
                        document.input_key,
                    )
                )
                if document.state is IndexState.READY:
                    local_missing.append(document)
            else:
                observation = observations[0]
                if (
                    observation.input_kind != document.input_kind
                    or observation.input_sha256 != document.input_sha256
                ):
                    findings.append(
                        ReconciliationFinding(
                            FindingKind.IDENTITY_MISMATCH,
                            document.source_revision_id,
                            document.input_key,
                        )
                    )
        for (revision_id, input_key), observations in valid_remote.items():
            if (revision_id, input_key) not in local_by_key and len(observations) == 1:
                findings.append(
                    ReconciliationFinding(
                        FindingKind.REMOTE_MISSING_LOCAL,
                        revision_id,
                        input_key,
                    )
                )
        findings.sort(
            key=lambda item: (
                item.kind.value,
                item.source_revision_id or "",
                item.input_key or "",
            )
        )
        local_missing.sort(key=lambda item: (item.source_revision_id, item.input_key))
        return findings, local_missing

    def _revision_state(self, revision_id: str) -> SourceRevisionState:
        view = self.knowledge_service.get_revision_view(revision_id)
        return SourceRevisionState(view.revision_state)

    def _one_current_store(self, revision_id: str) -> ProviderStore:
        documents = self.repository.list_documents_by_revision(
            revision_id,
            current_stores_only=True,
        )
        stores = [
            store
            for store_id in sorted({item.store_id for item in documents})
            if (store := self.repository.get_store_by_id(store_id)) is not None
        ]
        accepted: list[ProviderStore] = []
        for store in stores:
            scope = RetrievalScope(
                store.course_id,
                store.exam_id,
                (),
                TruthMode.COURSE_ONLY,
                (revision_id,),
            )
            view = self.knowledge_service.get_scope_sources(scope)
            matches = [
                item
                for item in view.revisions
                if item.source_revision_id == revision_id
            ]
            if len(matches) == 1:
                accepted.append(store)
        if len(accepted) != 1:
            raise ReconciliationConflict(
                "revision does not resolve to exactly one current store for its accepted scope"
            )
        return accepted[0]

    def _schedule_operation(
        self,
        store_id: str,
        revision_id: str,
        operation_kind: str,
    ) -> IndexJob:
        owner = f"index-route:{uuid4().hex}"[:100]
        job = self.repository.claim_revision_operation(
            store_id,
            revision_id,
            operation_kind,
            owner,
            self.now(),
            lease_seconds=self.lease_seconds,
        )
        if job is None or job.lease_token is None:
            raise ReconciliationConflict("revision lifecycle is already claimed")
        self.repository.release_job_lease(job.id, owner, job.lease_token)
        stored = self.repository.get_job(job.id)
        assert stored is not None
        return stored


__all__ = [
    "FindingKind",
    "IndexHealth",
    "IndexReconciler",
    "ReconciliationConflict",
    "ReconciliationFinding",
    "ReconciliationReport",
    "RevisionIndexView",
]
