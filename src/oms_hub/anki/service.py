"""Composition and serialized snapshot operations for NUC-local Anki."""

from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from oms_hub import __version__
from oms_hub.anki.ankiconnect import AnkiConnectClient
from oms_hub.anki.contracts import SnapshotManifest
from oms_hub.anki.ledger import AnkiLedger
from oms_hub.anki.runtime import (
    AnkiDoctorResult,
    LocalAnkiRuntime,
    WindowsAnkiLauncher,
)
from oms_hub.anki.snapshot_export import (
    EXPORT_VERSION,
    FullSnapshotExporter,
    SnapshotVersions,
    snapshot_note_hashes,
)

if TYPE_CHECKING:
    from oms_hub.config import Settings

NORMALIZER_VERSION = "1"


class LocalAnkiConfigurationError(RuntimeError):
    """The NUC-local Anki runtime is disabled or incomplete."""


class LocalAnkiService:
    """Own NUC-local health checks and durable snapshot publication."""

    def __init__(
        self,
        *,
        runtime: LocalAnkiRuntime,
        exporter: FullSnapshotExporter,
        ledger: AnkiLedger,
        data_dir: Path,
        versions: SnapshotVersions,
        lock: threading.RLock | None = None,
    ) -> None:
        self.runtime = runtime
        self.exporter = exporter
        self.anki = exporter.anki
        self.ledger = ledger
        self.data_dir = data_dir
        self.versions = versions
        self.lock = lock or threading.RLock()
        self.snapshot_path = data_dir / "snapshots" / "current.jsonl.gz"

    def doctor(self) -> AnkiDoctorResult:
        with self.lock:
            return self.runtime.doctor()

    def export_full(
        self,
        *,
        exported_at: datetime | None = None,
    ) -> SnapshotManifest:
        with self.lock:
            self.runtime.ensure_available()
            snapshot_dir = self.snapshot_path.parent
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            temporary = snapshot_dir / f".current-{uuid4().hex}.jsonl.gz"
            try:
                manifest = self.exporter.export(
                    temporary,
                    exported_at=exported_at,
                )
                hashes = snapshot_note_hashes(temporary)
                if len(hashes) != manifest.note_count:
                    raise ValueError("snapshot note count changed during validation")
                with temporary.open("r+b") as stream:
                    os.fsync(stream.fileno())
                temporary.replace(self.snapshot_path)
            finally:
                temporary.unlink(missing_ok=True)
            self.ledger.replace_note_hashes(hashes)
            self.ledger.set_snapshot_state(
                exported_at=manifest.exported_at,
                note_count=manifest.note_count,
                versions=self.versions,
            )
            return manifest


def build_local_anki_service(
    settings: Settings,
    *,
    lock: threading.RLock | None = None,
) -> LocalAnkiService:
    """Build the local runtime without contacting or launching Anki."""

    if not settings.anki_enabled:
        raise LocalAnkiConfigurationError("NUC-local Anki curation is disabled")
    if settings.anki_executable_path is None:
        raise LocalAnkiConfigurationError(
            "OMS_HUB_ANKI_EXECUTABLE_PATH is required"
        )
    anki = AnkiConnectClient(url=settings.anki_connect_url)
    ledger = AnkiLedger(settings.resolved_anki_data_dir / "ledger.sqlite3")
    runtime = LocalAnkiRuntime(
        anki=anki,
        launcher=WindowsAnkiLauncher(settings.anki_executable_path),
        startup_timeout_seconds=settings.anki_startup_timeout_seconds,
        startup_poll_seconds=settings.anki_startup_poll_seconds,
    )
    versions = SnapshotVersions(
        export_version=EXPORT_VERSION,
        normalizer_version=NORMALIZER_VERSION,
        embedding_model=settings.anki_embedding_model,
    )
    return LocalAnkiService(
        runtime=runtime,
        exporter=FullSnapshotExporter(
            anki=anki,
            producer_version=__version__,
        ),
        ledger=ledger,
        data_dir=settings.resolved_anki_data_dir,
        versions=versions,
        lock=lock,
    )
