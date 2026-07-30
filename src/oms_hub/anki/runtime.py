import asyncio
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from oms_hub.anki.ankiconnect import AnkiConnectError


class AnkiGateway(Protocol):
    async def version(self) -> int: ...

    async def get_active_profile(self) -> str: ...

    async def find_notes(self, query: str) -> list[int]: ...

    async def aclose(self) -> None: ...


class ProcessLauncher(Protocol):
    async def is_running(self) -> bool: ...

    async def launch(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AnkiPreflight:
    reachable: bool
    ankiconnect_version: int | None
    active_profile: str | None
    collection_accessible: bool
    sync_available: bool
    blocking_reason: str | None


class AnkiRuntime:
    def __init__(
        self,
        gateway: AnkiGateway,
        launcher: ProcessLauncher,
        *,
        startup_attempts: int,
        startup_poll_seconds: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if startup_attempts < 1:
            raise ValueError("startup_attempts must be positive")
        if startup_poll_seconds <= 0:
            raise ValueError("startup_poll_seconds must be positive")
        self.gateway = gateway
        self.launcher = launcher
        self.startup_attempts = startup_attempts
        self.startup_poll_seconds = startup_poll_seconds
        self._sleep = sleep

    async def preflight(self) -> AnkiPreflight:
        try:
            version = await self.gateway.version()
        except AnkiConnectError as exc:
            return AnkiPreflight(
                reachable=False,
                ankiconnect_version=None,
                active_profile=None,
                collection_accessible=False,
                sync_available=False,
                blocking_reason=str(exc),
            )
        try:
            profile = await self.gateway.get_active_profile()
            await self.gateway.find_notes("")
        except AnkiConnectError as exc:
            return AnkiPreflight(
                reachable=True,
                ankiconnect_version=version,
                active_profile=None,
                collection_accessible=False,
                sync_available=version >= 6,
                blocking_reason=str(exc),
            )
        return AnkiPreflight(
            reachable=True,
            ankiconnect_version=version,
            active_profile=profile,
            collection_accessible=True,
            sync_available=version >= 6,
            blocking_reason=None,
        )

    async def ensure_running(self) -> AnkiPreflight:
        result = await self.preflight()
        if result.reachable:
            return result
        if not await self.launcher.is_running():
            await self.launcher.launch()
        for _ in range(self.startup_attempts):
            await self._sleep(self.startup_poll_seconds)
            result = await self.preflight()
            if result.reachable:
                return result
        return result

    async def aclose(self) -> None:
        await self.gateway.aclose()


class WindowsAnkiLauncher:
    def __init__(self, executable: Path) -> None:
        self.executable = executable

    async def is_running(self) -> bool:
        if sys.platform != "win32":
            return False

        def check() -> bool:
            completed = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq anki.exe"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return "anki.exe" in completed.stdout.casefold()

        return await asyncio.to_thread(check)

    async def launch(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Anki launch is supported only on Windows")
        if not self.executable.is_file():
            raise RuntimeError(
                f"Anki executable was not found at {self.executable}"
            )
        await asyncio.create_subprocess_exec(str(self.executable))
