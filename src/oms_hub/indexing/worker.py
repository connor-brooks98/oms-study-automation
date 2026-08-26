"""Durable worker for Gemini source-revision indexing jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from oms_hub.indexing.models import IndexJob, IndexState
from oms_hub.indexing.repository import IndexRepository
from oms_hub.indexing.service import IndexingInputError, IndexingService, IndexResult
from oms_hub.ingestion.worker import IngestionWorker
from oms_hub.providers.gemini.errors import GeminiProviderError
from oms_hub.providers.gemini.file_search import GeminiFileSearchAdmin
from oms_hub.workers import RecoveryReport, WorkResult


class IndexWorker:
    """Claim and execute one durable Gemini indexing job at a time."""

    max_attempts = IngestionWorker.max_attempts

    def __init__(
        self,
        repository: IndexRepository,
        service: IndexingService,
        *,
        admin: GeminiFileSearchAdmin | None = None,
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
            config = getattr(getattr(resolved_admin, "client_factory", None), "config", None)
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
        job = self.repository.claim_next_job(
            self.worker_id,
            now,
            lease_seconds=self.lease_seconds,
        )
        if job is None:
            return WorkResult(worked=False)
        try:
            if job.state is IndexState.DELETING:
                self._delete_document(job)
                self._save_job(job, state=IndexState.DELETED)
            else:
                result = self._run_indexing(job)
                self._apply_result(job, result)
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
                last_error_message=self._concise_error(error),
            )
            self._terminalize_document(
                job,
                last_error_category=category,
            )
        except GeminiProviderError as error:
            self._handle_provider_error(job, error)
        except Exception as error:  # noqa: BLE001 - durable job boundary
            self._save_job(
                job,
                state=IndexState.TERMINAL_FAILURE,
                last_error_category="worker",
                last_error_message=self._concise_error(error),
            )
        finally:
            self.repository.release_job_lease(job.id, self.worker_id)
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
            job = self.repository.get_job(job_id)
            if job is None:
                continue
            if job.state in {
                IndexState.READY,
                IndexState.TERMINAL_FAILURE,
                IndexState.DELETED,
            }:
                continue
            if job.retry_count >= self.max_attempts:
                self._save_job(job, state=IndexState.TERMINAL_FAILURE)
                self._terminalize_document(job, last_error_category="retry-exhausted")
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
            if operation_name is not None and job.provider_operation_name != operation_name:
                job = self._save_job(
                    job,
                    state=job.state,
                    provider_operation_name=operation_name,
                )
            if job.state is IndexState.IMPORTING and operation_name is None:
                self._save_job(job, state=IndexState.FILE_UPLOADED)
                if document is not None:
                    self.repository.upsert_document(
                        replace(document, state=IndexState.FILE_UPLOADED)
                    )
            resumed += 1
        return RecoveryReport(
            reclaimed_leases=reclaimed,
            resumed_jobs=resumed,
            terminal_failures=terminal_failures,
        )

    def _run_indexing(self, job: IndexJob) -> IndexResult:
        result = asyncio.run(self.service.index_revision(job.source_revision_id))
        if not isinstance(result, IndexResult):
            raise TypeError("indexing service returned an invalid result")
        return result

    def _delete_document(self, job: IndexJob) -> None:
        if self.admin is None or job.provider_document_id is None:
            raise ValueError("deleting job is missing its provider document admin input")
        asyncio.run(self.admin.delete_document(job.provider_document_id))

    def _apply_result(self, job: IndexJob, result: IndexResult) -> None:
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
                **identity_changes,
            )
            self._terminalize_document(job, last_error_category=category)
        else:
            self._save_job(
                job,
                state=result.state,
                next_attempt_at=None,
                **identity_changes,
            )

    def _handle_provider_error(
        self,
        job: IndexJob,
        error: GeminiProviderError,
    ) -> None:
        if error.retryable:
            self._save_retry_or_terminal(
                job,
                last_error_category=error.category,
                last_error_message=self._concise_error(error),
                retry_state=(
                    IndexState.DELETING
                    if job.state is IndexState.DELETING
                    else IndexState.RETRYABLE_FAILURE
                ),
            )
            return
        self._save_job(
            job,
            state=IndexState.TERMINAL_FAILURE,
            last_error_category=error.category,
            last_error_message=self._concise_error(error),
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
    ) -> None:
        retry_count = job.retry_count + 1
        if retry_count >= self.max_attempts:
            state = IndexState.TERMINAL_FAILURE
            next_attempt_at = None
        else:
            state = retry_state
            delay = timedelta(seconds=5 * (2 ** min(max(retry_count - 1, 0), 6)))
            next_attempt_at = (self.now() + delay).isoformat()
        self._save_job(
            job,
            state=state,
            retry_count=retry_count,
            next_attempt_at=next_attempt_at,
            last_error_category=last_error_category,
            last_error_message=last_error_message,
            **(identity_changes or {}),
        )
        if state is IndexState.TERMINAL_FAILURE:
            self._terminalize_document(
                job,
                last_error_category=last_error_category,
            )

    def _save_job(self, job: IndexJob, *, state: IndexState, **changes: Any) -> IndexJob:
        return self.repository.upsert_job(
            replace(
                job,
                state=state,
                updated_at=self.now().isoformat(),
                **changes,
            )
        )

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

    @staticmethod
    def _concise_error(error: Exception) -> str:
        detail = " ".join(str(error).split())
        return (detail or type(error).__name__)[:1000]


__all__ = ["IndexWorker"]
