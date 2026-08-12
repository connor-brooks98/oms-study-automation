from __future__ import annotations

from pathlib import Path
from uuid import UUID

from oms_hub.anki.repository import AnkiCurationRepository

_MAX_SUBCALL_ORDINAL = (1 << 63) - 1


def _parse_subcall_ordinal(value: object) -> int:
    """Retained validation helper for replay stores authorized in a later scope."""
    if isinstance(value, bool):
        raise ValueError("provider attempt subcall ordinal is invalid")
    if isinstance(value, int):
        ordinal = value
    elif isinstance(value, str) and value.isdecimal():
        ordinal = int(value)
    else:
        raise ValueError("provider attempt subcall ordinal is invalid")
    if not 0 <= ordinal <= _MAX_SUBCALL_ORDINAL:
        raise ValueError("provider attempt subcall ordinal is invalid")
    return ordinal


def write_structured_replay_pack(
    repository: AnkiCurationRepository,
    job_id: UUID,
    destination: Path,
) -> Path:
    """Refuse ordinary event-ledger response material as a replay source.

    The event ledger is an audit trail with redaction, not the separately
    authorized encrypted response store required for replay.  That store is
    intentionally absent from A0, so a response-received event is manual
    recovery rather than a license to regenerate or replay a provider call.
    """
    if destination.exists():
        raise ValueError("structured replay pack destination already exists")
    repository.require_job(job_id)
    raise ValueError(
        "ordinary provider-attempt evidence is redacted; "
        "no sensitive-authorized replay store exists"
    )
