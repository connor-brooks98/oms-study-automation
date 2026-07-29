import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from oms_hub.anki.ankiconnect import AnkiConnectUnavailable

SOURCE_DECK_QUERY = 'deck:"Anking Step Deck"'
TARGET_NOTE_TYPE = "AnKingOverhaul (OMS_II_Extra/JCBrooks)"


class LocalAnkiRuntimeError(RuntimeError):
    """The NUC Anki installation is incomplete or cannot be launched safely."""


class RuntimeAnki(Protocol):
    def version(self) -> int: ...

    def find_notes(self, query: str) -> list[int]: ...

    def model_field_names(self, model_name: str) -> list[str]: ...


class AnkiLauncher(Protocol):
    def launch(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AnkiDoctorResult:
    ankiconnect_version: int
    source_note_count: int
    note_type_fields: tuple[str, ...]


class WindowsAnkiLauncher:
    def __init__(
        self,
        executable: Path,
        *,
        popen: Callable[..., object] = subprocess.Popen,
    ) -> None:
        if not executable.is_absolute():
            raise ValueError("Anki executable path must be absolute")
        self.executable = executable
        self.popen = popen

    def launch(self) -> None:
        if not self.executable.is_file():
            raise LocalAnkiRuntimeError(
                "Configured Anki executable does not exist"
            )
        self.popen([str(self.executable)], shell=False)


class LocalAnkiRuntime:
    def __init__(
        self,
        *,
        anki: RuntimeAnki,
        launcher: AnkiLauncher,
        startup_timeout_seconds: float,
        startup_poll_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if startup_timeout_seconds <= 0 or startup_poll_seconds <= 0:
            raise ValueError("Anki startup timing values must be positive")
        self.anki = anki
        self.launcher = launcher
        self.startup_timeout_seconds = startup_timeout_seconds
        self.startup_poll_seconds = startup_poll_seconds
        self.monotonic = monotonic
        self.sleep = sleep

    def ensure_available(self) -> int:
        try:
            return self.anki.version()
        except AnkiConnectUnavailable:
            self.launcher.launch()
        deadline = self.monotonic() + self.startup_timeout_seconds
        while self.monotonic() < deadline:
            remaining = deadline - self.monotonic()
            self.sleep(min(self.startup_poll_seconds, remaining))
            try:
                return self.anki.version()
            except AnkiConnectUnavailable:
                continue
        raise AnkiConnectUnavailable(
            "AnkiConnect did not become available before the startup deadline"
        )

    def doctor(self) -> AnkiDoctorResult:
        version = self.ensure_available()
        note_ids = self.anki.find_notes(SOURCE_DECK_QUERY)
        if not note_ids:
            raise LocalAnkiRuntimeError("Anking Step Deck is missing or empty")
        fields = tuple(self.anki.model_field_names(TARGET_NOTE_TYPE))
        if not {"Text", "Extra"} <= set(fields):
            raise LocalAnkiRuntimeError(
                "Generated note type must provide Text and Extra fields"
            )
        return AnkiDoctorResult(
            ankiconnect_version=version,
            source_note_count=len(note_ids),
            note_type_fields=fields,
        )
