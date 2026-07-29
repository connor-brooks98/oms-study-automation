from pathlib import Path
from typing import Any

import pytest

from oms_hub.anki.ankiconnect import AnkiConnectUnavailable
from oms_hub.anki.runtime import (
    LocalAnkiRuntime,
    LocalAnkiRuntimeError,
    WindowsAnkiLauncher,
)


class SequencedAnki:
    def __init__(
        self,
        versions: list[int | Exception],
        *,
        note_ids: list[int] | None = None,
        fields: list[str] | None = None,
    ) -> None:
        self.versions = list(versions)
        self.note_ids = [11] if note_ids is None else note_ids
        self.fields = ["Text", "Extra"] if fields is None else fields
        self.version_calls = 0
        self.queries: list[str] = []
        self.models: list[str] = []

    def version(self) -> int:
        self.version_calls += 1
        value = self.versions.pop(0) if self.versions else 6
        if isinstance(value, Exception):
            raise value
        return value

    def find_notes(self, query: str) -> list[int]:
        self.queries.append(query)
        return self.note_ids

    def model_field_names(self, model_name: str) -> list[str]:
        self.models.append(model_name)
        return self.fields


class RecordingLauncher:
    def __init__(self) -> None:
        self.calls = 0

    def launch(self) -> None:
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


class RecordingPopen:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool]] = []

    def __call__(
        self,
        arguments: list[str],
        *,
        shell: bool,
        **kwargs: Any,
    ) -> object:
        assert kwargs == {}
        self.calls.append((arguments, shell))
        return object()


def _runtime(
    anki: SequencedAnki,
    launcher: RecordingLauncher | None = None,
    clock: FakeClock | None = None,
) -> tuple[LocalAnkiRuntime, RecordingLauncher, FakeClock]:
    current_launcher = launcher or RecordingLauncher()
    current_clock = clock or FakeClock()
    return (
        LocalAnkiRuntime(
            anki=anki,
            launcher=current_launcher,
            startup_timeout_seconds=3,
            startup_poll_seconds=1,
            monotonic=current_clock.monotonic,
            sleep=current_clock.sleep,
        ),
        current_launcher,
        current_clock,
    )


def test_runtime_launches_anki_once_and_waits_to_bounded_deadline() -> None:
    unavailable = AnkiConnectUnavailable("offline")
    runtime, launcher, clock = _runtime(SequencedAnki([unavailable, 6]))

    assert runtime.ensure_available() == 6
    assert launcher.calls == 1
    assert clock.sleeps == [1]


def test_runtime_does_not_launch_when_ankiconnect_is_available() -> None:
    runtime, launcher, clock = _runtime(SequencedAnki([6]))

    assert runtime.ensure_available() == 6
    assert launcher.calls == 0
    assert clock.sleeps == []


def test_runtime_stops_waiting_at_startup_deadline() -> None:
    unavailable = AnkiConnectUnavailable("offline")
    runtime, launcher, clock = _runtime(SequencedAnki([unavailable] * 10))

    with pytest.raises(AnkiConnectUnavailable, match="startup deadline"):
        runtime.ensure_available()
    assert launcher.calls == 1
    assert clock.sleeps == [1, 1, 1]


def test_windows_launcher_uses_absolute_executable_without_shell(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "Anki.exe"
    executable.touch()
    popen = RecordingPopen()

    WindowsAnkiLauncher(executable, popen=popen).launch()

    assert popen.calls == [([str(executable)], False)]


def test_doctor_checks_source_deck_and_generated_note_type() -> None:
    anki = SequencedAnki([6], note_ids=[11, 22], fields=["Text", "Extra", "Tags"])
    runtime, _, _ = _runtime(anki)

    result = runtime.doctor()

    assert result.ankiconnect_version == 6
    assert result.source_note_count == 2
    assert result.note_type_fields == ("Text", "Extra", "Tags")
    assert anki.queries == ['deck:"Anking Step Deck"']
    assert anki.models == ["AnKingOverhaul (OMS_II_Extra/JCBrooks)"]


@pytest.mark.parametrize(
    ("note_ids", "fields", "message"),
    [
        ([], ["Text", "Extra"], "missing or empty"),
        ([11], ["Text"], "Text and Extra"),
    ],
)
def test_doctor_rejects_incomplete_nuc_anki_setup(
    note_ids: list[int],
    fields: list[str],
    message: str,
) -> None:
    runtime, _, _ = _runtime(
        SequencedAnki([6], note_ids=note_ids, fields=fields)
    )

    with pytest.raises(LocalAnkiRuntimeError, match=message):
        runtime.doctor()
