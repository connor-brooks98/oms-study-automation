import asyncio
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from oms_hub.anki.semantic.domain import (
    DocumentRecord,
    InputType,
    SemanticGenerationMismatchError,
)
from oms_hub.anki.semantic.service import (
    SemanticCoverageError,
    SemanticIndexService,
    content_hash,
)
from oms_hub.anki.semantic.store import SemanticSnapshotStore


class FakeEmbeddingClient:
    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors
        self.calls: list[tuple[InputType, tuple[str, ...]]] = []
        self.fail = False
        self.before_query: Callable[[], None] | None = None

    async def embed(
        self,
        texts: list[str] | tuple[str, ...],
        *,
        input_type: InputType,
    ) -> np.ndarray:
        self.calls.append((input_type, tuple(texts)))
        if self.fail:
            raise RuntimeError("injected embedding failure")
        if input_type == "query" and self.before_query is not None:
            self.before_query()
            self.before_query = None
        return np.asarray(
            [self.vectors[text] for text in texts],
            dtype=np.float32,
        )


def _record(note_id: int, text: str) -> DocumentRecord:
    return DocumentRecord(
        note_id=note_id,
        text=text,
        content_hash=content_hash(text),
    )


def _service(
    tmp_path: Path,
    embedder: FakeEmbeddingClient,
    *,
    min_coverage: float = 0.995,
) -> SemanticIndexService:
    return SemanticIndexService(
        SemanticSnapshotStore(tmp_path / "semantic"),
        embedder,
        model="voyage-4-large",
        dimensions=3,
        min_coverage=min_coverage,
        query_cache_size=2,
    )


def test_content_hash_normalizes_equivalent_note_text() -> None:
    assert content_hash("  Iron\u00a0 deficiency\nanemia  ") == content_hash(
        "Iron deficiency anemia"
    )
    assert content_hash("Iron deficiency anemia") != content_hash(
        "Iron deficiency anaemia"
    )


def test_refresh_embeds_only_added_and_changed_records(tmp_path: Path) -> None:
    async def scenario() -> None:
        embedder = FakeEmbeddingClient(
            {
                "first": [1.0, 0.0, 0.0],
                "second": [0.0, 1.0, 0.0],
                "second changed": [0.0, 0.8, 0.2],
                "third": [0.0, 0.0, 1.0],
            }
        )
        service = _service(tmp_path, embedder)
        await service.refresh([_record(1, "first"), _record(2, "second")])
        result = await service.refresh(
            [
                _record(1, "first"),
                _record(2, "second changed"),
                _record(3, "third"),
            ]
        )

        assert embedder.calls == [
            ("document", ("first", "second")),
            ("document", ("second changed", "third")),
        ]
        assert result.reused_count == 1
        assert result.embedded_count == 2
        assert result.deleted_count == 0
        assert result.coverage == 1.0
        assert result.manifest.note_ids == (1, 2, 3)

    asyncio.run(scenario())


