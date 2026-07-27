from collections.abc import Sequence
from datetime import UTC, datetime
from threading import Event
from uuid import UUID

import pytest

from oms_anki_agent.ankiconnect import AnkiConnectUnavailable
from oms_anki_agent.hub_client import HubAuthenticationError, HubUnavailable
from oms_anki_agent.service import (
    AgentService,
    ServiceSettings,
    WriteCommandDisabled,
)
from oms_hub.anki.contracts import (
    AgentCommand,
    AgentHeartbeat,
    SnapshotDelta,
    SnapshotManifest,
)
from oms_hub.anki.domain import AgentCommandType

COMMAND_ID = UUID("b2edb9da-4421-4d27-bc6b-7797ed310355")


def _command(command_type: AgentCommandType) -> AgentCommand:
    return AgentCommand(
        command_id=COMMAND_ID,
        command_type=command_type,
        payload={},
        payload_sha256="a" * 64,
        created_at=datetime(2026, 7, 27, tzinfo=UTC),
    )


def _snapshot() -> SnapshotDelta:
    return SnapshotDelta(
        manifest=SnapshotManifest(
            snapshot_id="full-empty",
            source_deck="Anking Step Deck",
            note_count=0,
            id_set_sha256="a" * 64,
            content_sha256="b" * 64,
            export_version="1",
            agent_version="test",
            ankiconnect_version=6,
            exported_at=datetime(2026, 7, 27, tzinfo=UTC),
            payload_sha256="c" * 64,
        ),
        upserts=(),
        deleted_note_ids=(),
        payload_sha256="d" * 64,
    )


class FakeHub:
    def __init__(self, commands: Sequence[AgentCommand] = ()) -> None:
        self.commands = list(commands)
        self.heartbeats: list[AgentHeartbeat] = []
        self.uploads: list[tuple[UUID, SnapshotDelta]] = []

    def post_heartbeat(self, heartbeat: AgentHeartbeat) -> dict[str, str]:
        self.heartbeats.append(heartbeat)
        return {"status": "ok"}

    def next_command(self) -> AgentCommand | None:
        return self.commands.pop(0) if self.commands else None

    def upload_snapshot(
        self,
        command_id: UUID,
        snapshot: SnapshotDelta,
    ) -> dict[str, str]:
        self.uploads.append((command_id, snapshot))
        return {"status": "accepted"}


class FakeAnki:
    def __init__(self, versions: Sequence[int | Exception]) -> None:
        self.versions = list(versions)
        self.calls = 0

    def version(self) -> int:
        self.calls += 1
        value = self.versions.pop(0) if self.versions else 6
        if isinstance(value, Exception):
            raise value
        return value


class FakeSnapshots:
    def __init__(self) -> None:
        self.commands: list[AgentCommand] = []
        self.commits: list[SnapshotDelta] = []

    def create(self, command: AgentCommand) -> SnapshotDelta:
        self.commands.append(command)
        return _snapshot()

    def commit(self, snapshot: SnapshotDelta) -> None:
        self.commits.append(snapshot)


class FakeLauncher:
    def __init__(self) -> None:
        self.calls = 0

    def open_anki(self) -> None:
        self.calls += 1


class FakeClock:
    def __init__(self) -> None:
        self.seconds = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.seconds += seconds


def _service(
    *,
    hub: FakeHub,
    anki: FakeAnki,
    snapshots: FakeSnapshots | None = None,
    launcher: FakeLauncher | None = None,
    clock: FakeClock | None = None,
) -> AgentService:
    current_clock = clock or FakeClock()
    return AgentService(
        hub=hub,
        anki=anki,
        snapshots=snapshots or FakeSnapshots(),
        launcher=launcher or FakeLauncher(),
        settings=ServiceSettings(
            agent_id="connor-mac",
            agent_version="test",
            poll_seconds=1,
            startup_timeout_seconds=3,
            startup_poll_seconds=1,
            maximum_backoff_seconds=4,
        ),
        now=lambda: datetime(2026, 7, 27, tzinfo=UTC),
        monotonic=current_clock.monotonic,
        sleep=current_clock.sleep,
    )


