"""Actor-scoped persistence for Ask conversations and retrieval provenance."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from oms_hub.ask.models import (
    AskMessage,
    AskMode,
    AskPageContext,
    AskThread,
    QuizPageContext,
)
from oms_hub.db import Database
from oms_hub.models import utc_now
from oms_hub.providers.contracts import RetrievalScope, TruthMode


class _AskBase(DeclarativeBase):
    pass


class _AskThreadRow(_AskBase):
    __tablename__ = "ask_threads"
    __table_args__ = (Index("ix_ask_threads_actor_scope", "actor_id", "scope_json"),)

    thread_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(320), nullable=False)
    mode: Mapped[str] = mapped_column(String(40), nullable=False)
    scope_json: Mapped[str] = mapped_column(Text, nullable=False)
    page_context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class _AskMessageRow(_AskBase):
    __tablename__ = "ask_messages"
    __table_args__ = (
        UniqueConstraint("thread_id", "sequence", name="uq_ask_messages_thread_sequence"),
        Index("ix_ask_messages_thread_order", "thread_id", "sequence"),
    )

    message_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("ask_threads.thread_id", ondelete="CASCADE"), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)


class _RetrievalRunRow(_AskBase):
    __tablename__ = "retrieval_runs"
    __table_args__ = (Index("ix_retrieval_runs_thread_order", "thread_id", "created_at"),)

    retrieval_run_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("ask_threads.thread_id", ondelete="CASCADE"), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(String(320), nullable=False)
    source_snapshot_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_revision_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(200), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(200), nullable=False)
    model: Mapped[str] = mapped_column(String(300), nullable=False)
    validation_outcome: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)


class _RetrievalEvidenceRow(_AskBase):
    __tablename__ = "retrieval_evidence"
    __table_args__ = (Index("ix_retrieval_evidence_evidence_id", "evidence_id"),)

    retrieval_run_id: Mapped[str] = mapped_column(
        ForeignKey("retrieval_runs.retrieval_run_id", ondelete="CASCADE"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    evidence_id: Mapped[str] = mapped_column(String(300), nullable=False)
    source_revision_id: Mapped[str | None] = mapped_column(String(300), nullable=True)


@dataclass(frozen=True, slots=True)
class RetrievalRun:
    retrieval_run_id: str
    thread_id: str
    source_snapshot_hash: str
    evidence_ids: tuple[str, ...]
    source_revision_ids: tuple[str, ...]
    provider_request_id: str | None
    prompt_version: str
    schema_version: str
    model: str
    validation_outcome: str
    created_at: str


@dataclass(frozen=True, slots=True)
class AskThreadView:
    thread: AskThread
    messages: tuple[AskMessage, ...]
    retrieval_runs: tuple[RetrievalRun, ...]
    actor_id: str

    @property
    def thread_id(self) -> str:
        return self.thread.thread_id


AskPageContextValue = AskPageContext | QuizPageContext


class AskRepository:
    """Persist Ask records without registering held central ORM models."""

    def __init__(self, database: Database) -> None:
        self.database = database
        _AskBase.metadata.create_all(database.engine)

    def create_thread(
        self,
        actor_id: str,
        mode: AskMode | AskThread,
        scope: RetrievalScope | None = None,
        page_context: AskPageContextValue | None = None,
        *,
        thread_id: str | None = None,
    ) -> AskThread:
        actor = _require_non_empty(actor_id, "actor_id")
        if isinstance(mode, AskThread):
            if scope is not None or page_context is not None:
                raise ValueError("scope and page_context must be omitted when passing AskThread")
            if thread_id is not None and thread_id != mode.thread_id:
                raise ValueError("thread_id does not match AskThread")
            thread = mode
        else:
            if not isinstance(mode, AskMode):
                raise TypeError("mode must be an AskMode or AskThread")
            if scope is None:
                raise ValueError("scope is required")
            thread = AskThread(
                thread_id=thread_id or str(uuid4()),
                mode=mode,
                scope=scope,
                page_context=page_context,
            )

        scope_json = _scope_json(thread.scope)
        page_context_json = _page_context_json(thread.page_context)
        now = utc_now()
        with self.database.session() as session:
            existing = session.get(_AskThreadRow, thread.thread_id)
            if existing is not None:
                if existing.actor_id != actor:
                    raise KeyError(thread.thread_id)
                if (
                    existing.mode == thread.mode.value
                    and existing.scope_json == scope_json
                    and existing.page_context_json == page_context_json
                ):
                    return thread
                raise ValueError("thread_id already belongs to a different thread")
            session.add(
                _AskThreadRow(
                    thread_id=thread.thread_id,
                    actor_id=actor,
                    mode=thread.mode.value,
                    scope_json=scope_json,
                    page_context_json=page_context_json,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.flush()
        return thread

    def append_user_message(
        self,
        thread_id: str,
        actor_id: str,
        content: str,
        *,
        message_id: str | None = None,
        page_context: AskPageContextValue | None = None,
    ) -> AskMessage:
        return self._append_message(
            thread_id,
            actor_id,
            "user",
            content,
            message_id=message_id,
            page_context=page_context,
        )

    def append_assistant_message(
        self,
        thread_id: str,
        actor_id: str,
        content: str,
        *,
        message_id: str | None = None,
        page_context: AskPageContextValue | None = None,
    ) -> AskMessage:
        return self._append_message(
            thread_id,
            actor_id,
            "assistant",
            content,
            message_id=message_id,
            page_context=page_context,
        )

    def record_retrieval_run(
        self,
        thread_id: str,
        actor_id: str,
        source_snapshot_hash: str,
        evidence_ids: Iterable[str] = (),
        source_revision_ids: Iterable[str] = (),
        provider_request_id: str | None = None,
        prompt_version: str = "",
        schema_version: str = "",
        model: str = "",
        validation_outcome: str = "",
        *,
        retrieval_run_id: str | None = None,
    ) -> RetrievalRun:
        actor = _require_non_empty(actor_id, "actor_id")
        source_hash = _require_non_empty(source_snapshot_hash, "source_snapshot_hash")
        evidence = _normalized_ids(evidence_ids, "evidence_ids")
        revisions = _normalized_ids(source_revision_ids, "source_revision_ids")
        prompt = _require_non_empty(prompt_version, "prompt_version")
        schema = _require_non_empty(schema_version, "schema_version")
        model_name = _require_non_empty(model, "model")
        run_id = _require_non_empty(retrieval_run_id or str(uuid4()), "retrieval_run_id")
        provider_id = (
            _require_non_empty(provider_request_id, "provider_request_id")
            if provider_request_id is not None
            else None
        )
        outcome = _require_non_empty(validation_outcome, "validation_outcome")
        now = utc_now()
        with self.database.session() as session:
            self._require_thread(session, thread_id, actor)
            if session.get(_RetrievalRunRow, run_id) is not None:
                raise ValueError("retrieval_run_id already exists")
            session.add(
                _RetrievalRunRow(
                    retrieval_run_id=run_id,
                    thread_id=thread_id,
                    actor_id=actor,
                    source_snapshot_hash=source_hash,
                    evidence_ids_json=_json_dump(evidence),
                    source_revision_ids_json=_json_dump(revisions),
                    provider_request_id=provider_id,
                    prompt_version=prompt,
                    schema_version=schema,
                    model=model_name,
                    validation_outcome=outcome,
                    created_at=now,
                )
            )
            session.flush()
            for ordinal, evidence_id in enumerate(evidence):
                session.add(
                    _RetrievalEvidenceRow(
                        retrieval_run_id=run_id,
                        ordinal=ordinal,
                        evidence_id=evidence_id,
                        source_revision_id=(
                            revisions[ordinal] if ordinal < len(revisions) else None
                        ),
                    )
                )
            session.flush()
            row = session.get(_RetrievalRunRow, run_id)
            assert row is not None
            return _retrieval_run(row)

    def get_thread(self, thread_id: str, actor_id: str) -> AskThreadView:
        actor = _require_non_empty(actor_id, "actor_id")
        with self.database.session() as session:
            row = self._require_thread(session, thread_id, actor)
            messages = tuple(
                _message(message)
                for message in session.scalars(
                    select(_AskMessageRow)
                    .where(
                        _AskMessageRow.thread_id == thread_id,
                        _AskMessageRow.actor_id == actor,
                    )
                    .order_by(_AskMessageRow.sequence, _AskMessageRow.message_id)
                ).all()
            )
            retrieval_runs = tuple(
                _retrieval_run(run)
                for run in session.scalars(
                    select(_RetrievalRunRow)
                    .where(
                        _RetrievalRunRow.thread_id == thread_id,
                        _RetrievalRunRow.actor_id == actor,
                    )
                    .order_by(_RetrievalRunRow.created_at, _RetrievalRunRow.retrieval_run_id)
                ).all()
            )
            return AskThreadView(
                thread=_thread(row),
                messages=messages,
                retrieval_runs=retrieval_runs,
                actor_id=actor,
            )

    def list_threads(
        self,
        scope: RetrievalScope,
        actor_id: str,
    ) -> list[AskThread]:
        actor = _require_non_empty(actor_id, "actor_id")
        scope_json = _scope_json(scope)
        with self.database.session() as session:
            return [
                _thread(row)
                for row in session.scalars(
                    select(_AskThreadRow)
                    .where(
                        _AskThreadRow.actor_id == actor,
                        _AskThreadRow.scope_json == scope_json,
                    )
                    .order_by(_AskThreadRow.created_at, _AskThreadRow.thread_id)
                ).all()
            ]

    def delete_thread(self, thread_id: str, actor_id: str) -> bool:
        actor = _require_non_empty(actor_id, "actor_id")
        with self.database.session() as session:
            self._require_thread(session, thread_id, actor)
            self._delete_thread_rows(session, thread_id, actor)
        return True

    def delete_threads_before(self, actor_id: str, before: str | datetime) -> int:
        actor = _require_non_empty(actor_id, "actor_id")
        cutoff = (
            before.isoformat()
            if isinstance(before, datetime)
            else _require_non_empty(before, "before")
        )
        with self.database.session() as session:
            thread_ids = list(
                session.scalars(
                    select(_AskThreadRow.thread_id)
                    .where(
                        _AskThreadRow.actor_id == actor,
                        _AskThreadRow.created_at < cutoff,
                    )
                    .order_by(_AskThreadRow.created_at, _AskThreadRow.thread_id)
                ).all()
            )
            for thread_id in thread_ids:
                self._delete_thread_rows(session, thread_id, actor)
            return len(thread_ids)

    def _append_message(
        self,
        thread_id: str,
        actor_id: str,
        role: str,
        content: str,
        *,
        message_id: str | None,
        page_context: AskPageContextValue | None,
    ) -> AskMessage:
        actor = _require_non_empty(actor_id, "actor_id")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content cannot be empty")
        identifier = _require_non_empty(message_id or str(uuid4()), "message_id")
        with self.database.session() as session:
            thread_row = self._require_thread(session, thread_id, actor)
            stored_thread = _thread(thread_row)
            _assert_context_matches(stored_thread.page_context, page_context)
            if session.get(_AskMessageRow, identifier) is not None:
                raise ValueError("message_id already exists")
            sequence = int(
                session.scalar(
                    select(func.max(_AskMessageRow.sequence)).where(
                        _AskMessageRow.thread_id == thread_id,
                        _AskMessageRow.actor_id == actor,
                    )
                )
                or 0
            ) + 1
            message = AskMessage(
                message_id=identifier,
                thread_id=thread_id,
                role=cast(Any, role),
                content=content,
            )
            session.add(
                _AskMessageRow(
                    message_id=message.message_id,
                    thread_id=thread_id,
                    actor_id=actor,
                    role=message.role,
                    content=message.content,
                    sequence=sequence,
                    created_at=utc_now(),
                )
            )
            thread_row.updated_at = utc_now()
            session.flush()
            return message

    @staticmethod
    def _require_thread(session: Session, thread_id: str, actor_id: str) -> _AskThreadRow:
        identifier = _require_non_empty(thread_id, "thread_id")
        row = session.scalar(
            select(_AskThreadRow).where(
                _AskThreadRow.thread_id == identifier,
                _AskThreadRow.actor_id == actor_id,
            )
        )
        if row is None:
            raise KeyError(identifier)
        return row

    @staticmethod
    def _delete_thread_rows(session: Session, thread_id: str, actor_id: str) -> None:
        run_ids = list(
            session.scalars(
                select(_RetrievalRunRow.retrieval_run_id).where(
                    _RetrievalRunRow.thread_id == thread_id,
                    _RetrievalRunRow.actor_id == actor_id,
                )
            ).all()
        )
        if run_ids:
            session.execute(
                delete(_RetrievalEvidenceRow).where(
                    _RetrievalEvidenceRow.retrieval_run_id.in_(run_ids)
                )
            )
            session.execute(
                delete(_RetrievalRunRow).where(_RetrievalRunRow.retrieval_run_id.in_(run_ids))
            )
        session.execute(
            delete(_AskMessageRow).where(
                _AskMessageRow.thread_id == thread_id,
                _AskMessageRow.actor_id == actor_id,
            )
        )
        session.execute(
            delete(_AskThreadRow).where(
                _AskThreadRow.thread_id == thread_id,
                _AskThreadRow.actor_id == actor_id,
            )
        )


def _thread(row: _AskThreadRow) -> AskThread:
    page_context = _page_context_from_json(row.page_context_json)
    return AskThread(
        thread_id=row.thread_id,
        mode=AskMode(row.mode),
        scope=_scope_from_json(row.scope_json),
        page_context=page_context,
    )


def _message(row: _AskMessageRow) -> AskMessage:
    return AskMessage(
        message_id=row.message_id,
        thread_id=row.thread_id,
        role=cast(Any, row.role),
        content=row.content,
    )


def _retrieval_run(row: _RetrievalRunRow) -> RetrievalRun:
    return RetrievalRun(
        retrieval_run_id=row.retrieval_run_id,
        thread_id=row.thread_id,
        source_snapshot_hash=row.source_snapshot_hash,
        evidence_ids=tuple(cast(list[str], json.loads(row.evidence_ids_json))),
        source_revision_ids=tuple(cast(list[str], json.loads(row.source_revision_ids_json))),
        provider_request_id=row.provider_request_id,
        prompt_version=row.prompt_version,
        schema_version=row.schema_version,
        model=row.model,
        validation_outcome=row.validation_outcome,
        created_at=row.created_at,
    )


def _scope_json(scope: RetrievalScope) -> str:
    return _json_dump(
        {
            "course_id": scope.course_id,
            "exam_id": scope.exam_id,
            "lecture_ids": list(scope.lecture_ids),
            "truth_mode": scope.truth_mode.value,
            "source_revision_ids": list(scope.source_revision_ids),
        }
    )


def _scope_from_json(value: str) -> RetrievalScope:
    payload = cast(dict[str, object], json.loads(value))
    lecture_ids = payload.get("lecture_ids")
    source_revision_ids = payload.get("source_revision_ids")
    if not isinstance(lecture_ids, list) or not isinstance(source_revision_ids, list):
        raise ValueError("stored Ask scope is malformed")
    return RetrievalScope(
        course_id=cast(str, payload["course_id"]),
        exam_id=cast(str | None, payload["exam_id"]),
        lecture_ids=tuple(cast(str, item) for item in lecture_ids),
        truth_mode=TruthMode(cast(str, payload["truth_mode"])),
        source_revision_ids=tuple(cast(str, item) for item in source_revision_ids),
    )


def _page_context_json(value: AskPageContextValue | None) -> str | None:
    if value is None:
        return None
    return _json_dump(value.model_dump(mode="json", exclude_none=True))


def _page_context_from_json(value: str | None) -> AskPageContextValue | None:
    if value is None:
        return None
    payload = cast(dict[str, object], json.loads(value))
    if payload.get("kind") == "quiz_question":
        return QuizPageContext.model_validate(payload)
    return AskPageContext.model_validate(payload)


def _assert_context_matches(
    stored: AskPageContextValue | None,
    supplied: AskPageContextValue | None,
) -> None:
    if isinstance(stored, QuizPageContext):
        if not isinstance(supplied, QuizPageContext):
            raise ValueError(
                "quiz thread append requires explicit matching QuizPageContext"
            )
        if stored != supplied:
            raise ValueError("question context cannot change within a thread")
        return
    if supplied is None:
        return
    if stored != supplied:
        if isinstance(stored, QuizPageContext) or isinstance(supplied, QuizPageContext):
            raise ValueError("question context cannot change within a thread")
        raise ValueError("page context cannot change within a thread")


def _normalized_ids(values: Iterable[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{field_name} must be a sequence of IDs")
    result = tuple(_require_non_empty(value, field_name) for value in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{field_name} cannot contain duplicates")
    return result


def _require_non_empty(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} cannot be empty")
    return value


def _json_dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
