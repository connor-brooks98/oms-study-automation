import hashlib
import re
import unicodedata
from collections import OrderedDict
from collections.abc import Collection, Sequence

import numpy as np

from oms_hub.anki.semantic.domain import (
    DocumentRecord,
    EmbeddingClient,
    FloatMatrix,
    PinnedCentroidSimilarityResult,
    SemanticHit,
    SemanticRefreshResult,
    SemanticSnapshot,
)
from oms_hub.anki.semantic.store import SemanticSnapshotStore


class SemanticCoverageError(ValueError):
    """The candidate snapshot does not cover enough eligible Anki notes."""


class SemanticIndexService:
    def __init__(
        self,
        store: SemanticSnapshotStore,
        embedder: EmbeddingClient,
        *,
        model: str,
        dimensions: int,
        min_coverage: float,
        query_cache_size: int,
    ) -> None:
        if not model.strip():
            raise ValueError("model cannot be empty")
        if dimensions < 1:
            raise ValueError("dimensions must be positive")
        if not 0 <= min_coverage <= 1:
            raise ValueError("min_coverage must be between zero and one")
        if query_cache_size < 1:
            raise ValueError("query_cache_size must be positive")
        self.store = store
        self.embedder = embedder
        self.model = model
        self.dimensions = dimensions
        self.min_coverage = min_coverage
        self.query_cache_size = query_cache_size
        self._query_cache: OrderedDict[str, FloatMatrix] = OrderedDict()

    async def refresh(
        self,
        records: Sequence[DocumentRecord],
        *,
        expected_note_ids: Collection[int] | None = None,
    ) -> SemanticRefreshResult:
        validated = self._validated_records(records)
        target_ids = {record.note_id for record in validated}
        expected = set(target_ids) if expected_note_ids is None else set(expected_note_ids)
        if any(note_id <= 0 for note_id in expected):
            raise ValueError("expected note IDs must be positive")
        if not target_ids <= expected:
            raise ValueError("semantic records contain notes outside the expected universe")
        coverage = len(target_ids) / len(expected) if expected else 1.0
        if coverage < self.min_coverage:
            raise SemanticCoverageError(
                f"semantic coverage {coverage:.1%} is below required {self.min_coverage:.1%}"
            )

        current = (
            self.store.load(
                expected_model=self.model,
                expected_dimensions=self.dimensions,
            )
            if self.store.current_path.exists()
            else None
        )
        current_rows = (
            {
                note_id: (row, current.manifest.content_hashes[row])
                for row, note_id in enumerate(current.manifest.note_ids)
            }
            if current is not None
            else {}
        )
        matrix = np.empty(
            (len(validated), self.dimensions),
            dtype=np.float32,
        )
        changed_rows: list[int] = []
        changed_texts: list[str] = []
        reused_count = 0
        for row, record in enumerate(validated):
            existing = current_rows.get(record.note_id)
            if existing is not None and existing[1] == record.content_hash and current is not None:
                matrix[row] = current.matrix[existing[0]].astype(np.float32)
                reused_count += 1
            else:
                changed_rows.append(row)
                changed_texts.append(record.text)
        if changed_texts:
            embedded = await self.embedder.embed(
                changed_texts,
                input_type="document",
            )
            embedded = _normalized_rows(
                embedded,
                expected_rows=len(changed_rows),
                dimensions=self.dimensions,
            )
            matrix[changed_rows] = embedded
        if len(validated):
            matrix = _normalized_rows(
                matrix,
                expected_rows=len(validated),
                dimensions=self.dimensions,
            )
        manifest = self.store.replace(
            validated,
            matrix,
            model=self.model,
        )
        return SemanticRefreshResult(
            manifest=manifest,
            reused_count=reused_count,
            embedded_count=len(changed_rows),
            deleted_count=len(set(current_rows) - target_ids),
            coverage=coverage,
        )

    async def search(
        self,
        queries: Sequence[str],
        *,
        eligible_note_ids: Collection[int] | None = None,
        limit: int,
        expected_generation: str | None = None,
    ) -> list[list[SemanticHit]]:
        if limit < 1:
            raise ValueError("limit must be positive")
        normalized_queries = [normalize_semantic_text(query) for query in queries]
        if any(not query for query in normalized_queries):
            raise ValueError("semantic queries cannot be blank")
        if not normalized_queries:
            return []
        snapshot = self.store.load(
            expected_model=self.model,
            expected_dimensions=self.dimensions,
            expected_generation=expected_generation,
        )
        if (
            expected_generation is not None
            and str(snapshot.manifest.generation) != expected_generation
        ):
            raise ValueError("pinned semantic generation is no longer active")
        eligible = None if eligible_note_ids is None else set(eligible_note_ids)
        if expected_generation is not None and eligible is not None:
            if eligible - set(snapshot.manifest.note_ids):
                raise SemanticCoverageError("pinned semantic snapshot lacks eligible notes")
        selected_rows = [
            row
            for row, note_id in enumerate(snapshot.manifest.note_ids)
            if eligible is None or note_id in eligible
        ]
        if not selected_rows:
            return [[] for _ in normalized_queries]
        query_vectors = await self._query_vectors(normalized_queries)
        matrix = snapshot.matrix[selected_rows].astype(np.float32)
        if expected_generation is not None:
            _validate_note_vectors(matrix, dimensions=self.dimensions)
        selected_note_ids = np.asarray(
            [snapshot.manifest.note_ids[row] for row in selected_rows],
            dtype=np.int64,
        )
        results: list[list[SemanticHit]] = []
        for query_vector in query_vectors:
            scores = matrix @ query_vector
            order = np.lexsort((selected_note_ids, -scores))[:limit]
            results.append(
                [
                    SemanticHit(
                        note_id=int(selected_note_ids[index]),
                        score=float(scores[index]),
                        content_hash=snapshot.manifest.content_hashes[selected_rows[index]],
                    )
                    for index in order
                ]
            )
        return results

    async def pinned_similarity(
        self,
        queries: Sequence[str],
        *,
        note_ids: Collection[int],
        expected_generation: str,
    ) -> dict[int, float]:
        """Score notes from the pinned snapshot only; never re-embed note text."""
        snapshot = self.store.load(
            expected_model=self.model,
            expected_dimensions=self.dimensions,
            expected_generation=expected_generation,
        )
        normalized = [normalize_semantic_text(query) for query in queries]
        if not normalized or any(not query for query in normalized):
            raise ValueError("semantic queries cannot be blank")
        vectors = await self._query_vectors(normalized)
        rows = {
            note_id: row
            for row, note_id in enumerate(snapshot.manifest.note_ids)
            if note_id in set(note_ids)
        }
        missing = set(note_ids) - set(rows)
        if missing:
            raise SemanticCoverageError("pinned semantic snapshot lacks scoped notes")
        matrix = snapshot.matrix[list(rows.values())].astype(np.float32)
        query_matrix = np.asarray(vectors, dtype=np.float32)
        scores = matrix @ query_matrix.T
        return {note_id: float(scores[index].max()) for index, note_id in enumerate(rows)}

    async def pinned_document_vectors(
        self,
        *,
        note_ids: Collection[int],
        expected_generation: str,
    ) -> dict[int, FloatMatrix]:
        """Return requested frozen document vectors without provider embedding.

        This is deliberately all-or-nothing: callers that pin a semantic
        generation must not quietly substitute a current snapshot or re-embed
        missing documents.
        """
        snapshot = self._pinned_snapshot(expected_generation)
        requested_ids = set(note_ids)
        if any(note_id <= 0 for note_id in requested_ids):
            raise SemanticCoverageError("scoped semantic note IDs must be positive")
        if not requested_ids:
            return {}
        rows = {
            note_id: row
            for row, note_id in enumerate(snapshot.manifest.note_ids)
            if note_id in requested_ids
        }
        missing = requested_ids - set(rows)
        if missing:
            raise SemanticCoverageError("pinned semantic snapshot lacks scoped notes")
        ordered_note_ids = tuple(sorted(requested_ids))
        matrix = snapshot.matrix[[rows[note_id] for note_id in ordered_note_ids]].astype(
            np.float32
        )
        _validate_note_vectors(matrix, dimensions=self.dimensions)
        vectors: dict[int, FloatMatrix] = {}
        for row, note_id in enumerate(ordered_note_ids):
            vector = matrix[row].copy()
            vector.flags.writeable = False
            vectors[note_id] = vector
        return vectors

    async def pinned_centroid_similarity(
        self,
        concept_terms: Sequence[Sequence[str]],
        *,
        note_ids: Collection[int],
        expected_generation: str,
    ) -> PinnedCentroidSimilarityResult:
        """Score pinned note vectors against normalized multi-term centroids."""
        snapshot = self._pinned_snapshot(expected_generation)
        requested_ids = set(note_ids)
        if any(note_id <= 0 for note_id in requested_ids):
            raise SemanticCoverageError("scoped semantic note IDs must be positive")
        rows = {
            note_id: row
            for row, note_id in enumerate(snapshot.manifest.note_ids)
            if note_id in requested_ids
        }
        unavailable_note_ids = tuple(sorted(requested_ids - set(rows)))
        normalized_terms = [
            tuple(normalize_semantic_text(term) for term in terms)
            for terms in concept_terms
        ]
        if not normalized_terms or any(
            not terms or any(not term for term in terms) for terms in normalized_terms
        ):
            raise ValueError("semantic concept terms cannot be blank")
        query_vectors = await self._query_vectors(
            tuple(term for terms in normalized_terms for term in terms)
        )
        centroids: list[FloatMatrix] = []
        offset = 0
        for terms in normalized_terms:
            centroids.append(_normalized_centroid(query_vectors[offset : offset + len(terms)]))
            offset += len(terms)
        ordered_note_ids = tuple(sorted(rows))
        matrix = snapshot.matrix[[rows[note_id] for note_id in ordered_note_ids]].astype(
            np.float32
        )
        _validate_note_vectors(matrix, dimensions=self.dimensions)
        centroid_matrix = np.asarray(centroids, dtype=np.float32)
        scores = matrix @ centroid_matrix.T
        if not np.isfinite(scores).all():
            raise ValueError("pinned semantic scores are invalid")
        return PinnedCentroidSimilarityResult(
            scores={
                note_id: float(scores[index].max())
                for index, note_id in enumerate(ordered_note_ids)
            },
            unavailable_note_ids=unavailable_note_ids,
        )

    def _pinned_snapshot(self, expected_generation: str) -> SemanticSnapshot:
        snapshot = self.store.load(
            expected_model=self.model,
            expected_dimensions=self.dimensions,
            expected_generation=expected_generation,
        )
        if str(snapshot.manifest.generation) != expected_generation:
            raise ValueError("pinned semantic generation is no longer active")
        return snapshot

    async def _query_vectors(
        self,
        queries: Sequence[str],
    ) -> list[FloatMatrix]:
        resolved: dict[str, FloatMatrix] = {}
        missing_keys: list[str] = []
        missing_queries: list[str] = []
        for query in queries:
            key = self._query_key(query)
            cached = self._query_cache.get(key)
            if cached is not None:
                self._query_cache.move_to_end(key)
                resolved[key] = cached
            elif key not in resolved and key not in missing_keys:
                missing_keys.append(key)
                missing_queries.append(query)
        if missing_queries:
            embedded = await self.embedder.embed(
                missing_queries,
                input_type="query",
            )
            embedded = _normalized_rows(
                embedded,
                expected_rows=len(missing_queries),
                dimensions=self.dimensions,
            )
            for row, key in enumerate(missing_keys):
                vector = embedded[row].copy()
                vector.flags.writeable = False
                resolved[key] = vector
                self._query_cache[key] = vector
                self._query_cache.move_to_end(key)
                while len(self._query_cache) > self.query_cache_size:
                    self._query_cache.popitem(last=False)
        vectors: list[FloatMatrix] = []
        for query in queries:
            key = self._query_key(query)
            vector = resolved.get(key)
            if vector is None:
                vector = self._query_cache[key]
            vectors.append(vector)
        return vectors

    def _query_key(self, query: str) -> str:
        payload = f"{self.model}\0{self.dimensions}\0query\0{query}"
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def _validated_records(
        records: Sequence[DocumentRecord],
    ) -> list[DocumentRecord]:
        if len({record.note_id for record in records}) != len(records):
            raise ValueError("semantic note IDs must be unique")
        ordered = sorted(records, key=lambda record: record.note_id)
        for record in ordered:
            if record.note_id <= 0 or not record.text.strip():
                raise ValueError("semantic records are invalid")
            if record.content_hash != content_hash(record.text):
                raise ValueError(f"semantic content hash is invalid for note {record.note_id}")
        return ordered


