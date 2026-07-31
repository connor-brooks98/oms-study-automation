from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.ingestion.service import IngestionService
from oms_hub.repositories import CatalogRepository
from oms_hub.web.csrf import require_form_csrf

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(prefix="/quarantine")


def _repository(request: Request) -> IngestionRepository:
    return cast(
        IngestionRepository,
        request.app.state.ingestion_repository,
    )


def _service(request: Request) -> IngestionService:
    return cast(IngestionService, request.app.state.ingestion_service)


@router.get("", response_class=HTMLResponse)
def quarantine_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="quarantine.html",
        context={
            "items": _repository(request).list_quarantined(),
            "lectures": CatalogRepository(
                request.app.state.database
            ).list_lectures(),
        },
    )


@router.post("/{item_id}/assign")
def assign_item(
    item_id: str,
    request: Request,
    lecture_id: Annotated[int, Form()],
    csrf_token: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    require_form_csrf(request, csrf_token)
    try:
        _service(request).assign(item_id, lecture_id)
    except KeyError as error:
        raise HTTPException(404, "upload or lecture not found") from error
    return RedirectResponse("/quarantine", status_code=303)
