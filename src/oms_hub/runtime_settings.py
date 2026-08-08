"""The intentionally tiny, audited remote-runtime setting allowlist.

This module is deliberately not a generic environment-variable editor.  A
remote dashboard must not be able to alter recovery, authentication, storage,
or deployment settings.  The only mutable value is the local AnkiConnect port,
which is staged for the next service start.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oms_hub.config import Settings
from oms_hub.models import RuntimeSettingAuditModel, RuntimeSettingModel

if TYPE_CHECKING:
    from oms_hub.db import Database


ANKI_CONNECT_PORT_KEY = "anki_connect_port"


@dataclass(frozen=True)
class RuntimeSettingStatus:
    anki_connect_port: int
    source: str
    revision: int | None
    restart_required: bool


def validate_anki_connect_port(settings: Settings, port: int) -> str:
    """Validate an allowlisted port by constructing a normal Settings object."""
    if isinstance(port, bool) or not 1024 <= port <= 65535:
        raise ValueError("AnkiConnect port must be between 1024 and 65535")
    endpoint = f"http://127.0.0.1:{port}"
    values = settings.model_dump(mode="python")
    values["anki_connect_url"] = endpoint
    candidate = Settings.model_validate(values)
    if port == candidate.dashboard_port:
        raise ValueError("AnkiConnect and Study Hub cannot use the same port")
    return candidate.anki_connect_url


class RuntimeSettingsRepository:
    """Persist and audit only the remote-safe AnkiConnect port override."""

    def __init__(self, database: "Database", base_settings: Settings):
        self.database = database
        self.base_settings = base_settings
        # This process starts on the base port until ``effective_settings`` is
        # called during app construction.  It must not reinterpret a newly
        # saved row as already active without a restart.
        self._active_port = _port(base_settings.anki_connect_url)

    def effective_settings(self) -> Settings:
        port = self._stored_port()
        if port is None:
            self._active_port = _port(self.base_settings.anki_connect_url)
            return self.base_settings
        endpoint = validate_anki_connect_port(self.base_settings, port)
        self._active_port = _port(endpoint)
        return self.base_settings.model_copy(update={"anki_connect_url": endpoint})

    def status(self) -> RuntimeSettingStatus:
        with self.database.session() as session:
            row = session.get(RuntimeSettingModel, ANKI_CONNECT_PORT_KEY)
            if row is None:
                desired_port = _port(self.base_settings.anki_connect_url)
                return RuntimeSettingStatus(
                    anki_connect_port=desired_port,
                    source="environment",
                    revision=self._last_revision(session),
                    restart_required=desired_port != self._active_port,
                )
            desired_port = int(row.value)
            active = desired_port == self._active_port
            return RuntimeSettingStatus(
                anki_connect_port=desired_port,
                source="active_override" if active else "staged_override",
                revision=row.revision,
                restart_required=not active,
            )

    def stage_anki_connect_port(self, port: int, *, actor: str) -> RuntimeSettingStatus:
        endpoint = validate_anki_connect_port(self.base_settings, port)
        normalized_port = _port(endpoint)
        with self.database.session() as session:
            current = session.get(RuntimeSettingModel, ANKI_CONNECT_PORT_KEY)
            revision = self._next_revision(session)
            previous_value = current.value if current is not None else None
            if current is None:
                session.add(
                    RuntimeSettingModel(
                        key=ANKI_CONNECT_PORT_KEY,
                        value=str(normalized_port),
                        revision=revision,
                    )
                )
            else:
                current.value = str(normalized_port)
                current.revision = revision
            session.add(
                RuntimeSettingAuditModel(
                    key=ANKI_CONNECT_PORT_KEY,
                    action="staged",
                    previous_value=previous_value,
                    value=str(normalized_port),
                    revision=revision,
                    actor=_actor(actor),
                )
            )
        return self.status()

    def clear_anki_connect_port(self, *, actor: str) -> RuntimeSettingStatus:
        with self.database.session() as session:
            current = session.get(RuntimeSettingModel, ANKI_CONNECT_PORT_KEY)
            if current is None:
                return self.status()
            revision = self._next_revision(session)
            previous_value = current.value
            session.delete(current)
            session.add(
                RuntimeSettingAuditModel(
                    key=ANKI_CONNECT_PORT_KEY,
                    action="cleared",
                    previous_value=previous_value,
                    value=None,
                    revision=revision,
                    actor=_actor(actor),
                )
            )
        return self.status()

    def _stored_port(self) -> int | None:
        with self.database.session() as session:
            row = session.get(RuntimeSettingModel, ANKI_CONNECT_PORT_KEY)
            return None if row is None else int(row.value)

    @staticmethod
    def _next_revision(session: Session) -> int:
        # The audit history persists across clear/re-stage cycles, so revision
        # numbers remain monotonic instead of being reset by a rollback.
        value = RuntimeSettingsRepository._last_revision(session)
        return int(value or 0) + 1

    @staticmethod
    def _last_revision(session: Session) -> int | None:
        value = session.scalar(
            select(func.max(RuntimeSettingAuditModel.revision)).where(
                RuntimeSettingAuditModel.key == ANKI_CONNECT_PORT_KEY
            )
        )
        return None if value is None else int(value)


def _port(endpoint: str) -> int:
    return int(endpoint.rsplit(":", 1)[1])


def _actor(value: str) -> str:
    normalized = value.strip()
    return normalized[:320] if normalized else "local"
