import asyncio
from pathlib import Path

from oms_hub.anki.ankiconnect import AnkiConnectUnavailable
from oms_hub.anki.runtime import AnkiRuntime
from oms_hub.app import create_app
from oms_hub.config import Settings


class FakeGateway:
    def __init__(self, *, unavailable_attempts: int = 0) -> None:
        self.unavailable_attempts = unavailable_attempts
        self.version_calls = 0
        self.closed = False

    async def version(self) -> int:
        self.version_calls += 1
        if self.version_calls <= self.unavailable_attempts:
            raise AnkiConnectUnavailable("AnkiConnect is unavailable")
        return 6

    async def get_active_profile(self) -> str:
        return "OMS NUC"

    async def find_notes(self, query: str) -> list[int]:
        assert query == ""
        return [11, 12]

    async def aclose(self) -> None:
        self.closed = True


class FakeLauncher:
    def __init__(self, *, running: bool = False) -> None:
        self.running = running
        self.launch_calls = 0

    async def is_running(self) -> bool:
        return self.running

    async def launch(self) -> None:
        self.launch_calls += 1
        self.running = True


async def _no_wait(_: float) -> None:
    return None


def test_preflight_reports_profile_and_collection_without_launching() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        launcher = FakeLauncher()
        runtime = AnkiRuntime(
            gateway,
            launcher,
            startup_attempts=2,
            startup_poll_seconds=0.01,
            sleep=_no_wait,
        )

        result = await runtime.preflight()

        assert result.reachable is True
        assert result.ankiconnect_version == 6
        assert result.active_profile == "OMS NUC"
        assert result.collection_accessible is True
        assert result.sync_available is True
        assert result.blocking_reason is None
        assert launcher.launch_calls == 0

    asyncio.run(scenario())


def test_preflight_unavailable_is_read_only() -> None:
    async def scenario() -> None:
        launcher = FakeLauncher()
        runtime = AnkiRuntime(
            FakeGateway(unavailable_attempts=10),
            launcher,
            startup_attempts=2,
            startup_poll_seconds=0.01,
            sleep=_no_wait,
        )

        result = await runtime.preflight()

        assert result.reachable is False
        assert result.collection_accessible is False
        assert result.blocking_reason == "AnkiConnect is unavailable"
        assert launcher.launch_calls == 0

    asyncio.run(scenario())


def test_ensure_running_launches_once_then_retries_preflight() -> None:
    async def scenario() -> None:
        gateway = FakeGateway(unavailable_attempts=1)
        launcher = FakeLauncher()
        runtime = AnkiRuntime(
            gateway,
            launcher,
            startup_attempts=3,
            startup_poll_seconds=0.01,
            sleep=_no_wait,
        )

        result = await runtime.ensure_running()

        assert result.reachable is True
        assert launcher.launch_calls == 1
        assert gateway.version_calls == 2

    asyncio.run(scenario())


def test_runtime_closes_owned_gateway() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        runtime = AnkiRuntime(
            gateway,
            FakeLauncher(),
            startup_attempts=1,
            startup_poll_seconds=0.01,
            sleep=_no_wait,
        )

        await runtime.aclose()

        assert gateway.closed is True

    asyncio.run(scenario())


def test_app_wires_local_runtime_only_when_anki_is_enabled(
    tmp_path: Path,
) -> None:
    disabled = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path / "disabled",
            database_url=f"sqlite:///{tmp_path / 'disabled.db'}",
            anki_enabled=False,
        )
    )
    enabled = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path / "enabled",
            database_url=f"sqlite:///{tmp_path / 'enabled.db'}",
            anki_enabled=True,
            dashboard_port=8787,
        )
    )
    try:
        assert disabled.state.anki_runtime is None
        assert isinstance(enabled.state.anki_runtime, AnkiRuntime)
        assert enabled.state.anki_curation_worker is not None
    finally:
        disabled.state.database.close()
        enabled.state.database.close()
        asyncio.run(enabled.state.anki_embedder.aclose())
        asyncio.run(enabled.state.anki_runtime.aclose())
