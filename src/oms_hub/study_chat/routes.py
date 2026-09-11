"""Private chat endpoints; authentication and CSRF remain owned by the Hub."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, StringConstraints, TypeAdapter
from sqlalchemy.exc import SQLAlchemyError

from oms_hub.llm.codex_session import SessionError
from oms_hub.study_chat.contracts import (
    ChatAnswer,
    ChatMode,
    ChatRequest,
    OwnerId,
    RevisionIds,
    StoredRequest,
    UuidId,
)
from oms_hub.study_chat.service import ChatService
from oms_hub.web.csrf import require_form_csrf

router = APIRouter(prefix="/study/chat")
templates = Jinja2Templates(directory=str(Path(__file__).parents[1] / "web" / "templates"))
_HEADERS = {"Cache-Control": "private, no-store"}


class ConversationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: ChatMode
    revision_ids: RevisionIds = ()


class AnswerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UuidId
    conversation_id: UuidId
    question: Annotated[
        str, StringConstraints(strict=True, min_length=1, max_length=20000, pattern=r"\S")
    ]


def _owner(request: Request, *, mutation: bool = False) -> str:
    owner = getattr(request.state, "study_owner_id", None)
    if not isinstance(owner, str) or not owner.strip():
        raise HTTPException(401, "Private Study Hub access is required.")
    TypeAdapter(OwnerId).validate_python(owner)
    if mutation:
        require_form_csrf(request, None)
    return owner


def _access(request: Request, *, mutation: bool = False) -> tuple[str, ChatService]:
    owner = _owner(request, mutation=mutation)
    service = getattr(request.app.state, "study_chat_service", None)
    if service is None:
        raise HTTPException(503, "Study chat has not been configured.")
    return owner, cast(ChatService, service)


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except PermissionError:
        raise HTTPException(403, "Conversation or source is unavailable.") from None
    except (ValueError, KeyError, OSError):
        raise HTTPException(
            409,
            "The request, conversation, or selected sources changed. "
            "Check the current request before starting a new one.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            503,
            "Chat state could not be saved. Check the current request before starting a new one.",
        ) from None


def _payload(row: StoredRequest) -> dict[str, object]:
    answer = row.answer
    if answer is None and row.state in {"failed", "interrupted"}:
        answer = ChatAnswer("unavailable", str(SessionError(row.error_code or "interrupted")), ())
    cited = set(answer.citation_ids) if answer else set()
    return {
        "request_id": row.request.request_id,
        "question": row.request.question,
        "state": row.state,
        "provider_phase": row.provider_phase,
        "error_code": row.error_code,
        "answer": asdict(answer) if answer else None,
        "citations": [
            {
                "id": p.passage_id,
                "label": p.citation,
                "url": f"/study/chat/requests/{row.request.request_id}/citations/{p.passage_id}",
            }
            for p in row.evidence
            if p.passage_id in cited
        ],
    }


@router.get("", response_class=HTMLResponse)
def page(request: Request, conversation_id: UuidId | None = None) -> HTMLResponse:
    _owner(request)
    if getattr(request.app.state, "study_chat_service", None) is None:
        return templates.TemplateResponse(
            request=request,
            name="study_chat.html",
            context={"configured": False, "conversation": None, "sources": [], "selected_ids": ()},
            headers=_HEADERS,
        )
    owner, service = _access(request)
    with _errors():
        conversation = (
            service.repository.load(conversation_id, owner_id=owner) if conversation_id else None
        )
        if conversation and conversation.cleared_at:
            conversation = None
        return templates.TemplateResponse(
            request=request,
            name="study_chat.html",
            context={
                "configured": True,
                "conversation": conversation,
                "sources": service.repository.sources.options(owner),
                "selected_ids": tuple(s.revision_id for s in conversation.sources)
                if conversation
                else (),
            },
            headers=_HEADERS,
        )


@router.post("/conversations")
def create_conversation(request: Request, body: ConversationInput) -> JSONResponse:
    owner, service = _access(request, mutation=True)
    with _errors():
        identity = service.repository.create(owner, body.mode, body.revision_ids)
        return JSONResponse({"conversation_id": identity}, headers=_HEADERS)


@router.get("/conversations/{conversation_id}")
def conversation(request: Request, conversation_id: UuidId) -> JSONResponse:
    owner, service = _access(request)
    with _errors():
        stored = service.repository.load(conversation_id, owner_id=owner)
        return JSONResponse(
            {
                "conversation_id": stored.id,
                "mode": stored.mode,
                "revision_ids": [s.revision_id for s in stored.sources],
                "cleared": bool(stored.cleared_at),
                "requests": [
                    _payload(row)
                    for row in service.repository.recent_requests(conversation_id, owner_id=owner)
                ],
            },
            headers=_HEADERS,
        )


@router.post("/answer")
def answer(request: Request, body: AnswerInput) -> JSONResponse:
    owner, service = _access(request, mutation=True)
    with _errors():
        conversation = service.repository.load(body.conversation_id, owner_id=owner)
        service.answer(
            ChatRequest(
                body.request_id,
                owner,
                body.conversation_id,
                conversation.mode,
                body.question,
                tuple(s.revision_id for s in conversation.sources),
            )
        )
        return JSONResponse(
            _payload(service.repository.load_request(body.request_id, owner_id=owner)),
            headers=_HEADERS,
        )


@router.get("/requests/{request_id}")
def request_status(request: Request, request_id: UuidId) -> JSONResponse:
    owner, service = _access(request)
    with _errors():
        return JSONResponse(
            _payload(service.repository.load_request(request_id, owner_id=owner)), headers=_HEADERS
        )


@router.post("/requests/{request_id}/cancel")
def cancel(request: Request, request_id: UuidId) -> JSONResponse:
    owner, service = _access(request, mutation=True)
    with _errors():
        service.cancel(request_id, owner_id=owner)
        return JSONResponse({"state": "interrupted"}, headers=_HEADERS)


@router.post("/conversations/{conversation_id}/clear")
def clear(request: Request, conversation_id: UuidId) -> JSONResponse:
    owner, service = _access(request, mutation=True)
    with _errors():
        service.clear(conversation_id, owner_id=owner)
        return JSONResponse({"cleared": True}, headers=_HEADERS)


@router.get("/requests/{request_id}/citations/{passage_id}", response_class=PlainTextResponse)
def citation(request: Request, request_id: UuidId, passage_id: str) -> PlainTextResponse:
    owner, service = _access(request)
    with _errors():
        row = service.repository.load_request(request_id, owner_id=owner)
        conversation = service.repository.load(row.request.conversation_id, owner_id=owner)
        if (
            conversation.cleared_at
            or row.answer is None
            or passage_id not in row.answer.citation_ids
        ):
            raise HTTPException(404, "Citation unavailable.")
        service.repository.sources.validate(owner, conversation.sources)
        for passage in row.evidence:
            if passage.passage_id == passage_id:
                return PlainTextResponse(f"{passage.citation}\n\n{passage.text}", headers=_HEADERS)
        raise HTTPException(404, "Citation unavailable.")
