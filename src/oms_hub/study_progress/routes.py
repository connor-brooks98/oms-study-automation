from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from oms_hub.llm.codex_session import SessionError
from oms_hub.models import StudySessionModel, StudySessionQuestionModel, StudyTopicSuggestionModel
from oms_hub.study_chat.contracts import UuidId
from oms_hub.study_chat.routes import _owner
from oms_hub.study_progress.blocks import BlockFilters, BlockService
from oms_hub.study_progress.service import ProgressService
from oms_hub.study_progress.sessions import AnswerSelection, Duration, StudySessionService
from oms_hub.study_progress.tags import TopicService
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
    except SessionError as error:
        raise HTTPException(503, str(error)) from None
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


NativeKey = Annotated[str, StringConstraints(pattern=r"^sha256:[a-f0-9]{64}$")]


class BlockInput(BlockFilters):
    selected_keys: Annotated[tuple[NativeKey, ...], Field(min_length=1, max_length=500)]


def _blocks(request: Request) -> BlockService:
    service = getattr(request.app.state, "study_block_service", None)
    if service is None:
        raise HTTPException(503, "Study blocks have not been configured.")
    return cast(BlockService, service)


def _topics(request: Request) -> TopicService:
    service = getattr(request.app.state, "study_topic_service", None)
    if service is None:
        raise HTTPException(503, "Topic review has not been configured.")
    return cast(TopicService, service)


def _csrf(request: Request) -> str:
    return cast(
        str, getattr(request.state, "csrf_token", request.cookies.get("study_hub_csrf", ""))
    )


@router.get("/blocks", response_class=HTMLResponse)
def blocks_page(
    request: Request,
    course: str | None = None,
    exam: Annotated[list[int] | None, Query()] = None,
    topic: Annotated[list[str] | None, Query()] = None,
    count: int = 20,
) -> HTMLResponse:
    owner = _owner(request)
    service = _blocks(request)
    with _errors():
        catalog = service.catalog(owner)
        filters = (
            BlockFilters(
                course=course,
                exam_numbers=tuple(exam or ()),
                topic_ids=tuple(topic or ()),
                count=count,
            )
            if course and exam
            else None
        )
        chosen = service.preview(catalog, filters) if filters else ()
        with service.sessions._session_factory() as session:
            records = session.execute(
                select(
                    StudySessionModel.id,
                    StudySessionModel.created_at,
                    StudySessionModel.closed_at,
                    func.count(StudySessionQuestionModel.attempt_id),
                )
                .join(StudySessionQuestionModel)
                .where(StudySessionModel.owner_id == owner)
                .group_by(StudySessionModel.id)
                .order_by(StudySessionModel.created_at.desc())
                .limit(20)
            ).all()
    return templates.TemplateResponse(
        request=request,
        name="study_blocks.html",
        context={
            "catalog": catalog,
            "filters": filters,
            "chosen": chosen,
            "csrf_token": _csrf(request),
            "courses": sorted({(q.course, q.course_label) for q in catalog.questions}),
            "exams": sorted({q.exam_number for q in catalog.questions}),
            "topics": sorted(
                {
                    (t.canonical_id, t.label)
                    for q in catalog.questions
                    for t in q.topics
                    if t.canonical_id
                }
            ),
            "recent": [
                {"id": row[0], "created_at": row[1], "closed": bool(row[2]), "count": row[3]}
                for row in records
            ],
        },
        headers=_HEADERS,
    )


@router.post("/blocks")
def create_block(request: Request, body: BlockInput) -> JSONResponse:
    owner = _owner(request, mutation=True)
    with _errors():
        filters = BlockFilters.model_validate(body.model_dump(exclude={"selected_keys"}))
        view = _blocks(request).create(owner, filters, selected_keys=body.selected_keys)
    return JSONResponse(
        {"session_id": view.id, "url": f"/study/sessions/{view.id}"}, headers=_HEADERS
    )


@router.post("/blocks/start")
def start_block(
    request: Request,
    course: Annotated[str, Form()],
    exam: Annotated[list[int], Form()],
    selected_key: Annotated[list[str], Form()],
    csrf_token: Annotated[str, Form()],
    topic: Annotated[list[str] | None, Form()] = None,
    count: Annotated[int, Form()] = 20,
) -> RedirectResponse:
    owner = _owner(request)
    require_form_csrf(request, csrf_token)
    with _errors():
        filters = BlockFilters(
            course=course, exam_numbers=tuple(exam), topic_ids=tuple(topic or ()), count=count
        )
        view = _blocks(request).create(owner, filters, selected_keys=tuple(selected_key))
    return RedirectResponse(f"/study/sessions/{view.id}", status_code=303, headers=_HEADERS)


