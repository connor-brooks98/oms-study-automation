import asyncio
import hashlib
from collections.abc import Collection, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from oms_hub.anki.maintenance import (
    LocalIndexMaintainer,
    LocalIndexRefreshError,
)
from oms_hub.anki.normalize import NormalizedNote
from oms_hub.anki.runtime import AnkiPreflight
from oms_hub.anki.semantic.domain import DocumentRecord
from oms_hub.anki.semantic.service import content_hash


def _note(
    note_id: int,
    *,
    text: str | None = None,
    extra: str = "",
) -> NormalizedNote:
    resolved_text = f"note {note_id}" if text is None else text
    return NormalizedNote(
        note_id=note_id,
        model_name="AnKing",
        text=resolved_text,
        extra=extra,
        raw_fields={"Text": resolved_text, "Extra": extra},
        tags=(),
        card_ids=(note_id + 100,),
        media=(),
        token_signature=str(note_id),
        content_sha256=f"{note_id:064x}",
    )


class FakeRuntime:
    def __init__(self, result: AnkiPreflight) -> None:
        self.result = result

    async def ensure_running(self) -> AnkiPreflight:
        return self.result


class FakeGateway:
    def __init__(self) -> None:
        self.sync_calls = 0

    async def sync(self) -> None:
        self.sync_calls += 1


class FakeSemantic:
    async def refresh(
        self,
        records: Sequence[DocumentRecord],
        *,
        expected_note_ids: Collection[int] | None = None,
    ) -> object:
        del records, expected_note_ids
        return object()


class FakeCompanion:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def refresh_from_anki(
        self,
        gateway: object,
        *,
        snapshot_id: str,
        fingerprint: str,
        query: str = "",
        semantic_refresher: object | None = None,
    ) -> list[NormalizedNote]:
        self.calls.append(
            {
                "gateway": gateway,
                "snapshot_id": snapshot_id,
                "fingerprint": fingerprint,
                "query": query,
                "semantic_refresher": semantic_refresher,
            }
        )
        return [_note(1), _note(2)]


class FakeStore:
    def load(
        self,
        *,
        expected_model: str | None = None,
        expected_dimensions: int | None = None,
    ) -> object:
        assert expected_model == "voyage-4-large"
        assert expected_dimensions == 1024
        return SimpleNamespace(
            manifest=SimpleNamespace(
                generation="semantic-generation",
                note_ids=(1, 2),
                content_hashes=(
                    content_hash("note 1"),
                    content_hash("note 2"),
                ),
            ),
            matrix=SimpleNamespace(nbytes=4096),
        )


def _preflight(*, reachable: bool = True) -> AnkiPreflight:
    return AnkiPreflight(
        reachable=reachable,
        ankiconnect_version=6 if reachable else None,
        active_profile="Acceptance Copy" if reachable else None,
        collection_accessible=reachable,
        sync_available=reachable,
        blocking_reason=None if reachable else "AnkiConnect unavailable",
    )


def test_local_index_refresh_publishes_companion_and_semantic_generations(
    tmp_path: Path,
) -> None:
    del tmp_path

    async def scenario() -> None:
        companion = FakeCompanion()
        gateway = FakeGateway()
        semantic = FakeSemantic()
        maintainer = LocalIndexMaintainer(
            FakeRuntime(_preflight()),
            gateway,
            companion,
            semantic,
            FakeStore(),
            semantic_model="voyage-4-large",
            semantic_dimensions=1024,
            min_coverage=0.995,
            snapshot_id_factory=lambda: "local-fixed",
            monotonic=_clock(10.0, 12.5),
            peak_memory_bytes=lambda: 8192,
        )

        result = await maintainer.refresh(query='deck:"AnKing Step Deck"')

        assert result.active_profile == "Acceptance Copy"
        assert result.companion_generation == "local-fixed"
        assert result.semantic_generation == "semantic-generation"
        assert result.note_count == 2
        assert result.semantic_count == 2
        assert result.semantic_coverage == 1.0
        assert result.duration_ms == 2500.0
        assert result.semantic_snapshot_size_bytes == 4096
        assert result.peak_memory_bytes == 8192
        assert gateway.sync_calls == 1
        assert companion.calls == [
            {
                "gateway": gateway,
                "snapshot_id": "local-fixed",
                "fingerprint": hashlib.sha256(
                    b"Acceptance Copy\0local-fixed\0deck:\"AnKing Step Deck\""
                ).hexdigest(),
                "query": 'deck:"AnKing Step Deck"',
                "semantic_refresher": semantic,
            }
        ]

    asyncio.run(scenario())


def test_local_index_refresh_stops_when_preflight_is_not_safe() -> None:
    async def scenario() -> None:
        companion = FakeCompanion()
        maintainer = LocalIndexMaintainer(
            FakeRuntime(_preflight(reachable=False)),
            FakeGateway(),
            companion,
            FakeSemantic(),
            FakeStore(),
            semantic_model="voyage-4-large",
            semantic_dimensions=1024,
            min_coverage=0.995,
        )

        with pytest.raises(
            LocalIndexRefreshError,
            match="AnkiConnect unavailable",
        ):
            await maintainer.refresh()

        assert companion.calls == []

    asyncio.run(scenario())


def test_local_index_refresh_excludes_blank_notes_from_semantic_coverage() -> None:
    class BlankNoteCompanion(FakeCompanion):
        async def refresh_from_anki(
            self,
            gateway: object,
            *,
            snapshot_id: str,
            fingerprint: str,
            query: str = "",
            semantic_refresher: object | None = None,
        ) -> list[NormalizedNote]:
            del gateway, snapshot_id, fingerprint, query, semantic_refresher
            return [_note(1), _note(2, text="")]

    class OneNoteStore:
        def load(
            self,
            *,
            expected_model: str | None = None,
            expected_dimensions: int | None = None,
        ) -> object:
            assert expected_model == "voyage-4-large"
            assert expected_dimensions == 1024
            return SimpleNamespace(
                manifest=SimpleNamespace(
                    generation="semantic-generation",
                    note_ids=(1,),
                    content_hashes=(content_hash("note 1"),),
                ),
                matrix=SimpleNamespace(nbytes=2048),
            )

    async def scenario() -> None:
        maintainer = LocalIndexMaintainer(
            FakeRuntime(_preflight()),
            FakeGateway(),
            BlankNoteCompanion(),
            FakeSemantic(),
            OneNoteStore(),
            semantic_model="voyage-4-large",
            semantic_dimensions=1024,
            min_coverage=0.995,
            snapshot_id_factory=lambda: "local-fixed",
            monotonic=_clock(10.0, 12.5),
        )

        result = await maintainer.refresh()

        assert result.note_count == 2
        assert result.semantic_count == 1
        assert result.semantic_coverage == 1.0

    asyncio.run(scenario())


def _clock(*values: float):
    remaining = iter(values)
    return lambda: next(remaining)
