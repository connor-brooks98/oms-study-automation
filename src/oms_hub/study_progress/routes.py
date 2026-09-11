from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy.exc import SQLAlchemyError

from oms_hub.study_chat.contracts import UuidId
from oms_hub.study_chat.routes import _owner
from oms_hub.study_progress.service import ProgressService
from oms_hub.study_progress.sessions import AnswerSelection, Duration, StudySessionService
from oms_hub.study_progress.taxonomy import TAXONOMIES
from oms_hub.web.csrf import require_form_csrf
from oms_hub.web.public_quiz_routes import _player_asset_version

router = APIRouter(prefix="/study")
templates = Jinja2Templates(directory=str(Path(__file__).parents[1] / "web" / "templates"))
_HEADERS = {"Cache-Control": "private, no-store"}
QuizToken = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
]


class CreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quiz_token: QuizToken


class AnswerInput(AnswerSelection):
    elapsed_ms: Duration | None = None


def _sessions(request: Request) -> StudySessionService:
    service = getattr(request.app.state, "study_session_service", None)
    if service is None:
        raise HTTPException(503, "Personal study sessions have not been configured.")
    return cast(StudySessionService, service)


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except PermissionError:
        raise HTTPException(403, "Session or publication is unavailable.") from None
    except (ValueError, KeyError):
        raise HTTPException(409, "This session or answer conflicts with its saved state.") from None
    except (SQLAlchemyError, OSError):
        raise HTTPException(503, "Study state is unavailable. Retry the same answer.") from None


@router.get("/sessions/new", response_class=HTMLResponse)
def new_session(request: Request, quiz_token: QuizToken) -> HTMLResponse:
    _owner(request)
    _sessions(request)
    return templates.TemplateResponse(
        request=request,
        name="study_session.html",
        context={
            "view": None,
            "quiz_token": quiz_token,
            "csrf_token": getattr(
                request.state, "csrf_token", request.cookies.get("study_hub_csrf", "")
            ),
        },
        headers=_HEADERS,
    )


@router.post("/sessions/start")
def start_session(
    request: Request, quiz_token: Annotated[QuizToken, Form()], csrf_token: Annotated[str, Form()]
) -> RedirectResponse:
    owner = _owner(request)
    require_form_csrf(request, csrf_token)
    with _errors():
        view = _sessions(request).create(owner, quiz_token)
    return RedirectResponse(f"/study/sessions/{view.id}", status_code=303, headers=_HEADERS)


@router.post("/sessions")
def create_session(request: Request, body: CreateInput) -> JSONResponse:
    owner = _owner(request, mutation=True)
    with _errors():
        view = _sessions(request).create(owner, body.quiz_token)
    return JSONResponse(
        {"session_id": view.id, "url": f"/study/sessions/{view.id}"}, headers=_HEADERS
    )


@router.get("/sessions/{session_id}", response_class=HTMLResponse)
def session_page(request: Request, session_id: UuidId) -> HTMLResponse:
    owner = _owner(request)
    with _errors():
        view = _sessions(request).load(session_id, owner_id=owner)
    return templates.TemplateResponse(
        request=request,
        name="study_session.html",
        context={"view": view, "player_asset_version": _player_asset_version()},
        headers=_HEADERS,
    )


@router.get("/sessions/{session_id}/content")
def session_content(request: Request, session_id: UuidId) -> JSONResponse:
    owner = _owner(request)
    with _errors():
        view = _sessions(request).load(session_id, owner_id=owner)
    return JSONResponse(
        {
            "token": view.id,
            "version": view.quiz_version,
            "title": view.title,
            "questions": view.questions,
            "closed": view.closed,
        },
        headers=_HEADERS,
    )


@router.post("/sessions/{session_id}/answers/{attempt_id}")
def answer(
    request: Request, session_id: UuidId, attempt_id: UuidId, body: AnswerInput
) -> JSONResponse:
    owner = _owner(request, mutation=True)
    submission = AnswerSelection.model_validate(body.model_dump(exclude={"elapsed_ms"}))
    with _errors():
        receipt = _sessions(request).answer(
            session_id, attempt_id, submission, owner_id=owner, elapsed_ms=body.elapsed_ms
        )
    return JSONResponse(
        receipt.feedback
        | {
            "attempt_id": receipt.attempt_id,
            "bank_attempt_id": receipt.bank_attempt_id,
            "result": receipt.result,
        },
        headers=_HEADERS,
    )


@router.post("/sessions/{session_id}/close")
def close_session(request: Request, session_id: UuidId) -> JSONResponse:
    owner = _owner(request, mutation=True)
    with _errors():
        _sessions(request).close(session_id, owner_id=owner)
    return JSONResponse({"closed": True}, headers=_HEADERS)


@router.get("/progress", response_class=HTMLResponse)
def progress(request: Request, first_only: bool = False) -> HTMLResponse:
    owner = _owner(request)
    service = getattr(request.app.state, "study_progress_service", None)
    with _errors():
        report = (
            cast(ProgressService, service).report(owner, first_only=first_only) if service else None
        )
    return templates.TemplateResponse(
        request=request,
        name="study_progress.html",
        context={"report": report, "taxonomies": TAXONOMIES},
        headers=_HEADERS,
    )


@router.get("/progress/data")
def progress_data(request: Request, first_only: bool = False) -> JSONResponse:
    owner = _owner(request)
    service = getattr(request.app.state, "study_progress_service", None)
    if service is None:
        raise HTTPException(503, "Personal progress has not been configured.")
    with _errors():
        report = cast(ProgressService, service).report(owner, first_only=first_only)
    return JSONResponse(jsonable_encoder(asdict(report)), headers=_HEADERS)
