from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ValidationError

from oms_hub.anki.contracts import (
    AgentCommand,
    AgentHeartbeat,
    EnvelopeReceipt,
    MediaUpload,
    SnapshotDelta,
)
from oms_hub.anki.domain import AgentCommandType
from oms_hub.anki.repository import AnkiCurationRepository

router = APIRouter(prefix="/agent/v1")


def _repository(request: Request) -> AnkiCurationRepository:
    return cast(AnkiCurationRepository, request.app.state.anki_repository)


def _agent_id(request: Request) -> str:
    return cast(str, request.state.agent_id)


async def _validated_body[Contract: BaseModel](
    request: Request,
    model: type[Contract],
) -> Contract:
    try:
        payload = await request.json()
        return model.model_validate(payload)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail="invalid agent payload") from exc


def _require_owned(
    request: Request,
    command_id: UUID,
    allowed_types: set[AgentCommandType],
) -> None:
    try:
        _repository(request).require_owned_agent_command(
            command_id,
            _agent_id(request),
            allowed_types,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="agent command was not found") from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail="agent command is not assigned to this agent",
        ) from exc


@router.post("/heartbeat")
def heartbeat(request: Request, payload: AgentHeartbeat) -> dict[str, str]:
    if payload.agent_id != _agent_id(request):
        raise HTTPException(status_code=409, detail="agent identity does not match")
    _repository(request).record_agent_heartbeat(
        agent_id=payload.agent_id,
        heartbeat_at=payload.observed_at.isoformat(),
        versions={
            "agent": payload.agent_version,
            "anki": payload.anki_version,
            "ankiconnect": payload.ankiconnect_version,
        },
        active_snapshot_id=payload.active_snapshot_id,
        health={"state": payload.health},
    )
    return {"status": "ok"}


@router.get("/commands/next", response_model=AgentCommand)
def next_command(request: Request) -> AgentCommand | Response:
    stored = _repository(request).claim_next_agent_command(
        _agent_id(request),
        datetime.now(UTC),
    )
    if stored is None:
        return Response(status_code=204)
    return AgentCommand(
        command_id=stored.id,
        command_type=stored.command_type,
        payload=stored.payload,
        payload_sha256=stored.payload_sha256,
        created_at=datetime.fromisoformat(stored.created_at),
    )


@router.post("/commands/{command_id}/snapshot")
async def upload_snapshot(
    command_id: UUID,
    request: Request,
) -> dict[str, str]:
    _require_owned(
        request,
        command_id,
        {
            AgentCommandType.FULL_SNAPSHOT,
            AgentCommandType.DELTA_SNAPSHOT,
        },
    )
    payload = await _validated_body(request, SnapshotDelta)
    _repository(request).complete_agent_command(
        command_id,
        _agent_id(request),
        {
            "snapshot_id": payload.manifest.snapshot_id,
            "payload_sha256": payload.payload_sha256,
        },
    )
    return {"status": "accepted"}


@router.post("/commands/{command_id}/media")
async def upload_media(
    command_id: UUID,
    request: Request,
) -> dict[str, str]:
    _require_owned(
        request,
        command_id,
        {AgentCommandType.FETCH_MEDIA},
    )
    payload = await _validated_body(request, MediaUpload)
    if payload.command_id != command_id:
        raise HTTPException(status_code=409, detail="command identity does not match")
    _repository(request).complete_agent_command(
        command_id,
        _agent_id(request),
        {
            "filename": payload.filename,
            "sha256": payload.sha256,
            "byte_count": payload.byte_count,
        },
    )
    return {"status": "accepted"}


@router.post("/commands/{command_id}/receipt")
async def upload_receipt(
    command_id: UUID,
    request: Request,
) -> dict[str, str]:
    _require_owned(
        request,
        command_id,
        {AgentCommandType.APPLY_ENVELOPE},
    )
    payload = await _validated_body(request, EnvelopeReceipt)
    _repository(request).record_receipt(
        payload.envelope_id,
        payload.model_dump(mode="json"),
    )
    _repository(request).complete_agent_command(
        command_id,
        _agent_id(request),
        {
            "envelope_id": str(payload.envelope_id),
            "payload_sha256": payload.payload_sha256,
        },
    )
    return {"status": "accepted"}
