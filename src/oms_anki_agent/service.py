import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Protocol, cast
from uuid import UUID

from oms_anki_agent.ankiconnect import AnkiConnectUnavailable
from oms_anki_agent.apply import AgentEnvelopeApplier, LocalAnki
from oms_anki_agent.hub_client import HubClientError
from oms_anki_agent.ledger import AgentLedger
from oms_anki_agent.snapshot import (
    FullSnapshotExporter,
    PreparedSnapshot,
    SnapshotUpload,
    prepare_snapshot_upload,
)
from oms_hub.anki.contracts import (
    AgentCommand,
    AgentHeartbeat,
    EnvelopeReceipt,
)
from oms_hub.anki.domain import AgentCommandType


class WriteCommandDisabled(RuntimeError):
    """Write envelopes remain disabled until the idempotent apply milestone."""


class ServiceHub(Protocol):
    def post_heartbeat(self, heartbeat: AgentHeartbeat) -> dict[str, str]: ...

    def next_command(self) -> AgentCommand | None: ...

    def upload_snapshot(
        self,
        command_id: UUID,
        snapshot: SnapshotUpload,
    ) -> dict[str, str]: ...

    def upload_receipt(self, command_id: UUID, receipt: EnvelopeReceipt) -> dict[str, str]: ...


class ServiceAnki(Protocol):
    def version(self) -> int: ...


class SnapshotFactory(Protocol):
    def create(self, command: AgentCommand) -> SnapshotUpload: ...

    def commit(self, snapshot: SnapshotUpload) -> None: ...


class AnkiLauncher(Protocol):
    def open_anki(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ServiceSettings:
    agent_id: str
    agent_version: str
    poll_seconds: float = 5.0
    startup_timeout_seconds: float = 30.0
    startup_poll_seconds: float = 1.0
    maximum_backoff_seconds: float = 60.0


class SubprocessAnkiLauncher:
    def open_anki(self) -> None:
        subprocess.run(
            ["/usr/bin/open", "-a", "Anki"],
            check=False,
            shell=False,
        )


class AgentService:
    def __init__(
        self,
        *,
        hub: ServiceHub,
        anki: ServiceAnki,
        snapshots: SnapshotFactory,
        launcher: AnkiLauncher,
        settings: ServiceSettings,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.hub = hub
        self.anki = anki
        self.snapshots = snapshots
        self.launcher = launcher
        self.settings = settings
        self.now = now
        self.monotonic = monotonic
        self.sleep = sleep
        self.active_snapshot_id: str | None = None
        self.envelope_applier = (
            AgentEnvelopeApplier(
                anki=cast(LocalAnki, anki),
                ledger=cast(AgentLedger, getattr(snapshots, "ledger", None)),
                agent_id=settings.agent_id,
            )
            if getattr(snapshots, "ledger", None) is not None
            else None
        )

    def run_once(self) -> str:
        ankiconnect_version = self._ensure_anki()
        self.hub.post_heartbeat(
            AgentHeartbeat(
                agent_id=self.settings.agent_id,
                agent_version=self.settings.agent_version,
                anki_version="unknown",
                ankiconnect_version=ankiconnect_version,
                active_snapshot_id=self.active_snapshot_id,
                health="ok",
                observed_at=self.now(),
                supported_envelope_contract_versions=(1, 2),
            )
        )
        command = self.hub.next_command()
        if command is None:
            return "idle"
        if command.command_type is AgentCommandType.APPLY_ENVELOPE:
            if self.envelope_applier is None:
                raise WriteCommandDisabled("apply requires the local durable operation ledger")
            raw = command.payload.get("envelope", command.payload)
            if not isinstance(raw, dict):
                raise ValueError("apply command lacks an action envelope")
            receipt = self.envelope_applier.apply(raw)
            self.hub.upload_receipt(command.command_id, receipt)
            return "envelope_applied"
        if command.command_type not in {
            AgentCommandType.FULL_SNAPSHOT,
            AgentCommandType.DELTA_SNAPSHOT,
        }:
            raise WriteCommandDisabled(
                f"{command.command_type.value} has no enabled read-only handler"
            )
        snapshot = self.snapshots.create(command)
        self.hub.upload_snapshot(command.command_id, snapshot)
        self.snapshots.commit(snapshot)
        self.active_snapshot_id = snapshot.manifest.snapshot_id
        return "snapshot_uploaded"

    def run(self, stop: Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                self.run_once()
            except HubClientError as exc:
                if not exc.transient:
                    raise
                self.sleep(min(backoff, self.settings.maximum_backoff_seconds))
                backoff = min(
                    backoff * 2,
                    self.settings.maximum_backoff_seconds,
                )
                continue
            backoff = 1.0
            if not stop.is_set():
                self.sleep(self.settings.poll_seconds)

    def _ensure_anki(self) -> int:
        try:
            return self.anki.version()
        except AnkiConnectUnavailable:
            self.launcher.open_anki()
        deadline = self.monotonic() + self.settings.startup_timeout_seconds
        while self.monotonic() < deadline:
            remaining = deadline - self.monotonic()
            self.sleep(min(self.settings.startup_poll_seconds, remaining))
            try:
                return self.anki.version()
            except AnkiConnectUnavailable:
                continue
        raise AnkiConnectUnavailable(
            "AnkiConnect did not become available before the startup deadline"
        )


class LedgerSnapshotFactory:
    """Adapt streamed full exports to the current version-1 upload contract."""

    def __init__(
        self,
        *,
        exporter: FullSnapshotExporter,
        ledger: AgentLedger,
        work_root: Path,
    ) -> None:
        if exporter.ledger is not None:
            raise ValueError(
                "service snapshot exporter must defer ledger updates until Hub acceptance"
            )
        self.exporter = exporter
        self.ledger = ledger
        self.work_root = work_root
        self._pending_payload_sha256: str | None = None

    def create(self, command: AgentCommand) -> PreparedSnapshot:
        prior = self.ledger.note_hashes()
        self.work_root.mkdir(parents=True, exist_ok=True)
        output = self.work_root / f"{command.command_id}.jsonl.gz"
        manifest = self.exporter.export(output)
        snapshot = prepare_snapshot_upload(
            manifest=manifest,
            exported_notes=output,
            destination=self.work_root / f"{command.command_id}.upload.json",
            command_type=command.command_type,
            prior_hashes=prior,
        )
        self._pending_payload_sha256 = snapshot.payload_sha256
        return snapshot

    def commit(self, snapshot: SnapshotUpload) -> None:
        if (
            not isinstance(snapshot, PreparedSnapshot)
            or self._pending_payload_sha256 != snapshot.payload_sha256
        ):
            raise ValueError("snapshot does not match the pending ledger update")
        self.ledger.replace_note_hashes(snapshot.note_hashes)
        self._pending_payload_sha256 = None