def normalize_semantic_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", normalized).strip()


def content_hash(text: str) -> str:
    payload = f"anki-note-v1\0{normalize_semantic_text(text)}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _normalized_rows(
    value: FloatMatrix,
    *,
    expected_rows: int,
    dimensions: int,
) -> FloatMatrix:
    matrix = np.asarray(value, dtype=np.float32)
    if matrix.shape != (expected_rows, dimensions) or not np.isfinite(matrix).all():
        raise ValueError("embedding rows have invalid dimensions or values")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise ValueError("embedding rows cannot contain zero vectors")
    return (matrix / norms).astype(np.float32, copy=False)


def _normalized_centroid(vectors: Sequence[FloatMatrix]) -> FloatMatrix:
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[0] < 1 or not np.isfinite(matrix).all():
        raise ValueError("semantic centroid vectors are invalid")
    norms = np.linalg.norm(matrix, axis=1)
    if np.any(norms == 0):
        raise ValueError("semantic centroid vectors cannot contain zero vectors")
    normalized = matrix / norms[:, np.newaxis]
    centroid = normalized.mean(axis=0)
    centroid_norm = float(np.linalg.norm(centroid))
    if not np.isfinite(centroid_norm) or centroid_norm == 0:
        raise ValueError("semantic centroid is invalid")
    return (centroid / centroid_norm).astype(np.float32, copy=False)


def _validate_note_vectors(matrix: FloatMatrix, *, dimensions: int) -> None:
    if matrix.ndim != 2 or matrix.shape[1] != dimensions or not np.isfinite(matrix).all():
        raise ValueError("pinned semantic vectors are invalid")
    if np.any(np.linalg.norm(matrix, axis=1) == 0):
        raise ValueError("pinned semantic vectors cannot contain zero vectors")
