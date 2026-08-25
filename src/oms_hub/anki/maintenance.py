import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from oms_hub.anki.index import (
    LocalAnkiReader,
    SemanticRefresher,
)
from oms_hub.anki.normalize import NormalizedNote, semantic_text
from oms_hub.anki.runtime import AnkiPreflight
from oms_hub.anki.semantic.domain import SemanticSnapshot
from oms_hub.anki.semantic.service import content_hash


class LocalIndexRefreshError(RuntimeError):
    """A local index refresh stopped before a usable generation existed."""


class RuntimePreflight(Protocol):
    async def ensure_running(self) -> AnkiPreflight: ...


class CompanionRefresher(Protocol):
    async def refresh_from_anki(
        self,
        gateway: LocalAnkiReader,
        *,
        snapshot_id: str,
        fingerprint: str,
        query: str = "",
        semantic_refresher: SemanticRefresher | None = None,
    ) -> list[NormalizedNote]: ...


class SemanticSnapshotReader(Protocol):
    def load(
        self,
        *,
        expected_model: str | None = None,
        expected_dimensions: int | None = None,
    ) -> SemanticSnapshot: ...


@dataclass(frozen=True, slots=True)
class LocalIndexRefreshResult:
    active_profile: str
    companion_generation: str
    semantic_generation: str
    note_count: int
    semantic_count: int
    semantic_coverage: float
    duration_ms: float
    semantic_snapshot_size_bytes: int
    peak_memory_bytes: int


class LocalIndexMaintainer:
    def __init__(
        self,
        runtime: RuntimePreflight,
        gateway: LocalAnkiReader,
        companion: CompanionRefresher,
        semantic: SemanticRefresher,
        semantic_store: SemanticSnapshotReader,
        *,
        semantic_model: str,
        semantic_dimensions: int,
        min_coverage: float,
        snapshot_id_factory: Callable[[], str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        peak_memory_bytes: Callable[[], int] | None = None,
    ) -> None:
        if not semantic_model.strip():
            raise ValueError("semantic model cannot be empty")
        if semantic_dimensions < 1:
            raise ValueError("semantic dimensions must be positive")
        if not 0 <= min_coverage <= 1:
            raise ValueError("semantic coverage threshold is invalid")
        self.runtime = runtime
        self.gateway = gateway
        self.companion = companion
        self.semantic = semantic
        self.semantic_store = semantic_store
        self.semantic_model = semantic_model
        self.semantic_dimensions = semantic_dimensions
        self.min_coverage = min_coverage
        self.snapshot_id_factory = (
            snapshot_id_factory
            if snapshot_id_factory is not None
            else lambda: f"local-{uuid4()}"
        )
        self.monotonic = monotonic
        self.peak_memory_bytes = peak_memory_bytes or (lambda: 0)

    async def refresh(self, *, query: str = "") -> LocalIndexRefreshResult:
        preflight = await self.runtime.ensure_running()
        if (
            not preflight.reachable
            or not preflight.collection_accessible
            or not preflight.sync_available
            or not preflight.active_profile
        ):
            raise LocalIndexRefreshError(
                preflight.blocking_reason
                or "Local Anki preflight did not pass"
            )
        await self.gateway.sync()
        normalized_query = query.strip()
        snapshot_id = self.snapshot_id_factory().strip()
        if not snapshot_id:
            raise LocalIndexRefreshError("snapshot generation is invalid")
        fingerprint = hashlib.sha256(
            (
                f"{preflight.active_profile}\0"
                f"{snapshot_id}\0{normalized_query}"
            ).encode()
        ).hexdigest()
        started = self.monotonic()
        notes = await self.companion.refresh_from_anki(
            self.gateway,
            snapshot_id=snapshot_id,
            fingerprint=fingerprint,
            query=normalized_query,
            semantic_refresher=self.semantic,
        )
        snapshot = self.semantic_store.load(
            expected_model=self.semantic_model,
            expected_dimensions=self.semantic_dimensions,
        )
        semantic_hashes = dict(
            zip(
                snapshot.manifest.note_ids,
                snapshot.manifest.content_hashes,
                strict=True,
            )
        )
        note_ids = {note.note_id for note in notes}
        semantic_notes = [
            (note.note_id, semantic_text(note))
            for note in notes
            if semantic_text(note).strip()
        ]
        indexed_count = sum(
            semantic_hashes.get(note_id) == content_hash(text)
            for note_id, text in semantic_notes
        )
        coverage = (
            indexed_count / len(semantic_notes) if semantic_notes else 1.0
        )
        if coverage < self.min_coverage:
            raise LocalIndexRefreshError(
                f"semantic coverage {coverage:.3%} is below required "
                f"{self.min_coverage:.3%}"
            )
        elapsed_ms = (self.monotonic() - started) * 1000
        return LocalIndexRefreshResult(
            active_profile=preflight.active_profile,
            companion_generation=snapshot_id,
            semantic_generation=str(snapshot.manifest.generation),
            note_count=len(note_ids),
            semantic_count=indexed_count,
            semantic_coverage=coverage,
            duration_ms=elapsed_ms,
            semantic_snapshot_size_bytes=int(snapshot.matrix.nbytes),
            peak_memory_bytes=self.peak_memory_bytes(),
        )
