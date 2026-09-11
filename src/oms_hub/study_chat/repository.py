"""Durable owner-scoped requests; provider completion precedes answer acceptance."""

import hashlib
import json
import re
from dataclasses import asdict
from typing import Literal, cast
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from oms_hub.anki.sources import SourcePassage
from oms_hub.llm.codex_session import SessionLifecycle
from oms_hub.models import ChatConversationModel, ChatRequestModel, utc_now
from oms_hub.study_chat.contracts import (
    BeginResult,
    ChatAnswer,
    ChatMode,
    ChatRequest,
    Conversation,
    OwnerId,
    RequestState,
    SourceSnapshot,
    StoredRequest,
    UuidId,
    validate_answer,
)
from oms_hub.study_chat.sources import ChatSources, SessionFactory, select_passages

_ACTIVE = ("pending", "running")
_NEXT_PHASES: dict[str | None, set[str]] = {
    None: {"dispatching", "failed", "interrupted"},
    "dispatching": {"thread_created", "failed", "interrupted"},
    "thread_created": {"turn_started", "failed", "interrupted"},
    "turn_started": {"completed", "failed", "interrupted"},
}


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


class ChatRepository:
    def __init__(self, session_factory: SessionFactory, *, sources: ChatSources) -> None:
        self.sessions = session_factory
        self.sources = sources

    def create(self, owner_id: str, mode: ChatMode, revision_ids: tuple[int, ...]) -> str:
        identity = str(uuid4())
        # Reuse the same boundary validation used for incoming questions.
        ChatRequest(str(uuid4()), owner_id, identity, mode, "create", revision_ids)
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            snapshots = self.sources.snapshot(owner_id, revision_ids)
            session.add(
                ChatConversationModel(
                    id=identity,
                    owner_id=owner_id,
                    mode=mode,
                    source_snapshot_json=_json([s.model_dump() for s in snapshots]),
                )
            )
        return identity

    def load(self, conversation_id: str, *, owner_id: str) -> Conversation:
        with self.sessions() as session:
            return self._conversation(self._owned(session, conversation_id, owner_id))

    def load_request(self, request_id: str, *, owner_id: str) -> StoredRequest:
        with self.sessions() as session:
            row, conversation = self._owned_request(session, request_id, owner_id)
            return self._record(row, conversation)

    def recent_requests(self, conversation_id: str, *, owner_id: str) -> tuple[StoredRequest, ...]:
        with self.sessions() as session:
            conversation = self._owned(session, conversation_id, owner_id)
            if conversation.cleared_at:
                return ()
            rows = session.scalars(
                select(ChatRequestModel)
                .where(
                    ChatRequestModel.conversation_id == conversation_id,
                )
                .order_by(ChatRequestModel.created_at.desc(), ChatRequestModel.request_id.desc())
                .limit(20)
            ).all()
            return tuple(self._record(row, conversation) for row in reversed(rows))

    def begin(self, request: ChatRequest, *, model: str) -> BeginResult:
        if not model.strip() or model != model.strip() or len(model) > 200:
            raise ValueError("invalid model")
        with self.sessions() as session:
            # SQLite is the Hub's store; serialize idempotency/history across repository instances.
            session.execute(text("BEGIN IMMEDIATE"))
            conversation = self._owned(session, request.conversation_id, request.owner_id)
            self._scope(conversation, request)
            existing = session.get(ChatRequestModel, request.request_id)
            if existing is not None:
                if existing.conversation_id != conversation.id:
                    raise ValueError("request identity conflict")
                stored = self._record(existing, conversation)
                if stored.request != request or stored.model != model:
                    raise ValueError("request content conflict")
                return BeginResult(stored, False)
            if session.scalar(
                select(ChatRequestModel.request_id).where(
                    ChatRequestModel.conversation_id == conversation.id,
                    ChatRequestModel.state.in_(_ACTIVE),
                )
            ):
                raise ValueError("conversation has an active request")
            snapshots = self._conversation(conversation).sources
            evidence = (
                select_passages(
                    request.question, self.sources.passages(request.owner_id, snapshots)
                )
                if snapshots
                else ()
            )
            history = self._history(session, conversation.id)
            digest = hashlib.sha256(
                _json(
                    {
                        "request": asdict(request),
                        "model": model,
                        "sources": json.loads(conversation.source_snapshot_json),
                        "evidence": [asdict(p) for p in evidence],
                        "history": history,
                    }
                ).encode()
            ).hexdigest()
            row = ChatRequestModel(
                request_id=request.request_id,
                conversation_id=conversation.id,
                request_sha256=digest,
                question=request.question,
                model=model,
                evidence_json=_json([asdict(p) for p in evidence]),
                history_request_ids_json=_json(history),
                state="pending",
            )
            session.add(row)
            conversation.updated_at = utc_now()
            session.flush()
            return BeginResult(self._record(row, conversation), True)

    def record_lifecycle(
        self,
        request_id: str,
        event: SessionLifecycle,
        *,
        owner_id: str,
    ) -> None:
        if event.request_id != request_id:
            raise ValueError("lifecycle request mismatch")
        for value in (event.thread_id, event.turn_id):
            if value is not None and (not value.strip() or len(value) > 200):
                raise ValueError("invalid provider identity")
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row, conversation = self._owned_request(session, request_id, owner_id)
            self._open(conversation)
            if row.state not in _ACTIVE:
                raise ValueError("request is terminal")
            if event.phase == "dispatching" and row.provider_phase is not None:
                raise ValueError("dispatch cannot be replayed")
            history = json.loads(row.lifecycle_json)
            event_data = asdict(event)
            if (
                history
                and {k: v for k, v in history[-1].items() if k != "recorded_at"} == event_data
            ):
                return
            if event.phase not in _NEXT_PHASES.get(
                row.provider_phase,
                set(),
            ):
                raise ValueError("invalid lifecycle transition")
            if row.thread_id is not None and event.thread_id != row.thread_id:
                raise ValueError("provider thread identity changed")
            if row.turn_id is not None and event.turn_id != row.turn_id:
                raise ValueError("provider turn identity changed")
            if event.phase == "dispatching" and (event.thread_id or event.turn_id):
                raise ValueError("dispatching cannot have provider identities")
            if event.phase == "thread_created" and (not event.thread_id or event.turn_id):
                raise ValueError("thread_created requires only thread identity")
            if event.phase in {"turn_started", "completed"} and not (
                event.thread_id and event.turn_id
            ):
                raise ValueError("lifecycle missing provider identities")
            if event.turn_id and not event.thread_id:
                raise ValueError("turn identity requires thread")
            if event.phase == "dispatching":
                self.sources.validate(owner_id, self._conversation(conversation).sources)
            row.provider_phase = event.phase
            row.thread_id, row.turn_id = event.thread_id, event.turn_id
            row.state = event.phase if event.phase in {"failed", "interrupted"} else "running"
            row.lifecycle_json = _json([*history, event_data | {"recorded_at": utc_now()}])
            row.updated_at = utc_now()

    def append(self, request: ChatRequest, answer: ChatAnswer) -> None:
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row, conversation = self._owned_request(session, request.request_id, request.owner_id)
            self._scope(conversation, request)
            stored = self._record(row, conversation)
            if stored.request != request:
                raise ValueError("request content conflict")
            if row.state == "completed":
                if stored.answer != answer:
                    raise ValueError("answer content conflict")
                return
            if row.state not in _ACTIVE:
                raise ValueError("request is terminal")
            validate_answer(answer, {p.passage_id for p in stored.evidence}, mode=request.mode)
            if answer.status == "answered" and row.provider_phase != "completed":
                raise ValueError("provider completion is required")
            if row.provider_phase is not None and row.provider_phase != "completed":
                raise ValueError("provider has not completed")
            if answer.status == "no_support" and request.mode != "lecture":
                raise ValueError("no_support requires lecture mode")
            if request.mode == "medical_reference" and answer.status != "unavailable":
                raise ValueError("medical reference access unavailable")
            self.sources.validate(request.owner_id, self._conversation(conversation).sources)
            row.answer_status, row.answer_text = answer.status, answer.text
            row.citation_ids_json = _json(answer.citation_ids)
            row.state = "completed"
            row.updated_at = conversation.updated_at = utc_now()

    def fail(
        self,
        request_id: str,
        *,
        owner_id: str,
        error_code: str,
        interrupted: bool = False,
    ) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", error_code):
            raise ValueError("error must be a safe code")
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row, _ = self._owned_request(session, request_id, owner_id)
            if row.state == "completed":
                raise ValueError("accepted answer is terminal")
            row.state = "interrupted" if interrupted or row.state == "interrupted" else "failed"
            row.error_code, row.updated_at = error_code, utc_now()

    def clear(self, conversation_id: str, *, owner_id: str) -> None:
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            conversation = self._owned(session, conversation_id, owner_id)
            conversation.cleared_at = conversation.updated_at = utc_now()
            for row in session.scalars(
                select(ChatRequestModel).where(
                    ChatRequestModel.conversation_id == conversation_id,
                    ChatRequestModel.state.in_(_ACTIVE),
                )
            ):
                row.state, row.error_code = "interrupted", "conversation_cleared"
                row.updated_at = utc_now()

    def interrupt_pending(self, *, owner_id: str) -> int:
        """Explicit startup recovery only, after O has reconciled the owned client process."""
        TypeAdapter(OwnerId).validate_python(owner_id)
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            rows = session.scalars(
                select(ChatRequestModel)
                .join(ChatConversationModel)
                .where(
                    ChatConversationModel.owner_id == owner_id,
                    ChatRequestModel.state.in_(_ACTIVE),
                )
            ).all()
            for row in rows:
                row.state, row.error_code = "interrupted", "restart_interrupted"
                row.updated_at = utc_now()
            return len(rows)

    @staticmethod
    def _history(session: Session, conversation_id: str) -> tuple[str, ...]:
        rows = session.scalars(
            select(ChatRequestModel)
            .where(
                ChatRequestModel.conversation_id == conversation_id,
                ChatRequestModel.state == "completed",
                ChatRequestModel.answer_status.in_(("answered", "no_support")),
            )
            .order_by(ChatRequestModel.created_at.desc(), ChatRequestModel.request_id.desc())
            .limit(12)
        )
        selected: list[str] = []
        remaining = 12000
        for row in rows:
            size = len(row.question) + len(row.answer_text or "")
            if size > remaining:
                break
            selected.append(row.request_id)
            remaining -= size
        return tuple(reversed(selected))

    @staticmethod
    def _owned(session: Session, conversation_id: str, owner_id: str) -> ChatConversationModel:
        TypeAdapter(UuidId).validate_python(conversation_id)
        TypeAdapter(OwnerId).validate_python(owner_id)
        row = session.get(ChatConversationModel, conversation_id)
        if row is None or row.owner_id != owner_id:
            raise PermissionError("conversation unavailable")
        return row

    def _owned_request(
        self,
        session: Session,
        request_id: str,
        owner_id: str,
    ) -> tuple[ChatRequestModel, ChatConversationModel]:
        TypeAdapter(UuidId).validate_python(request_id)
        row = session.get(ChatRequestModel, request_id)
        if row is None:
            raise PermissionError("request unavailable")
        return row, self._owned(session, row.conversation_id, owner_id)

    @staticmethod
    def _open(row: ChatConversationModel) -> None:
        if row.cleared_at is not None:
            raise ValueError("conversation is cleared")

    def _scope(self, row: ChatConversationModel, request: ChatRequest) -> None:
        self._open(row)
        snapshots = self._conversation(row).sources
        if (
            row.id != request.conversation_id
            or row.mode != request.mode
            or tuple(s.revision_id for s in snapshots) != request.revision_ids
        ):
            raise ValueError("conversation scope is immutable")

    @staticmethod
    def _conversation(row: ChatConversationModel) -> Conversation:
        return Conversation(
            row.id,
            row.owner_id,
            cast(ChatMode, row.mode),
            tuple(SourceSnapshot.model_validate(s) for s in json.loads(row.source_snapshot_json)),
            row.cleared_at,
        )

    def _record(self, row: ChatRequestModel, conversation: ChatConversationModel) -> StoredRequest:
        snapshot = self._conversation(conversation)
        answer = (
            None
            if row.answer_status is None
            else ChatAnswer(
                cast(Literal["answered", "no_support", "unavailable"], row.answer_status),
                row.answer_text or "",
                tuple(json.loads(row.citation_ids_json)),
            )
        )
        return StoredRequest(
            ChatRequest(
                row.request_id,
                conversation.owner_id,
                conversation.id,
                snapshot.mode,
                row.question,
                tuple(s.revision_id for s in snapshot.sources),
            ),
            row.model,
            TypeAdapter(tuple[SourcePassage, ...]).validate_json(row.evidence_json),
            tuple(json.loads(row.history_request_ids_json)),
            cast(RequestState, row.state),
            row.provider_phase,
            row.thread_id,
            row.turn_id,
            answer,
            row.error_code,
        )
