"""Private, CSRF-protected activity controls; requests never dispatch providers."""

from pathlib import Path
from typing import cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import OperationalError

from oms_hub.processes import ProcessService
from oms_hub.web.csrf import require_form_csrf

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _owner(request: Request) -> str:
    owner = getattr(request.state, "study_owner_id", None)
    if not isinstance(owner, str) or not owner:
        raise HTTPException(403, "Private study owner required.")
    return owner


@router.get("/processes", response_class=HTMLResponse)
def processes_page(request: Request) -> HTMLResponse:
    _owner(request)
    return templates.TemplateResponse(
        request=request,
        name="processes.html",
        context={},
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/api/processes")
def processes_list(request: Request) -> JSONResponse:
    service = cast(ProcessService, request.app.state.process_service)
    return JSONResponse(
        service.list(_owner(request)), headers={"Cache-Control": "private, no-store"}
    )


@router.post("/api/processes/{family}/{job_id}/{action}")
def processes_action(request: Request, family: str, job_id: str, action: str) -> JSONResponse:
    owner = _owner(request)
    require_form_csrf(request, None)
    service = cast(ProcessService, request.app.state.process_service)
    try:
        item = service.action(family, job_id, action, owner_id=owner)
    except KeyError as error:
        raise HTTPException(404, "Process not found.") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    except OperationalError as error:
        raise HTTPException(409, "An operation is changing. Refresh and try again.") from error
    return JSONResponse({"item": item}, headers={"Cache-Control": "private, no-store"})
