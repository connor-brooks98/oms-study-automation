"""Durable worker for Gemini source-revision indexing jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from oms_hub.indexing.models import IndexJob, IndexState, validate_transition
from oms_hub.indexing.repository import IndexRepository
from oms_hub.indexing.service import IndexingInputError, IndexLease, IndexLeaseLost, IndexResult
from oms_hub.ingestion.worker import IngestionWorker
from oms_hub.providers.gemini.errors import GeminiProviderError
from oms_hub.workers import RecoveryReport, WorkResult


class _IndexingService(Protocol):
    async def index_revision(self, source_revision_id: str) -> IndexResult: ...


class _ProviderConfig(Protocol):
    request_timeout_seconds: int
    operation_timeout_seconds: int


class _DocumentAdmin(Protocol):
    async def delete_document(self, provider_document_id: str) -> None: ...

    async def delete_remote_document(self, provider_document_id: str) -> None: ...


class _ClaimLost(RuntimeError):
    pass


class IndexWorker:
    """Claim and execute one durable Gemini indexing job at a time."""

    max_attempts = IngestionWorker.max_attempts

    def __init__(
        self,
        repository: IndexRepository,
        service: _IndexingService,
        *,
        admin: _DocumentAdmin | None = None,
        worker_id: str = "gemini-index-worker",
        lease_seconds: int | None = None,
        max_attempts: int = max_attempts,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        worker_id = worker_id.strip()
        if not worker_id or len(worker_id) > 100:
            raise ValueError("worker ID is invalid")
        resolved_admin = admin or getattr(service, "admin", None)
        if lease_seconds is None:
            config: _ProviderConfig | None = getattr(
                getattr(resolved_admin, "client_factory", None), "config", None
            )
            request_timeout = getattr(config, "request_timeout_seconds", 120)
            operation_timeout = getattr(config, "operation_timeout_seconds", 900)
            lease_seconds = request_timeout * 5 + operation_timeout + 30
        if lease_seconds <= 0:
            raise ValueError("lease seconds must be positive")
        if max_attempts < 1:
            raise ValueError("maximum attempts must be positive")
        self.repository = repository
        self.service = service
        self.admin = resolved_admin
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.now = now or (lambda: datetime.now(UTC))

    def run_once(self) -> WorkResult:
        now = self.now()
        claim_token = self._claim_token()
        job = self.repository.claim_next_job(
            claim_token,
            now,
            lease_seconds=self.lease_seconds,
        )
        if job is None:
            return WorkResult(worked=False)
        try:
            try:
                if job.state is IndexState.DELETING or job.operation_kind in {
                    "delete",
                    "rebuild",
                }:
                    self._delete_revision(job, lease_owner=claim_token)
                else:
                    result = self._run_indexing(job)
                    self._apply_result(job, result, lease_owner=claim_token)
            except IndexingInputError as error:
                category = (
                    "file-too-large"
                    if any(
                        marker in str(error).casefold()
                        for marker in ("size limit", "size-limit", "exceeds")
                    )
                    else "contract"
                )
                self._save_job(
                    job,
                    state=IndexState.TERMINAL_FAILURE,
                    last_error_category=category,
                    last_error_message=category,
                    lease_owner=claim_token,
                )
                self._terminalize_document(
                    job,
                    last_error_category=category,
                )
            except GeminiProviderError as error:
                self._handle_provider_error(job, error, lease_owner=claim_token)
            except IndexLeaseLost:
                pass
            except Exception as error:  # noqa: BLE001 - durable job boundary
                self._save_job(
                    job,
                    state=IndexState.TERMINAL_FAILURE,
                    last_error_category="worker",
                    last_error_message=type(error).__name__,
                    lease_owner=claim_token,
                )
        except _ClaimLost:
            pass
        finally:
            self.repository.release_job_lease(job.id, claim_token, job.lease_token)
        return WorkResult(worked=True, job_id=job.id)

    def recover_interrupted(self) -> RecoveryReport:
        now = self.now()
        candidates = {
            job.id
            for job in self.repository.list_jobs()
            if job.lease_owner is not None
            and job.lease_expires_at is not None
            and job.lease_expires_at <= now.isoformat()
        }
        reclaimed = self.repository.reclaim_expired_jobs(now)
        resumed = 0
        terminal_failures = 0
        for job_id in candidates:
            claim_token = self._claim_token()
            job = self.repository.claim_job(
                job_id,
                claim_token,
                now,
                lease_seconds=self.lease_seconds,
            )
            if job is None:
                continue
            try:
                if job.state in {
                    IndexState.READY,
                    IndexState.TERMINAL_FAILURE,
                    IndexState.DELETED,
                }:
                    continue
                if job.retry_count >= self.max_attempts:
                    saved = self._save_job(
                        job,
                        state=IndexState.TERMINAL_FAILURE,
                        lease_owner=claim_token,
                    )
                    self._terminalize_document(
                        saved,
                        last_error_category="retry-exhausted",
                    )
                    terminal_failures += 1
                    continue
                if job.next_attempt_at is not None and job.next_attempt_at > now.isoformat():
                    continue
                document = self.repository.get_document_by_source_revision(
                    job.store_id,
                    job.source_revision_id,
                )
                operation_name = (
                    document.provider_operation_name
                    if document is not None and document.provider_operation_name is not None
                    else job.provider_operation_name
                )
                identity_changes: dict[str, str] = {}
                if (
                    document is not None
                    and document.provider_document_id is not None
                    and job.provider_document_id != document.provider_document_id
                ):
                    identity_changes["provider_document_id"] = document.provider_document_id
                if operation_name is not None and job.provider_operation_name != operation_name:
                    identity_changes["provider_operation_name"] = operation_name
                if identity_changes:
                    job = self._save_job(
                        job,
                        state=job.state,
                        lease_owner=claim_token,
                        **identity_changes,
                    )
                if job.state is IndexState.IMPORTING and operation_name is None:
                    validate_transition(job.state, IndexState.FILE_UPLOADED)
                    self._save_job(
                        job,
                        state=IndexState.FILE_UPLOADED,
                        lease_owner=claim_token,
                    )
                    if document is not None and document.state is not IndexState.FILE_UPLOADED:
                        validate_transition(document.state, IndexState.FILE_UPLOADED)
                        self.repository.upsert_document(
                            replace(document, state=IndexState.FILE_UPLOADED)
                        )
                resumed += 1
            except _ClaimLost:
                pass
            finally:
                self.repository.release_job_lease(job.id, claim_token, job.lease_token)
        return RecoveryReport(
            reclaimed_leases=reclaimed,
            resumed_jobs=resumed,
            terminal_failures=terminal_failures,
        )

    def _run_indexing(self, job: IndexJob) -> IndexResult:
        if getattr(self.service, "supports_revision_lease", False):
            if job.lease_token is None:
                raise _ClaimLost("index job lease token is missing")
            result = asyncio.run(
                self.service.index_revision(  # type: ignore[call-arg]
                    job.source_revision_id,
                    lease=IndexLease(
                        job.id,
                        job.lease_token,
                        self.lease_seconds,
                        self.now,
                    ),
                )
            )
        else:
            result = asyncio.run(self.service.index_revision(job.source_revision_id))
        if not isinstance(result, IndexResult):
            raise TypeError("indexing service returned an invalid result")
        return result

    def _delete_revision(self, job: IndexJob, *, lease_owner: str) -> None:
        if self.admin is None or job.lease_token is None:
            raise ValueError("deleting job is missing its provider admin or lease token")
        documents = self.repository.list_documents(job.store_id)
        for document in documents:
            if document.source_revision_id != job.source_revision_id:
                continue
            if document.state is IndexState.DELETED:
                continue
            now = self.now()
            if not self.repository.renew_revision_lease(
                job.id,
                job.lease_token,
                now,
                self.lease_seconds,
            ):
                raise _ClaimLost("index job lease expired")
            if not self.repository.mark_document_deleting_with_token(
                document.id,
                job.id,
                job.lease_token,
                now,
            ):
                raise _ClaimLost("index job lease was replaced")
            if document.provider_document_id is not None:
                delete_remote = getattr(self.admin, "delete_remote_document", None)
                if delete_remote is None:
                    delete_remote = self.admin.delete_document
                asyncio.run(delete_remote(document.provider_document_id))
            if not self.repository.mark_document_deleted_with_token(
                document.id,
                job.id,
                job.lease_token,
                self.now(),
            ):
                raise _ClaimLost("index job lease was replaced")
        if job.operation_kind == "rebuild":
            if not self.repository.reset_deleted_revision_for_rebuild(
                job.id,
                job.lease_token,
                self.now(),
            ):
                raise _ClaimLost("index job lease was replaced")
            return
        self._save_job(
            job,
            state=IndexState.DELETED,
            operation_kind="delete",
            lease_owner=lease_owner,
        )

    def _apply_result(
        self,
        job: IndexJob,
        result: IndexResult,
        *,
        lease_owner: str,
    ) -> None:
        document = self.repository.get_document_by_source_revision(
            job.store_id,
            job.source_revision_id,
        )
        identity_changes: dict[str, str | None] = {}
        if result.provider_document_name is not None:
            identity_changes["provider_document_id"] = result.provider_document_name
        if document is not None:
            if (
                result.provider_document_name is None
                and document.provider_document_id is not None
            ):
                identity_changes["provider_document_id"] = document.provider_document_id
            if document.provider_operation_name is not None:
                identity_changes["provider_operation_name"] = document.provider_operation_name
        if result.state is IndexState.RETRYABLE_FAILURE:
            category = (
                document.last_error_category
                if document is not None and document.last_error_category
                else "provider"
            )
            self._save_retry_or_terminal(
                job,
                last_error_category=category,
                identity_changes=identity_changes,
                lease_owner=lease_owner,
            )
        elif result.state is IndexState.TERMINAL_FAILURE:
            category = (
                document.last_error_category
                if document is not None and document.last_error_category
                else "provider"
            )
            self._save_job(
                job,
                state=IndexState.TERMINAL_FAILURE,
                last_error_category=category,
                lease_owner=lease_owner,
                **identity_changes,
            )
            self._terminalize_document(job, last_error_category=category)
        else:
            self._save_job(
                job,
                state=result.state,
                next_attempt_at=None,
                lease_owner=lease_owner,
                **identity_changes,
            )

    def _handle_provider_error(
        self,
        job: IndexJob,
        error: GeminiProviderError,
        *,
        lease_owner: str,
    ) -> None:
        if error.retryable:
            self._save_retry_or_terminal(
                job,
                last_error_category=error.category,
                last_error_message=error.category,
                retry_state=(
                    IndexState.DELETING
                    if job.state is IndexState.DELETING
                    else IndexState.RETRYABLE_FAILURE
                ),
                lease_owner=lease_owner,
            )
            return
        self._save_job(
            job,
            state=IndexState.TERMINAL_FAILURE,
            last_error_category=error.category,
            last_error_message=error.category,
            lease_owner=lease_owner,
        )
        self._terminalize_document(job, last_error_category=error.category)

    def _save_retry_or_terminal(
        self,
        job: IndexJob,
        *,
        last_error_category: str,
        last_error_message: str | None = None,
        retry_state: IndexState = IndexState.RETRYABLE_FAILURE,
        identity_changes: dict[str, str | None] | None = None,
        lease_owner: str | None = None,
    ) -> None:
        retry_count = job.retry_count + 1
        if retry_count >= self.max_attempts:
            state = IndexState.TERMINAL_FAILURE
            next_attempt_at = None
        else:
            state = retry_state
            delay = timedelta(seconds=5 * (2 ** min(max(retry_count - 1, 0), 6)))
            next_attempt_at = (self.now() + delay).isoformat()
        saved = self._save_job(
            job,
            state=state,
            retry_count=retry_count,
            next_attempt_at=next_attempt_at,
            last_error_category=last_error_category,
            last_error_message=last_error_message,
            **(identity_changes or {}),
            lease_owner=lease_owner,
        )
        if state is IndexState.TERMINAL_FAILURE:
            self._terminalize_document(
                saved,
                last_error_category=last_error_category,
            )

    def _save_job(
        self,
        job: IndexJob,
        *,
        state: IndexState,
        lease_owner: str | None = None,
        **changes: Any,
    ) -> IndexJob:
        now = self.now()
        candidate = replace(
            job,
            state=state,
            updated_at=now.isoformat(),
            **changes,
        )
        if lease_owner is None:
            return self.repository.upsert_job(candidate)
        saved = self.repository.save_claimed_job(candidate, lease_owner, now=now)
        if saved is None:
            raise _ClaimLost("index job lease was replaced")
        return saved

    def _terminalize_document(
        self,
        job: IndexJob,
        *,
        last_error_category: str | None = None,
    ) -> None:
        document = self.repository.get_document_by_source_revision(
            job.store_id,
            job.source_revision_id,
        )
        if document is not None and document.state not in {
            IndexState.DELETING,
            IndexState.DELETED,
        }:
            category = document.last_error_category or last_error_category or "provider"
            self.repository.upsert_document(
                replace(
                    document,
                    state=IndexState.TERMINAL_FAILURE,
                    retry_count=max(document.retry_count, job.retry_count),
                    last_error_category=category,
                )
            )

    def _claim_token(self) -> str:
        return f"{self.worker_id[:67]}:{uuid4().hex}"


__all__ = ["IndexWorker"]
