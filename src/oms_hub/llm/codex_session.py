"""Subscription-session contracts; no process, login, or generation on import.

B1 freezes local metadata only. Live transport and capability evidence belong to B2
and the separately authorized Windows/provider proof.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

PINNED_VERSION = "codex-cli 0.153.4"
PINNED_SCHEMA_SHA256 = "e8284c5cb8157554a3dd1e035aadbd4325aea501af56887e9c2e12eb1b9b9448"
INSPECTED_MACOS_BINARY_SHA256 = "87a08119b8effa519f0ecb552dc98043f58a8200bf2ec5da60f76890c33e9c3a"


@dataclass(frozen=True)
class SessionRequest:
    request_id: str
    model: str
    instructions: str
    source_text: str
    image_paths: tuple[Path, ...] = ()
    output_schema: dict[str, object] | None = None


@dataclass(frozen=True)
class SessionResult:
    thread_id: str
    turn_id: str
    text: str


@dataclass(frozen=True)
class SessionLifecycle:
    request_id: str
    phase: Literal[
        "dispatching", "thread_created", "turn_started", "completed", "failed", "interrupted"
    ]
    thread_id: str | None = None
    turn_id: str | None = None


@dataclass(frozen=True)
class SessionStatus:
    state: Literal["disconnected", "connecting", "connected", "limited", "unavailable"]
    model_ids: tuple[str, ...]
    image_model_ids: tuple[str, ...]
    reset_at: str | None
    error_code: str | None


@dataclass(frozen=True)
class LoginChallenge:
    login_id: str
    url: str
    user_code: str | None


def model_ready(model: dict[str, object], *, images: bool) -> bool:
    """Check advertised inputs only, never auth, schema support, or live readiness."""
    name = model.get("model")
    modalities = model.get("inputModalities")
    return (
        isinstance(name, str)
        and bool(name.strip())
        and isinstance(modalities, list)
        and all(isinstance(value, str) for value in modalities)
        and "text" in modalities
        and (not images or "image" in modalities)
    )
