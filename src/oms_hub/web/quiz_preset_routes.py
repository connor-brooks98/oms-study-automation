"""Private preset CRUD, with no generation or provider side effects."""

from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from oms_hub.study_generation.quiz_presets import (
    PresetConflict,
    QuizPresetRepository,
    validate_owner,
)
from oms_hub.web.csrf import require_form_csrf

_HEADERS = {"Cache-Control": "private, no-store"}


class PrivatePresetRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def private(request: Request) -> Response:
            try:
                response = await handler(request)
            except HTTPException as error:
                response = JSONResponse({"detail": error.detail}, status_code=error.status_code)
            except RequestValidationError:
                response = JSONResponse(
                    {"detail": "Check the preset name, instructions and ID."}, status_code=422
                )
            response.headers.update(_HEADERS)
            return response

        return private


router = APIRouter(prefix="/study/quiz-presets", route_class=PrivatePresetRoute)


class PresetInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=80)
    instructions: str = Field(min_length=1, max_length=4000)


def _access(request: Request, *, mutation: bool = False) -> tuple[str, QuizPresetRepository]:
    try:
        owner = validate_owner(getattr(request.state, "study_owner_id", None))
    except ValueError:
        raise HTTPException(401, "Private Study Hub access is required.") from None
    if mutation:
        require_form_csrf(request, None)
    repository = getattr(request.app.state, "quiz_presets", None)
    if repository is None:
        raise HTTPException(503, "Quiz presets are unavailable.")
    return owner, cast(QuizPresetRepository, repository)


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except PresetConflict as error:
        raise HTTPException(409, str(error)) from None
    except KeyError:
        raise HTTPException(404, "Preset was not found.") from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    except SQLAlchemyError:
        raise HTTPException(503, "Quiz presets could not be saved. Please try again.") from None


@router.get("")
def list_presets(request: Request) -> dict[str, object]:
    owner, repository = _access(request)
    with _errors():
        return {"presets": [asdict(preset) for preset in repository.list(owner)]}


@router.post("")
def save_preset(request: Request, values: PresetInput) -> dict[str, str]:
    owner, repository = _access(request, mutation=True)
    with _errors():
        return asdict(repository.save(owner, values.name, values.instructions))


@router.put("/{preset_id}")
def update_preset(request: Request, preset_id: UUID, values: PresetInput) -> dict[str, str]:
    owner, repository = _access(request, mutation=True)
    with _errors():
        return asdict(
            repository.save(owner, values.name, values.instructions, preset_id=str(preset_id))
        )


@router.delete("/{preset_id}")
def delete_preset(request: Request, preset_id: UUID) -> dict[str, bool]:
    owner, repository = _access(request, mutation=True)
    with _errors():
        repository.delete(owner, str(preset_id))
    return {"deleted": True}
