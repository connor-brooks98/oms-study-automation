"""Durable, identity-bound text generation without ambiguous request replay."""

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from oms_hub.files.atomic import verified_atomic_write
from oms_hub.llm.codex_session import (
    CodexSessionClient,
    SessionError,
    SessionLifecycle,
    SessionRequest,
)


def generate_bound_text(
    client: CodexSessionClient,
    request: SessionRequest,
    journal: Path,
    identity: dict[str, object],
    *,
    on_lifecycle: Callable[[SessionLifecycle], None] | None = None,
) -> str:
    journal.parent.mkdir(parents=True, exist_ok=True)
    state: dict[str, object] = {"identity": identity, "events": []}
    try:
        with journal.open("x", encoding="utf-8") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        try:
            previous = json.loads(journal.read_text(encoding="utf-8"))
            cached_text = previous.get("text")
            if (
                previous.get("identity") != identity or not isinstance(cached_text, str)
                or previous.get("result_sha256")
                != hashlib.sha256(cached_text.encode()).hexdigest()
                or not previous.get("events")
                or previous["events"][-1].get("phase") != "completed"
            ):
                raise SessionError("interrupted")
            return cached_text
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as error:
            raise SessionError("interrupted") from error

    events: list[dict[str, object]] = []
    observed = False

    def save() -> None:
        temporary = journal.with_name(f".{journal.name}.{uuid4()}.tmp")
        verified_atomic_write(json.dumps(state, sort_keys=True).encode(), temporary)
        temporary.replace(journal)

    def persist(event: SessionLifecycle) -> None:
        nonlocal observed
        # Even a rejected notification means this was not a zero-event preflight.
        observed = True
        if event.request_id != request.request_id:
            raise ValueError("text request identity mismatch")
        events.append(asdict(event) | {"at": datetime.now(UTC).isoformat()})
        state["events"] = events
        save()
        if on_lifecycle is not None:
            on_lifecycle(event)

    try:
        response = client.generate(request, cancelled=lambda: False, on_lifecycle=persist)
    except SessionError:
        if not observed:
            # Preserve preflight evidence; only a later explicit call may retry.
            journal.replace(journal.with_name(f"{journal.stem}.blocked-{uuid4()}.json"))
        raise
    if not events or events[-1].get("phase") != "completed":
        raise SessionError("interrupted")
    if (events[-1].get("thread_id"), events[-1].get("turn_id")) != (
        response.thread_id, response.turn_id
    ):
        raise SessionError("protocol_error")
    state["raw_response"] = response.text
    save()
    try:
        decoded = json.loads(response.text)
        if not isinstance(decoded, dict) or set(decoded) != {"text"}:
            raise ValueError("invalid text result")
        text = decoded["text"]
        if not isinstance(text, str):
            raise ValueError("invalid text result")
    except (ValueError, TypeError) as error:
        raise SessionError("invalid_output") from error
    state["text"] = text
    state["result_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    save()
    return text
