"""Private Hub surface for signing in and sending sources through ExtendLM."""

import time
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from oms_hub.artifacts import ArtifactError, ArtifactRole, ArtifactService
from oms_hub.extendlm import ISSUER, MCP_URL, ExtendLMError
from oms_hub.extendlm_service import ExtendLMService
from oms_hub.ingestion.domain import UploadKind
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.repositories import CatalogRepository
from oms_hub.web.csrf import require_form_csrf

router = APIRouter(prefix="/settings/extendlm")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
COOKIE = "oms_extendlm_browser"


def service(request: Request) -> ExtendLMService:
    return cast(ExtendLMService, request.app.state.extendlm)


def owner(request: Request) -> str:
    value = getattr(request.state, "study_owner_id", None)
    if not isinstance(value, str) or not value:
        raise HTTPException(403, "Private Hub sign-in required.")
    return value


def key(request: Request) -> str:
    return service(request).session_key(request.cookies.get(COOKIE, ""), owner(request))


def response(data: object) -> JSONResponse:
    return JSONResponse(data, headers={"Cache-Control": "private, no-store"})


@router.get("", response_class=HTMLResponse)
def page(request: Request, lecture_id: int | None = None) -> HTMLResponse:
    owner(request)
    lecture = None
    if lecture_id is not None:
        lecture = CatalogRepository(request.app.state.database).get_lecture(lecture_id)
        if lecture is None:
            raise HTTPException(404, "Lecture not found.")
    return templates.TemplateResponse(
        request=request,
        name="extendlm.html",
        context={"lecture": lecture, "mcp_url": MCP_URL},
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/connect")
def connect(
    request: Request, csrf_token: str = Form(), lecture_id: int | None = Form(default=None)
) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    settings = request.app.state.settings
    host = request.url.hostname
    public = bool(settings.public_hostname and host == settings.public_hostname)
    base = f"https://{settings.public_hostname}" if public else str(request.base_url).rstrip("/")
    cookie, url = service(request).begin(owner(request), f"{base}/settings/extendlm/callback")
    if lecture_id is not None:
        with service(request).state(
            service(request).session_key(cookie, owner(request)), write=True
        ) as data:
            data["lecture_id"] = lecture_id
    result = JSONResponse({"authorization_url": url}, headers={"Cache-Control": "no-store"})
    result.set_cookie(
        COOKIE,
        cookie,
        max_age=30 * 86400,
        httponly=True,
        secure=public,
        samesite="lax",
        path="/settings/extendlm",
    )
    return result


@router.get("/callback")
def callback(
    request: Request, code: str = "", state: str = "", iss: str = "", error: str = ""
) -> RedirectResponse:
    request.scope["query_string"] = b""  # Keep OAuth codes out of Uvicorn access logs.
    if error:
        raise ExtendLMError(
            "ExtendLM sign-in was not approved. Return to the upload page and sign in."
        )
    # The issuer advertises authorization_response_iss_parameter_supported.
    if iss != ISSUER:
        raise ExtendLMError("The sign-in response came from an unexpected issuer. Start again.")
    service(request).finish(key(request), code, state, iss)
    with service(request).state(key(request)) as data:
        lecture_id = data.get("lecture_id")
    destination = "/settings/extendlm"
    if isinstance(lecture_id, int):
        destination += f"?lecture_id={lecture_id}"
    return RedirectResponse(
        destination,
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.get("/status")
def status(request: Request) -> JSONResponse:
    owner(request)
    try:
        session_key = key(request)
    except ExtendLMError:
        return response({"signed_in": False})
    with service(request).state(session_key) as state:
        connected = bool(state.get("tokens") and state.get("expires", 0) > time.time())
        selected = bool(state.get("selection"))
    return response(
        {"signed_in": connected, "selected": selected, "jobs": service(request).jobs(session_key)}
    )


@router.post("/disconnect")
def disconnect(request: Request, csrf_token: str = Form()) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    service(request).disconnect(key(request))
    result = response({"signed_in": False})
    result.delete_cookie(COOKIE, path="/settings/extendlm")
    return result


@router.post("/accounts")
def accounts(request: Request, csrf_token: str = Form()) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    return response({"accounts": service(request).accounts(key(request))})


@router.post("/select")
def select(
    request: Request, choice: str = Form(max_length=64), csrf_token: str = Form()
) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    service(request).select(key(request), choice)
    return response({"selected": True})


@router.post("/notebooks")
def notebooks(
    request: Request, csrf_token: str = Form(), cursor: str = Form(default="", max_length=2048)
) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    return response(service(request).notebooks(key(request), cursor))


@router.post("/uploads", status_code=202)
def upload(
    request: Request,
    files: Annotated[list[UploadFile], File()],
    notebook_id: str = Form(max_length=512),
    csrf_token: str = Form(),
) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    job_id = service(request).enqueue(
        key(request), notebook_id, [(f.filename or "", f.file) for f in files]
    )
    result = response({"job_id": job_id})
    result.status_code = 202
    return result


@router.post("/lectures/{lecture_id}/upload", status_code=202)
def upload_lecture(
    request: Request,
    lecture_id: int,
    notebook_id: str = Form(max_length=512),
    csrf_token: str = Form(),
) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    session_key = key(request)
    database = request.app.state.database
    if CatalogRepository(database).get_lecture(lecture_id) is None:
        raise HTTPException(404, "Lecture not found.")
    revisions = IngestionRepository(database).list_current_revisions(lecture_id)
    artifacts = ArtifactService(database, request.app.state.settings)
    try:
        with ExitStack() as stack:
            files = []
            for revision in revisions:
                if revision.state != "current":
                    continue
                role = (
                    ArtifactRole.PDF if revision.kind == UploadKind.SLIDES else ArtifactRole.CLEANED
                )
                resolved = artifacts.resolve(revision.id, role)
                name = resolved.path.name
                if role == ArtifactRole.CLEANED and Path(name).suffix.lower() not in {
                    ".txt",
                    ".md",
                }:
                    name = f"{resolved.path.stem}.txt"
                files.append((name, stack.enter_context(resolved.path.open("rb"))))
            job_id = service(request).enqueue(
                session_key, notebook_id, files, lecture_id=lecture_id
            )
    except ArtifactError as error:
        raise HTTPException(
            409, "Lecture materials are not ready. Check the lecture's files."
        ) from error
    result = response({"job_id": job_id})
    result.status_code = 202
    return result


@router.post("/jobs/{job_id}/resume")
def resume(request: Request, job_id: str, csrf_token: str = Form()) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    service(request).resume(key(request), job_id)
    return response({"job_id": job_id})


@router.post("/jobs/{job_id}/check")
def check(request: Request, job_id: str, csrf_token: str = Form()) -> JSONResponse:
    require_form_csrf(request, csrf_token)
    service(request).check_sources(key(request), job_id)
    return response({"job_id": job_id})
