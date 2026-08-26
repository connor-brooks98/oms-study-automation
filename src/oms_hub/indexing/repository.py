"""SQLAlchemy repository for provider stores, documents, and index jobs."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult

from oms_hub.db import Database
from oms_hub.indexing.models import (
    IndexJob,
    IndexJobModel,
    IndexState,
    ProviderDocument,
    ProviderDocumentModel,
    ProviderStore,
    ProviderStoreModel,
    StoreKey,
    validate_transition,
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class IndexRepository:
    """Persist indexing state without creating or altering the schema."""

    def __init__(self, database: Database):
        self.database = database

    def create_store(self, store: ProviderStore) -> ProviderStore:
        with self.database.session() as session:
            session.add(self._store_row(store))
            session.flush()
        return store

    def save_store(self, store: ProviderStore) -> ProviderStore:
        with self.database.session() as session:
            row = session.get(ProviderStoreModel, store.id)
            if row is None:
                session.add(self._store_row(store))
            else:
                self._copy_store(row, store)
            session.flush()
        return store

    def get_store(self, store_key: StoreKey | str) -> ProviderStore | None:
        return self.get_current_store(store_key)

    def get_current_store(self, store_key: StoreKey | str) -> ProviderStore | None:
        key = _key_value(store_key)
        with self.database.session() as session:
            row = session.scalar(
                select(ProviderStoreModel)
                .where(
                    ProviderStoreModel.store_key == key,
                    ProviderStoreModel.is_current.is_(True),
                )
                .order_by(ProviderStoreModel.generation.desc())
            )
            return self._store_from_row(row) if row is not None else None

    def get_store_by_id(self, store_id: str) -> ProviderStore | None:
        with self.database.session() as session:
            row = session.get(ProviderStoreModel, store_id)
            return self._store_from_row(row) if row is not None else None

    def list_store_generations(self, store_key: StoreKey | str) -> list[ProviderStore]:
        key = _key_value(store_key)
        with self.database.session() as session:
            rows = session.scalars(
                select(ProviderStoreModel)
                .where(ProviderStoreModel.store_key == key)
                .order_by(ProviderStoreModel.generation.asc())
            ).all()
            return [self._store_from_row(row) for row in rows]

    def next_store_generation(self, store_key: StoreKey | str) -> int:
        key = _key_value(store_key)
        with self.database.session() as session:
            maximum = session.scalar(
                select(func.max(ProviderStoreModel.generation)).where(
                    ProviderStoreModel.store_key == key
                )
            )
            return int(maximum or 0) + 1

    def mark_store_stale(self, store_id: str) -> ProviderStore:
        with self.database.session() as session:
            row = session.get(ProviderStoreModel, store_id)
            if row is None:
                raise KeyError(store_id)
            current = IndexState(row.state)
            if current is not IndexState.STALE:
                validate_transition(current, IndexState.STALE)
                row.state = IndexState.STALE.value
            row.is_current = False
            row.updated_at = _utc_now()
            session.flush()
            return self._store_from_row(row)

    def save_document(self, document: ProviderDocument) -> ProviderDocument:
        with self.database.session() as session:
            session.add(self._document_row(document))
            session.flush()
        return document

    def upsert_document(self, document: ProviderDocument) -> ProviderDocument:
        with self.database.session() as session:
            row = session.get(ProviderDocumentModel, document.id)
            if row is None and document.provider_document_id is not None:
                row = session.scalar(
                    select(ProviderDocumentModel).where(
                        ProviderDocumentModel.provider == document.provider,
                        ProviderDocumentModel.provider_document_id
                        == document.provider_document_id,
                    )
                )
            if row is None:
                row = session.scalar(
                    select(ProviderDocumentModel).where(
                        ProviderDocumentModel.store_id == document.store_id,
                        ProviderDocumentModel.source_revision_id == document.source_revision_id,
                        ProviderDocumentModel.input_key == document.input_key,
                    )
                )
            if row is None:
                row = self._document_row(document)
                session.add(row)
            else:
                self._copy_document(row, document)
                document = self._document_from_row(row)
            session.flush()
        return document

    def get_document(self, document_id: str) -> ProviderDocument | None:
        with self.database.session() as session:
            row = session.get(ProviderDocumentModel, document_id)
            if row is None:
                row = session.scalar(
                    select(ProviderDocumentModel).where(
                        ProviderDocumentModel.provider_document_id == document_id
                    )
                )
            return self._document_from_row(row) if row is not None else None

    def get_document_by_provider_id(self, provider_document_id: str) -> ProviderDocument | None:
        with self.database.session() as session:
            row = session.scalar(
                select(ProviderDocumentModel).where(
                    ProviderDocumentModel.provider_document_id == provider_document_id
                )
            )
            return self._document_from_row(row) if row is not None else None

    def get_document_by_source_revision(
        self,
        store_id: str,
        source_revision_id: str,
        *,
        input_key: str = "pptx",
    ) -> ProviderDocument | None:
        with self.database.session() as session:
            row = session.scalar(
                select(ProviderDocumentModel).where(
                    ProviderDocumentModel.store_id == store_id,
                    ProviderDocumentModel.source_revision_id == source_revision_id,
                    ProviderDocumentModel.input_key == input_key,
                )
            )
            return self._document_from_row(row) if row is not None else None

    def list_documents(self, store: ProviderStore | str) -> list[ProviderDocument]:
        store_id = store.id if isinstance(store, ProviderStore) else store
        with self.database.session() as session:
            rows = session.scalars(
                select(ProviderDocumentModel)
                .where(ProviderDocumentModel.store_id == store_id)
                .order_by(
                    ProviderDocumentModel.input_key.asc(),
                    ProviderDocumentModel.id.asc(),
                )
            ).all()
            return [self._document_from_row(row) for row in rows]

    def transition_document(self, document_id: str, after: IndexState) -> ProviderDocument:
        with self.database.session() as session:
            row = session.get(ProviderDocumentModel, document_id)
            if row is None:
                row = session.scalar(
                    select(ProviderDocumentModel).where(
                        ProviderDocumentModel.provider_document_id == document_id
                    )
                )
            if row is None:
                raise KeyError(document_id)
            before = IndexState(row.state)
            validate_transition(before, after)
            row.state = after.value
            row.updated_at = _utc_now()
            session.flush()
            return self._document_from_row(row)

    def mark_document_deleting(self, provider_document_id: str) -> ProviderDocument:
        with self.database.session() as session:
            row = session.scalar(
                select(ProviderDocumentModel).where(
                    ProviderDocumentModel.provider_document_id == provider_document_id
                )
            )
            if row is None:
                raise KeyError(provider_document_id)
            state = IndexState(row.state)
            if state is not IndexState.DELETING and state is not IndexState.DELETED:
                validate_transition(state, IndexState.DELETING)
                row.state = IndexState.DELETING.value
                row.updated_at = _utc_now()
            session.flush()
            return self._document_from_row(row)

    def mark_document_deleted(self, provider_document_id: str) -> ProviderDocument:
        return self.transition_document(provider_document_id, IndexState.DELETED)

    def save_job(self, job: IndexJob) -> IndexJob:
        with self.database.session() as session:
            session.add(self._job_row(job))
            session.flush()
        return job

    def upsert_job(self, job: IndexJob) -> IndexJob:
        with self.database.session() as session:
            row = session.get(IndexJobModel, job.id)
            if row is None:
                row = session.scalar(
                    select(IndexJobModel).where(
                        IndexJobModel.store_id == job.store_id,
                        IndexJobModel.source_revision_id == job.source_revision_id,
                    )
                )
            if row is None:
                row = self._job_row(job)
                session.add(row)
            else:
                self._copy_job(row, job)
                job = self._job_from_row(row)
            session.flush()
        return job

    def get_job(self, job_id: str) -> IndexJob | None:
        with self.database.session() as session:
            row = session.get(IndexJobModel, job_id)
            return self._job_from_row(row) if row is not None else None

    def list_jobs(self) -> list[IndexJob]:
        with self.database.session() as session:
            rows = session.scalars(
                select(IndexJobModel).order_by(
                    IndexJobModel.created_at, IndexJobModel.id
                )
            ).all()
            return [self._job_from_row(row) for row in rows]

    def save_claimed_job(self, job: IndexJob, lease_owner: str) -> IndexJob | None:
        if not lease_owner.strip() or len(lease_owner) > 100:
            raise ValueError("lease owner is blank or unbounded")
        with self.database.session() as session:
            changed = session.execute(
                update(IndexJobModel)
                .where(
                    IndexJobModel.id == job.id,
                    IndexJobModel.lease_owner == lease_owner,
                )
                .values(
                    store_id=job.store_id,
                    source_revision_id=job.source_revision_id,
                    operation_kind=job.operation_kind,
                    lease_token=job.lease_token,
                    provider_document_id=job.provider_document_id,
                    provider_operation_name=job.provider_operation_name,
                    state=job.state.value,
                    retry_count=job.retry_count,
                    last_error_category=job.last_error_category,
                    last_error_message=job.last_error_message,
                    next_attempt_at=job.next_attempt_at,
                    updated_at=job.updated_at,
                )
            )
            if cast(CursorResult[Any], changed).rowcount != 1:
                return None
            row = session.get(IndexJobModel, job.id)
            assert row is not None
            session.refresh(row)
            return self._job_from_row(row)

    def claim_job(
        self,
        job_id: str,
        worker_id: str,
        now: datetime,
        *,
        lease_seconds: int,
    ) -> IndexJob | None:
        if not worker_id.strip() or len(worker_id) > 100:
            raise ValueError("worker id is blank or unbounded")
        if lease_seconds <= 0:
            raise ValueError("lease seconds must be positive")
        now_value = now.isoformat()
        expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.database.session() as session:
            claimed = session.execute(
                update(IndexJobModel)
                .where(
                    IndexJobModel.id == job_id,
                    or_(
                        IndexJobModel.lease_owner.is_(None),
                        IndexJobModel.lease_expires_at.is_(None),
                        IndexJobModel.lease_expires_at <= now_value,
                    ),
                )
                .values(lease_owner=worker_id, lease_expires_at=expires)
            )
            if cast(CursorResult[Any], claimed).rowcount != 1:
                return None
            row = session.get(IndexJobModel, job_id)
            assert row is not None
            session.refresh(row)
            return self._job_from_row(row)

    def claim_next_job(
        self,
        worker_id: str,
        now: datetime,
        *,
        lease_seconds: int,
    ) -> IndexJob | None:
        if not worker_id.strip() or len(worker_id) > 100:
            raise ValueError("worker id is blank or unbounded")
        if lease_seconds <= 0:
            raise ValueError("lease seconds must be positive")
        now_value = now.isoformat()
        expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        terminal = {
            IndexState.READY.value,
            IndexState.TERMINAL_FAILURE.value,
            IndexState.DELETED.value,
        }
        with self.database.session() as session:
            row = session.scalar(
                select(IndexJobModel)
                .where(
                    IndexJobModel.state.not_in(terminal),
                    or_(
                        IndexJobModel.next_attempt_at.is_(None),
                        IndexJobModel.next_attempt_at <= now_value,
                    ),
                    or_(
                        IndexJobModel.lease_owner.is_(None),
                        IndexJobModel.lease_expires_at.is_(None),
                        IndexJobModel.lease_expires_at <= now_value,
                    ),
                )
                .order_by(IndexJobModel.created_at, IndexJobModel.id)
                .limit(1)
            )
            if row is None:
                return None
            claimed = session.execute(
                update(IndexJobModel)
                .where(
                    IndexJobModel.id == row.id,
                    or_(
                        IndexJobModel.lease_owner.is_(None),
                        IndexJobModel.lease_expires_at.is_(None),
                        IndexJobModel.lease_expires_at <= now_value,
                    ),
                )
                .values(lease_owner=worker_id, lease_expires_at=expires)
            )
            if cast(CursorResult[Any], claimed).rowcount != 1:
                return None
            session.flush()
            session.refresh(row)
            return self._job_from_row(row)

    def release_job_lease(self, job_id: str, worker_id: str) -> bool:
        with self.database.session() as session:
            released = session.execute(
                update(IndexJobModel)
                .where(
                    IndexJobModel.id == job_id,
                    IndexJobModel.lease_owner == worker_id,
                )
                .values(lease_owner=None, lease_expires_at=None)
            )
            return cast(CursorResult[Any], released).rowcount == 1

    def reclaim_expired_jobs(self, now: datetime) -> int:
        with self.database.session() as session:
            reclaimed = session.execute(
                update(IndexJobModel)
                .where(
                    IndexJobModel.lease_owner.is_not(None),
                    IndexJobModel.lease_expires_at <= now.isoformat(),
                )
                .values(lease_owner=None, lease_expires_at=None)
            )
            return cast(CursorResult[Any], reclaimed).rowcount

    @staticmethod
    def _store_row(store: ProviderStore) -> ProviderStoreModel:
        return ProviderStoreModel(
            id=store.id,
            store_key=str(store.store_key),
            provider=store.provider,
            provider_store_name=store.provider_store_name,
            embedding_model=store.embedding_model,
            authority_namespace=store.authority_namespace,
            course_id=store.course_id,
            exam_id=store.exam_id,
            state=store.state.value,
            generation=store.generation,
            is_current=store.is_current,
            created_at=store.created_at,
            updated_at=store.updated_at,
        )

    @staticmethod
    def _copy_store(row: ProviderStoreModel, store: ProviderStore) -> None:
        row.store_key = str(store.store_key)
        row.provider = store.provider
        row.provider_store_name = store.provider_store_name
        row.embedding_model = store.embedding_model
        row.authority_namespace = store.authority_namespace
        row.course_id = store.course_id
        row.exam_id = store.exam_id
        row.state = store.state.value
        row.generation = store.generation
        row.is_current = store.is_current
        row.created_at = store.created_at
        row.updated_at = store.updated_at

    @staticmethod
    def _store_from_row(row: ProviderStoreModel) -> ProviderStore:
        return ProviderStore(
            id=row.id,
            store_key=StoreKey.parse(row.store_key),
            provider=row.provider,
            provider_store_name=row.provider_store_name,
            embedding_model=row.embedding_model,
            authority_namespace=row.authority_namespace,
            course_id=row.course_id,
            exam_id=row.exam_id,
            state=IndexState(row.state),
            generation=row.generation,
            is_current=row.is_current,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _document_row(document: ProviderDocument) -> ProviderDocumentModel:
        return ProviderDocumentModel(
            id=document.id,
            store_id=document.store_id,
            provider=document.provider,
            provider_document_id=document.provider_document_id,
            source_revision_id=document.source_revision_id,
            input_key=document.input_key,
            input_kind=document.input_kind,
            input_sha256=document.input_sha256,
            provider_file_name=document.provider_file_name,
            provider_document_name=document.provider_document_name,
            provider_operation_name=document.provider_operation_name,
            input_byte_count=document.input_byte_count,
            metadata_json=document.metadata_json,
            state=document.state.value,
            retry_count=document.retry_count,
            last_error_category=document.last_error_category,
            created_at=document.created_at,
            updated_at=document.updated_at,
        )

    @staticmethod
    def _copy_document(row: ProviderDocumentModel, document: ProviderDocument) -> None:
        row.store_id = document.store_id
        row.provider = document.provider
        row.provider_document_id = document.provider_document_id
        row.source_revision_id = document.source_revision_id
        row.input_key = document.input_key
        row.input_kind = document.input_kind
        row.input_sha256 = document.input_sha256
        row.provider_file_name = document.provider_file_name
        row.provider_document_name = document.provider_document_name
        row.provider_operation_name = document.provider_operation_name
        row.input_byte_count = document.input_byte_count
        row.metadata_json = document.metadata_json
        row.state = document.state.value
        row.retry_count = document.retry_count
        row.last_error_category = document.last_error_category
        row.created_at = document.created_at
        row.updated_at = document.updated_at

    @staticmethod
    def _document_from_row(row: ProviderDocumentModel) -> ProviderDocument:
        try:
            metadata = json.loads(row.metadata_json)
        except (TypeError, ValueError) as error:
            raise ValueError("persisted provider document metadata is invalid JSON") from error
        return ProviderDocument(
            id=row.id,
            store_id=row.store_id,
            provider=row.provider,
            provider_document_id=row.provider_document_id,
            source_revision_id=row.source_revision_id,
            input_key=row.input_key,
            input_kind=row.input_kind,
            input_sha256=row.input_sha256,
            provider_file_name=row.provider_file_name,
            provider_document_name=row.provider_document_name,
            provider_operation_name=row.provider_operation_name,
            input_byte_count=row.input_byte_count,
            metadata=metadata,
            state=IndexState(row.state),
            retry_count=row.retry_count,
            last_error_category=row.last_error_category,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _job_row(job: IndexJob) -> IndexJobModel:
        return IndexJobModel(
            id=job.id,
            store_id=job.store_id,
            source_revision_id=job.source_revision_id,
            operation_kind=job.operation_kind,
            lease_token=job.lease_token,
            provider_document_id=job.provider_document_id,
            provider_operation_name=job.provider_operation_name,
            state=job.state.value,
            retry_count=job.retry_count,
            last_error_category=job.last_error_category,
            last_error_message=job.last_error_message,
            next_attempt_at=job.next_attempt_at,
            lease_owner=job.lease_owner,
            lease_expires_at=job.lease_expires_at,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )

    @staticmethod
    def _copy_job(row: IndexJobModel, job: IndexJob) -> None:
        row.store_id = job.store_id
        row.source_revision_id = job.source_revision_id
        row.operation_kind = job.operation_kind
        row.lease_token = job.lease_token
        row.provider_document_id = job.provider_document_id
        row.provider_operation_name = job.provider_operation_name
        row.state = job.state.value
        row.retry_count = job.retry_count
        row.last_error_category = job.last_error_category
        row.last_error_message = job.last_error_message
        row.next_attempt_at = job.next_attempt_at
        row.lease_owner = job.lease_owner
        row.lease_expires_at = job.lease_expires_at
        row.created_at = job.created_at
        row.updated_at = job.updated_at

    @staticmethod
    def _job_from_row(row: IndexJobModel) -> IndexJob:
        return IndexJob(
            id=row.id,
            store_id=row.store_id,
            source_revision_id=row.source_revision_id,
            operation_kind=row.operation_kind,
            lease_token=row.lease_token,
            provider_document_id=row.provider_document_id,
            provider_operation_name=row.provider_operation_name,
            state=IndexState(row.state),
            retry_count=row.retry_count,
            last_error_category=row.last_error_category,
            last_error_message=row.last_error_message,
            next_attempt_at=row.next_attempt_at,
            lease_owner=row.lease_owner,
            lease_expires_at=row.lease_expires_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


def _key_value(value: StoreKey | str) -> str:
    return str(value if isinstance(value, StoreKey) else StoreKey.parse(value))


__all__ = ["IndexRepository"]
