"""Owner-only actions for recoverable lecture material removal."""

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import OperationalError

from oms_hub.artifact_writes import ArtifactWriteClaimLost, ArtifactWriteContended
from oms_hub.lecture_materials import LectureMaterials, MaterialConflict
from oms_hub.web.csrf import require_form_csrf

router = APIRouter()


def _change(
    request: Request, lecture_id: int, revision_id: int, csrf_token: str | None, *, restore: bool
) -> RedirectResponse:
    owner = getattr(request.state, "study_owner_id", None)
    if not isinstance(owner, str) or not owner:
        raise HTTPException(403, "Private study owner required.")
    require_form_csrf(request, csrf_token)
    service = LectureMaterials(request.app.state.database, request.app.state.settings)
    try:
        (service.restore if restore else service.remove)(lecture_id, revision_id)
    except KeyError as error:
        raise HTTPException(404, "Lecture material not found.") from error
    except MaterialConflict as error:
        raise HTTPException(409, str(error)) from error
    except (ArtifactWriteContended, ArtifactWriteClaimLost, OperationalError) as error:
        raise HTTPException(
            409, "A lecture operation is active. Wait for it to stop and try again."
        ) from error
    return RedirectResponse(
        f"/lectures/{lecture_id}", status_code=303, headers={"Cache-Control": "private, no-store"}
    )


@router.post("/lectures/{lecture_id}/materials/{revision_id}/remove")
def remove_material(
    request: Request, lecture_id: int, revision_id: int, csrf_token: str | None = Form(default=None)
) -> RedirectResponse:
    return _change(request, lecture_id, revision_id, csrf_token, restore=False)


@router.post("/lectures/{lecture_id}/materials/{revision_id}/restore")
def restore_material(
    request: Request, lecture_id: int, revision_id: int, csrf_token: str | None = Form(default=None)
) -> RedirectResponse:
    return _change(request, lecture_id, revision_id, csrf_token, restore=True)