@router.get("/blocks/questions/{key_id}", response_class=HTMLResponse)
def topic_page(request: Request, key_id: NativeKey) -> HTMLResponse:
    owner = _owner(request)
    service = _topics(request)
    with _errors():
        context = service.context(owner, key_id)
        ready = service.bank.get_question(context.candidate.key)
        accepted = (
            {t.canonical_id for t in ready.topics if t.review_state == "accepted"}
            if ready
            else set()
        )
        with service.sessions() as session:
            recent = session.execute(
                select(StudyTopicSuggestionModel.id, StudyTopicSuggestionModel.state)
                .where(
                    StudyTopicSuggestionModel.owner_id == owner,
                    StudyTopicSuggestionModel.key_json == context.candidate.key.model_dump_json(),
                )
                .order_by(StudyTopicSuggestionModel.created_at.desc())
                .limit(10)
            ).all()
    return templates.TemplateResponse(
        request=request,
        name="study_topics.html",
        context={
            "question": context,
            "accepted_ids": accepted,
            "taxonomies": TAXONOMIES,
            "configured": service.configured,
            "csrf_token": _csrf(request),
            "suggestion": None,
            "recent": [{"id": row[0], "state": row[1]} for row in recent],
        },
        headers=_HEADERS,
    )


@router.post("/blocks/questions/{key_id}/topics")
def review_topics(
    request: Request,
    key_id: NativeKey,
    content_hash: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
    category: Annotated[list[str] | None, Form()] = None,
) -> RedirectResponse:
    owner = _owner(request)
    require_form_csrf(request, csrf_token)
    with _errors():
        _topics(request).review(
            owner, key_id, tuple(category or ()), expected_content_hash=content_hash
        )
    return RedirectResponse(f"/study/blocks/questions/{key_id}", status_code=303, headers=_HEADERS)


@router.post("/blocks/questions/{key_id}/suggestions")
def prepare_suggestion(
    request: Request, key_id: NativeKey, csrf_token: Annotated[str, Form()]
) -> RedirectResponse:
    owner = _owner(request)
    require_form_csrf(request, csrf_token)
    with _errors():
        view = _topics(request).prepare(owner, key_id)
    return RedirectResponse(
        f"/study/blocks/suggestions/{view.id}", status_code=303, headers=_HEADERS
    )


@router.get("/blocks/suggestions/{identity}", response_class=HTMLResponse)
def suggestion_page(request: Request, identity: UuidId) -> HTMLResponse:
    owner = _owner(request)
    with _errors():
        view = _topics(request).load(owner, identity)
    return templates.TemplateResponse(
        request=request,
        name="study_topics.html",
        context={"suggestion": view, "csrf_token": _csrf(request)},
        headers=_HEADERS,
    )


@router.post("/blocks/suggestions/{identity}/run")
def run_suggestion(
    request: Request, identity: UuidId, csrf_token: Annotated[str, Form()]
) -> RedirectResponse:
    owner = _owner(request)
    require_form_csrf(request, csrf_token)
    with _errors():
        _topics(request).run(owner, identity)
    return RedirectResponse(
        f"/study/blocks/suggestions/{identity}", status_code=303, headers=_HEADERS
    )


@router.post("/blocks/suggestions/{identity}/accept")
def accept_suggestion(
    request: Request,
    identity: UuidId,
    csrf_token: Annotated[str, Form()],
    category: Annotated[list[str], Form()],
) -> RedirectResponse:
    owner = _owner(request)
    require_form_csrf(request, csrf_token)
    service = _topics(request)
    with _errors():
        service.accept(owner, identity, tuple(category))
        view = service.load(owner, identity)
    return RedirectResponse(
        f"/study/blocks/questions/{view.key.question_id}", status_code=303, headers=_HEADERS
    )


@router.post("/blocks/suggestions/{identity}/cancel")
def cancel_suggestion(
    request: Request, identity: UuidId, csrf_token: Annotated[str, Form()]
) -> RedirectResponse:
    owner = _owner(request)
    require_form_csrf(request, csrf_token)
    with _errors():
        _topics(request).cancel(owner, identity)
    return RedirectResponse(
        f"/study/blocks/suggestions/{identity}", status_code=303, headers=_HEADERS
    )
