"""Offline-injectable Anki snapshot build/upload orchestration."""

import inspect
from typing import Any, Protocol, cast

from oms_hub.anki.learning_contracts import AnkiLearningSnapshot
from oms_hub.anki.learning_repository import (
    AnkiLearningRepository,
    AnkiSyncRun,
    normalize_snapshot,
)


class AnkiLearningSnapshotReader(Protocol):
    async def read_snapshot(self, query: str = "") -> AnkiLearningSnapshot: ...


class AnkiLearningUploader(Protocol):
    async def upload(self, snapshot: AnkiLearningSnapshot) -> object: ...


class AnkiLearningSync:
    """Build, upload, and receipt a minimized snapshot through injected ports."""

    def __init__(
        self,
        reader: object,
        uploader: object,
        repository: AnkiLearningRepository,
        *,
        query: str = "",
    ) -> None:
        self._reader = reader
        self._uploader = uploader
        self._repository = repository
        self._query = query

    async def build_and_upload(self) -> AnkiSyncRun:
        snapshot = await self._read_snapshot()
        normalized = normalize_snapshot(snapshot)
        await self._upload(normalized)
        return self._repository.record_sync(normalized)

    async def _read_snapshot(self) -> AnkiLearningSnapshot:
        method = getattr(self._reader, "read_snapshot", None)
        if method is None:
            method = getattr(self._reader, "snapshot", None)
        if method is None or not callable(method):
            raise TypeError("reader must provide read_snapshot or snapshot")
        result = method(self._query)
        if inspect.isawaitable(result):
            result = await result
        return cast(AnkiLearningSnapshot, result)

    async def _upload(self, snapshot: AnkiLearningSnapshot) -> None:
        method = getattr(self._uploader, "upload", None)
        if method is None:
            method = getattr(self._uploader, "upload_snapshot", None)
        if method is None or not callable(method):
            raise TypeError("uploader must provide upload or upload_snapshot")
        result: Any = method(snapshot)
        if inspect.isawaitable(result):
            await result


__all__ = [
    "AnkiLearningSnapshotReader",
    "AnkiLearningSync",
    "AnkiLearningUploader",
]