def test_refresh_drops_deleted_notes_and_preserves_generation_on_failure(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        embedder = FakeEmbeddingClient(
            {
                "first": [1.0, 0.0, 0.0],
                "second": [0.0, 1.0, 0.0],
                "changed": [0.0, 0.0, 1.0],
            }
        )
        service = _service(tmp_path, embedder)
        initial = await service.refresh(
            [_record(1, "first"), _record(2, "second")]
        )
        embedder.fail = True

        with pytest.raises(RuntimeError, match="injected"):
            await service.refresh([_record(2, "changed")])

        loaded = service.store.load()
        assert loaded.manifest.generation == initial.manifest.generation
        assert loaded.manifest.note_ids == (1, 2)

    asyncio.run(scenario())


def test_refresh_removes_deleted_note_without_reembedding_survivor(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        embedder = FakeEmbeddingClient(
            {
                "first": [1.0, 0.0, 0.0],
                "second": [0.0, 1.0, 0.0],
            }
        )
        service = _service(tmp_path, embedder)
        await service.refresh(
            [_record(1, "first"), _record(2, "second")]
        )
        embedder.calls.clear()

        result = await service.refresh([_record(2, "second")])

        assert result.deleted_count == 1
        assert result.reused_count == 1
        assert result.embedded_count == 0
        assert result.manifest.note_ids == (2,)
        assert embedder.calls == []

    asyncio.run(scenario())


def test_refresh_enforces_coverage_before_embedding(tmp_path: Path) -> None:
    async def scenario() -> None:
        embedder = FakeEmbeddingClient({"first": [1.0, 0.0, 0.0]})
        service = _service(tmp_path, embedder)

        with pytest.raises(SemanticCoverageError, match="99.5"):
            await service.refresh(
                [_record(1, "first")],
                expected_note_ids=range(1, 202),
            )

        assert embedder.calls == []
        assert not service.store.current_path.exists()

    asyncio.run(scenario())


def test_search_filters_before_exact_ranking_and_sorts_ties(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        embedder = FakeEmbeddingClient(
            {
                "first": [1.0, 0.0, 0.0],
                "second": [0.0, 1.0, 0.0],
                "third": [0.0, 1.0, 0.0],
                "first query": [1.0, 0.0, 0.0],
                "tie query": [0.0, 1.0, 0.0],
            }
        )
        service = _service(tmp_path, embedder)
        await service.refresh(
            [
                _record(30, "third"),
                _record(10, "first"),
                _record(20, "second"),
            ]
        )

        filtered = await service.search(
            ["first query"],
            eligible_note_ids={20, 30},
            limit=10,
        )
        tied = await service.search(
            ["tie query"],
            eligible_note_ids={10, 20, 30},
            limit=10,
        )

        assert [hit.note_id for hit in filtered[0]] == [20, 30]
        assert [hit.note_id for hit in tied[0]] == [20, 30, 10]

    asyncio.run(scenario())


def test_query_cache_avoids_duplicate_embedding_calls_and_is_bounded(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        embedder = FakeEmbeddingClient(
            {
                "first": [1.0, 0.0, 0.0],
                "query one": [1.0, 0.0, 0.0],
                "query two": [0.0, 1.0, 0.0],
                "query three": [0.0, 0.0, 1.0],
            }
        )
        service = _service(tmp_path, embedder)
        await service.refresh([_record(1, "first")])

        await service.search(["query one", "query one"], limit=1)
        await service.search(["query two", "query three"], limit=1)
        await service.search(["query one"], limit=1)

        query_calls = [
            call for call in embedder.calls if call[0] == "query"
        ]
        assert query_calls == [
            ("query", ("query one",)),
            ("query", ("query two", "query three")),
            ("query", ("query one",)),
        ]

    asyncio.run(scenario())


def test_expected_generation_rejects_replacement_before_search(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        embedder = FakeEmbeddingClient(
            {
                "first": [1.0, 0.0, 0.0],
                "replacement": [0.0, 1.0, 0.0],
                "query": [1.0, 0.0, 0.0],
            }
        )
        service = _service(tmp_path, embedder)
        first = await service.refresh([_record(1, "first")])
        await service.refresh([_record(2, "replacement")])

        with pytest.raises(SemanticGenerationMismatchError, match="no longer active"):
            await service.search(
                ["query"],
                limit=1,
                expected_generation=str(first.manifest.generation),
            )

        assert ("query", ("query",)) not in embedder.calls

    asyncio.run(scenario())


def test_pinned_document_vectors_returns_exact_generation_without_embedding(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        embedder = FakeEmbeddingClient(
            {
                "first": [1.0, 0.0, 0.0],
                "second": [0.0, 1.0, 0.0],
            }
        )
        service = _service(tmp_path, embedder)
        generation = await service.refresh(
            [_record(1, "first"), _record(2, "second")]
        )
        embedder.calls.clear()

        vectors = await service.pinned_document_vectors(
            note_ids=(2, 1),
            expected_generation=str(generation.manifest.generation),
        )

        assert list(vectors) == [1, 2]
        assert vectors[1].tolist() == pytest.approx([1.0, 0.0, 0.0])
        assert vectors[2].tolist() == pytest.approx([0.0, 1.0, 0.0])
        assert vectors[1].flags.writeable is False
        assert embedder.calls == []

    asyncio.run(scenario())


def test_pinned_document_vectors_rejects_missing_note_without_embedding(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        embedder = FakeEmbeddingClient({"first": [1.0, 0.0, 0.0]})
        service = _service(tmp_path, embedder)
        generation = await service.refresh([_record(1, "first")])
        embedder.calls.clear()

        with pytest.raises(SemanticCoverageError, match="lacks scoped notes"):
            await service.pinned_document_vectors(
                note_ids=(1, 2),
                expected_generation=str(generation.manifest.generation),
            )

        assert embedder.calls == []

    asyncio.run(scenario())


def test_pinned_document_vectors_rejects_wrong_generation_without_embedding(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        embedder = FakeEmbeddingClient({"first": [1.0, 0.0, 0.0]})
        service = _service(tmp_path, embedder)
        generation = await service.refresh([_record(1, "first")])
        embedder.calls.clear()

        with pytest.raises(SemanticGenerationMismatchError, match="no longer active"):
            await service.pinned_document_vectors(
                note_ids=(1,),
                expected_generation=f"{generation.manifest.generation}-replacement",
            )

        assert embedder.calls == []

    asyncio.run(scenario())


def test_search_holds_pinned_snapshot_when_generation_switches_during_query(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        embedder = FakeEmbeddingClient(
            {
                "first": [1.0, 0.0, 0.0],
                "replacement": [0.0, 1.0, 0.0],
                "query": [1.0, 0.0, 0.0],
            }
        )
        service = _service(tmp_path, embedder)
        first = await service.refresh([_record(1, "first")])
        embedder.before_query = lambda: service.store.replace(
            [_record(2, "replacement")],
            np.asarray([[0.0, 1.0, 0.0]], dtype=np.float32),
            model="voyage-4-large",
        )

        hits = await service.search(
            ["query"],
            limit=1,
            expected_generation=str(first.manifest.generation),
        )

        assert [hit.note_id for hit in hits[0]] == [1]
        assert service.store.load().manifest.note_ids == (2,)

    asyncio.run(scenario())


@pytest.mark.performance
def test_exact_search_handles_68000_note_ids(
    tmp_path: Path,
    record_property,
) -> None:
    async def scenario() -> None:
        count = 68_000
        embedder = FakeEmbeddingClient(
            {
                "query": [1.0, 0.0, 0.0],
            }
        )
        store = SemanticSnapshotStore(tmp_path / "semantic")
        records = [
            DocumentRecord(
                note_id=note_id,
                text=f"note {note_id}",
                content_hash=f"{note_id:064x}",
            )
            for note_id in range(1, count + 1)
        ]
        vectors = np.zeros((count, 3), dtype=np.float32)
        vectors[:, 0] = 1.0
        store.replace(records, vectors, model="voyage-4-large")
        service = SemanticIndexService(
            store,
            embedder,
            model="voyage-4-large",
            dimensions=3,
            min_coverage=0.995,
            query_cache_size=2,
        )

        started = time.perf_counter()
        hits = await service.search(["query"], limit=5)
        elapsed = time.perf_counter() - started

        assert [hit.note_id for hit in hits[0]] == [1, 2, 3, 4, 5]
        assert store.load().matrix.nbytes == count * 3 * 2
        record_property("semantic_search_seconds", elapsed)

    asyncio.run(scenario())