def test_run_once_heartbeats_polls_one_command_and_uploads_snapshot() -> None:
    hub = FakeHub([_command(AgentCommandType.FULL_SNAPSHOT)])
    snapshots = FakeSnapshots()
    launcher = FakeLauncher()
    service = _service(
        hub=hub,
        anki=FakeAnki([6]),
        snapshots=snapshots,
        launcher=launcher,
    )

    assert service.run_once() == "snapshot_uploaded"
    assert len(hub.heartbeats) == 1
    assert hub.heartbeats[0].model_fields_set == {
        "agent_id",
        "agent_version",
        "anki_version",
        "ankiconnect_version",
        "active_snapshot_id",
        "health",
        "observed_at",
    }
    assert snapshots.commands[0].command_id == COMMAND_ID
    assert hub.uploads == [(COMMAND_ID, _snapshot())]
    assert snapshots.commits == [_snapshot()]
    assert launcher.calls == 0


def test_service_opens_anki_only_when_unavailable_and_uses_bounded_deadline() -> None:
    hub = FakeHub()
    launcher = FakeLauncher()
    clock = FakeClock()
    unavailable = AnkiConnectUnavailable("offline")
    service = _service(
        hub=hub,
        anki=FakeAnki([unavailable, unavailable, 6]),
        launcher=launcher,
        clock=clock,
    )

    assert service.run_once() == "idle"
    assert launcher.calls == 1
    assert clock.sleeps == [1, 1]

    timed_out = _service(
        hub=FakeHub(),
        anki=FakeAnki([unavailable] * 10),
        launcher=FakeLauncher(),
        clock=FakeClock(),
    )
    with pytest.raises(AnkiConnectUnavailable, match="deadline"):
        timed_out.run_once()


def test_write_command_is_rejected_before_snapshot_or_anki_write_behavior() -> None:
    hub = FakeHub([_command(AgentCommandType.APPLY_ENVELOPE)])
    snapshots = FakeSnapshots()
    service = _service(hub=hub, anki=FakeAnki([6]), snapshots=snapshots)

    with pytest.raises(WriteCommandDisabled):
        service.run_once()
    assert snapshots.commands == []
    assert snapshots.commits == []
    assert hub.uploads == []


class UploadFailingHub(FakeHub):
    def upload_snapshot(
        self,
        command_id: UUID,
        snapshot: SnapshotDelta,
    ) -> dict[str, str]:
        raise HubUnavailable("upload failed", transient=True)


def test_failed_upload_does_not_commit_snapshot_ledger() -> None:
    hub = UploadFailingHub([_command(AgentCommandType.DELTA_SNAPSHOT)])
    snapshots = FakeSnapshots()
    service = _service(hub=hub, anki=FakeAnki([6]), snapshots=snapshots)

    with pytest.raises(HubUnavailable):
        service.run_once()
    assert snapshots.commits == []


class FailingHub(FakeHub):
    def __init__(self, failures: Sequence[Exception]) -> None:
        super().__init__()
        self.failures = list(failures)

    def post_heartbeat(self, heartbeat: AgentHeartbeat) -> dict[str, str]:
        if self.failures:
            raise self.failures.pop(0)
        return super().post_heartbeat(heartbeat)


def test_run_retries_only_transient_service_failures_and_stops_cleanly() -> None:
    transient = HubUnavailable("offline", transient=True)
    hub = FailingHub([transient, transient])
    clock = FakeClock()
    stop = Event()

    def stop_after_three_sleeps(seconds: float) -> None:
        clock.sleep(seconds)
        if len(clock.sleeps) == 3:
            stop.set()

    service = _service(hub=hub, anki=FakeAnki([6]), clock=clock)
    service.sleep = stop_after_three_sleeps
    service.run(stop)

    assert clock.sleeps == [1, 2, 1]
    assert len(hub.heartbeats) == 1

    fatal = _service(
        hub=FailingHub(
            [HubAuthenticationError("bad token", transient=False)]
        ),
        anki=FakeAnki([6]),
    )
    with pytest.raises(HubAuthenticationError):
        fatal.run(Event())
