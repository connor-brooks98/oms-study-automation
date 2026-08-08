import json
import os
import re
import shutil
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from oms_hub.anki.domain import SourceKind
from oms_hub.anki.semantic.domain import (
    DocumentRecord,
    EmbeddingClient,
)
from oms_hub.anki.semantic.service import (
    SemanticIndexService,
    content_hash,
)
from oms_hub.anki.semantic.store import SemanticSnapshotStore
from oms_hub.anki.sources import SourcePassage

_FTS_TOKEN = re.compile(r"[\w#]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class SourceScope:
    revision_ids: tuple[int, ...] = ()
    source_kinds: tuple[SourceKind, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceIndexGeneration:
    generation: UUID
    passage_count: int
    indexed_count: int


@dataclass(frozen=True, slots=True)
class SourceSearchHit:
    passage: SourcePassage
    score: float
    semantic_score: float | None
    lexical_score: float | None
    semantic_rank: int | None
    lexical_rank: int | None


class LectureSourceIndex:
    def __init__(
        self,
        root: Path,
        embedder: EmbeddingClient,
        *,
        model: str,
        dimensions: int,
        query_cache_size: int = 256,
    ) -> None:
        if not model.strip():
            raise ValueError("source semantic model cannot be blank")
        if dimensions < 1:
            raise ValueError("source semantic dimensions must be positive")
        if query_cache_size < 1:
            raise ValueError("source query cache size must be positive")
        self.root = root
        self.embedder = embedder
        self.model = model
        self.dimensions = dimensions
        self.query_cache_size = query_cache_size

    @property
    def generations_path(self) -> Path:
        return self.root / "generations"

    @property
    def current_path(self) -> Path:
        return self.root / "CURRENT"

    async def refresh(
        self,
        passages: Sequence[SourcePassage],
    ) -> SourceIndexGeneration:
        ordered = sorted(passages, key=lambda passage: passage.passage_id)
        if len({passage.passage_id for passage in ordered}) != len(ordered):
            raise ValueError("source passage IDs must be unique")
        indexed = [
            passage
            for passage in ordered
            if passage.text
            and passage.extraction_status != "vision_unavailable"
        ]
        semantic_ids = {
            passage.passage_id: _semantic_id(passage.passage_id)
            for passage in indexed
        }
        if len(set(semantic_ids.values())) != len(semantic_ids):
            raise ValueError("source passage semantic IDs collided")

        generation = uuid4()
        self.generations_path.mkdir(parents=True, exist_ok=True)
        temporary = self.generations_path / f".building-{generation}"
        destination = self.generations_path / str(generation)
        pointer = self.root / f".CURRENT-{generation}.tmp"
        published = False
        try:
            temporary.mkdir()
            _build_database(
                temporary / "sources.sqlite3",
                ordered,
                semantic_ids,
                generation=generation,
            )
            semantic = self._semantic_service(
                temporary / "semantic",
            )
            await semantic.refresh(
                [
                    DocumentRecord(
                        note_id=semantic_ids[passage.passage_id],
                        text=passage.text,
                        content_hash=content_hash(passage.text),
                    )
                    for passage in indexed
                ]
            )
            _validate_generation(
                temporary,
                expected_generation=generation,
                expected_passages=len(ordered),
                expected_indexed=len(indexed),
                model=self.model,
                dimensions=self.dimensions,
            )
            temporary.rename(destination)
            _fsync_directory(self.generations_path)
            _write_fsynced(pointer, f"{generation}\n")
            os.replace(pointer, self.current_path)
            _fsync_directory(self.root)
            published = True
            return SourceIndexGeneration(
                generation=generation,
                passage_count=len(ordered),
                indexed_count=len(indexed),
            )
        except Exception:
            pointer.unlink(missing_ok=True)
            if temporary.exists():
                shutil.rmtree(temporary)
            if destination.exists() and not published:
                shutil.rmtree(destination)
            raise

    async def search(
        self,
        query: str,
        source_scope: SourceScope,
        *,
        limit: int,
    ) -> list[SourceSearchHit]:
        if limit < 1:
            raise ValueError("source search limit must be positive")
        generation_path = self._current_generation_path()
        database_path = generation_path / "sources.sqlite3"
        eligible = _eligible_semantic_ids(database_path, source_scope)
        if not eligible:
            return []
        candidate_limit = max(limit * 4, limit)
        semantic_service = self._semantic_service(
            generation_path / "semantic"
        )
        semantic_hits = (
            await semantic_service.search(
                [query],
                eligible_note_ids=eligible,
                limit=candidate_limit,
            )
        )[0]
        lexical_hits = _search_fts(
            database_path,
            query,
            source_scope,
            limit=candidate_limit,
        )
        semantic_ranks = {
            hit.note_id: rank
            for rank, hit in enumerate(semantic_hits, start=1)
        }
        semantic_scores = {
            hit.note_id: hit.score for hit in semantic_hits
        }
        lexical_ranks = {
            semantic_id: rank
            for rank, (semantic_id, _) in enumerate(
                lexical_hits,
                start=1,
            )
        }
        lexical_scores = dict(lexical_hits)
        semantic_ids = set(semantic_ranks) | set(lexical_ranks)
        passages = _passages_by_semantic_id(
            database_path,
            semantic_ids,
        )
        hits = [
            SourceSearchHit(
                passage=passages[semantic_id],
                score=(
                    (
                        1.0 / (60 + semantic_ranks[semantic_id])
                        if semantic_id in semantic_ranks
                        else 0.0
                    )
                    + (
                        1.0 / (60 + lexical_ranks[semantic_id])
                        if semantic_id in lexical_ranks
                        else 0.0
                    )
                ),
                semantic_score=semantic_scores.get(semantic_id),
                lexical_score=lexical_scores.get(semantic_id),
                semantic_rank=semantic_ranks.get(semantic_id),
                lexical_rank=lexical_ranks.get(semantic_id),
            )
            for semantic_id in semantic_ids
        ]
        return sorted(
            hits,
            key=lambda hit: (-hit.score, hit.passage.passage_id),
        )[:limit]

    def get_passage(self, passage_id: str) -> SourcePassage | None:
        database_path = (
            self._current_generation_path() / "sources.sqlite3"
        )
        with closing(sqlite3.connect(database_path)) as connection, connection:
            row = connection.execute(
                f"{_PASSAGE_SELECT} WHERE passage_id = ?",
                (passage_id,),
            ).fetchone()
        return None if row is None else _row_to_passage(row)

    def current_generation(self) -> UUID:
        return UUID(
            self._current_generation_path().name
        )

    def _current_generation_path(self) -> Path:
        if not self.current_path.is_file():
            raise FileNotFoundError("source index is unavailable")
        try:
            generation = UUID(
                self.current_path.read_text(encoding="utf-8").strip()
            )
        except (OSError, ValueError) as exc:
            raise ValueError("source index pointer is invalid") from exc
        path = self.generations_path / str(generation)
        if not path.is_dir():
            raise ValueError("source index generation is unavailable")
        return path

    def _semantic_service(
        self,
        root: Path,
    ) -> SemanticIndexService:
        return SemanticIndexService(
            SemanticSnapshotStore(root),
            self.embedder,
            model=self.model,
            dimensions=self.dimensions,
            min_coverage=0.0,
            query_cache_size=self.query_cache_size,
        )


_PASSAGE_SELECT = """
SELECT passage_id, source_id, revision_id, lecture_id, artifact_id, source_kind,
       locator, text, content_hash, extraction_status, slide_number,
       start_seconds, end_seconds, summary_backrefs_json, summary_section
FROM passages
"""


def _build_database(
    path: Path,
    passages: Sequence[SourcePassage],
    semantic_ids: dict[str, int],
    *,
    generation: UUID,
) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            """
            PRAGMA journal_mode = DELETE;
            CREATE TABLE passages (
                passage_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL UNIQUE,
                semantic_id INTEGER UNIQUE,
                revision_id INTEGER NOT NULL,
                lecture_id INTEGER NOT NULL,
                artifact_id TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                locator TEXT NOT NULL,
                text TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                extraction_status TEXT NOT NULL,
                slide_number INTEGER,
                start_seconds REAL,
                end_seconds REAL,
                summary_backrefs_json TEXT NOT NULL,
                summary_section TEXT,
                indexed INTEGER NOT NULL
            );
            CREATE INDEX ix_passages_scope
                ON passages(revision_id, source_kind, semantic_id);
            CREATE VIRTUAL TABLE passages_fts
                USING fts5(semantic_id UNINDEXED, passage_id UNINDEXED, text);
            CREATE TABLE source_index_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        for passage in passages:
            semantic_id = semantic_ids.get(passage.passage_id)
            connection.execute(
                """
                INSERT INTO passages (
                    passage_id, source_id, semantic_id, revision_id, lecture_id,
                    artifact_id, source_kind, locator, text, content_hash,
                    extraction_status, slide_number, start_seconds,
                    end_seconds, summary_backrefs_json, summary_section, indexed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    passage.passage_id,
                    passage.source_id,
                    semantic_id,
                    passage.revision_id,
                    passage.lecture_id,
                    passage.artifact_id,
                    passage.source_kind.value,
                    passage.locator,
                    passage.text,
                    passage.content_hash,
                    passage.extraction_status,
                    passage.slide_number,
                    passage.start_seconds,
                    passage.end_seconds,
                    json.dumps(passage.summary_backrefs),
                    passage.summary_section,
                    int(semantic_id is not None),
                ),
            )
            if semantic_id is not None:
                connection.execute(
                    """
                    INSERT INTO passages_fts (
                        semantic_id, passage_id, text
                    ) VALUES (?, ?, ?)
                    """,
                    (semantic_id, passage.passage_id, passage.text),
                )
        metadata = {
            "generation": str(generation),
            "passage_count": str(len(passages)),
            "indexed_count": str(len(semantic_ids)),
        }
        connection.executemany(
            "INSERT INTO source_index_meta (key, value) VALUES (?, ?)",
            sorted(metadata.items()),
        )
    _fsync_file(path)


def _validate_generation(
    path: Path,
    *,
    expected_generation: UUID,
    expected_passages: int,
    expected_indexed: int,
    model: str,
    dimensions: int,
) -> None:
    with closing(
        sqlite3.connect(path / "sources.sqlite3")
    ) as connection, connection:
        count = int(
            connection.execute("SELECT COUNT(*) FROM passages").fetchone()[0]
        )
        indexed = int(
            connection.execute(
                "SELECT COUNT(*) FROM passages WHERE indexed = 1"
            ).fetchone()[0]
        )
        stored_generation = connection.execute(
            """
            SELECT value FROM source_index_meta
            WHERE key = 'generation'
            """
        ).fetchone()
    snapshot = SemanticSnapshotStore(path / "semantic").load(
        expected_model=model,
        expected_dimensions=dimensions,
    )
    if (
        count != expected_passages
        or indexed != expected_indexed
        or len(snapshot.manifest.note_ids) != indexed
        or stored_generation is None
        or stored_generation[0] != str(expected_generation)
    ):
        raise ValueError("source index generation does not reconcile")


def _eligible_semantic_ids(
    database_path: Path,
    scope: SourceScope,
) -> set[int]:
    clauses = ["indexed = 1"]
    parameters: list[object] = []
    scope_clauses, scope_parameters = _scope_sql(scope)
    clauses.extend(scope_clauses)
    parameters.extend(scope_parameters)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        rows = connection.execute(
            f"""
            SELECT semantic_id FROM passages
            WHERE {' AND '.join(clauses)}
            """,
            parameters,
        ).fetchall()
    return {int(row[0]) for row in rows}


def _search_fts(
    database_path: Path,
    query: str,
    scope: SourceScope,
    *,
    limit: int,
) -> list[tuple[int, float]]:
    safe_query = _safe_fts_query(query)
    if not safe_query:
        return []
    clauses, parameters = _scope_sql(scope, alias="passages")
    filters = f" AND {' AND '.join(clauses)}" if clauses else ""
    with closing(sqlite3.connect(database_path)) as connection, connection:
        rows = connection.execute(
            f"""
            SELECT passages_fts.semantic_id, bm25(passages_fts) AS rank
            FROM passages_fts
            JOIN passages
              ON passages.semantic_id = passages_fts.semantic_id
            WHERE passages_fts MATCH ?
            {filters}
            ORDER BY rank, passages_fts.passage_id
            LIMIT ?
            """,
            (safe_query, *parameters, limit),
        ).fetchall()
    return [
        (int(row[0]), -float(row[1]))
        for row in rows
    ]


def _scope_sql(
    scope: SourceScope,
    *,
    alias: str = "passages",
) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    parameters: list[object] = []
    revision_ids = tuple(dict.fromkeys(scope.revision_ids))
    if any(revision_id <= 0 for revision_id in revision_ids):
        raise ValueError("source revision scope is invalid")
    if revision_ids:
        placeholders = ", ".join("?" for _ in revision_ids)
        clauses.append(f"{alias}.revision_id IN ({placeholders})")
        parameters.extend(revision_ids)
    source_kinds = tuple(
        dict.fromkeys(kind.value for kind in scope.source_kinds)
    )
    if source_kinds:
        placeholders = ", ".join("?" for _ in source_kinds)
        clauses.append(f"{alias}.source_kind IN ({placeholders})")
        parameters.extend(source_kinds)
    return clauses, parameters


def _passages_by_semantic_id(
    database_path: Path,
    semantic_ids: set[int],
) -> dict[int, SourcePassage]:
    if not semantic_ids:
        return {}
    ordered_ids = sorted(semantic_ids)
    placeholders = ", ".join("?" for _ in ordered_ids)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        rows = connection.execute(
            f"""
            {_PASSAGE_SELECT}
            WHERE semantic_id IN ({placeholders})
            """,
            ordered_ids,
        ).fetchall()
        id_rows = connection.execute(
            f"""
            SELECT semantic_id, passage_id FROM passages
            WHERE semantic_id IN ({placeholders})
            """,
            ordered_ids,
        ).fetchall()
    passage_by_id = {
        passage.passage_id: passage
        for passage in (_row_to_passage(row) for row in rows)
    }
    return {
        int(semantic_id): passage_by_id[str(passage_id)]
        for semantic_id, passage_id in id_rows
    }


def _row_to_passage(row: tuple[object, ...]) -> SourcePassage:
    return SourcePassage(
        passage_id=str(row[0]),
        source_id=str(row[1]),
        revision_id=int(str(row[2])),
        lecture_id=int(str(row[3])),
        artifact_id=str(row[4]),
        source_kind=SourceKind(str(row[5])),
        locator=str(row[6]),
        text=str(row[7]),
        content_hash=str(row[8]),
        extraction_status=str(row[9]),  # type: ignore[arg-type]
        slide_number=None if row[10] is None else int(str(row[10])),
        start_seconds=(
            None if row[11] is None else float(str(row[11]))
        ),
        end_seconds=(
            None if row[12] is None else float(str(row[12]))
        ),
        summary_backrefs=tuple(str(value) for value in json.loads(str(row[13]))),
        summary_section=(None if row[14] is None else str(row[14])),  # type: ignore[arg-type]
    )


def _semantic_id(passage_id: str) -> int:
    value = int(passage_id[:15], 16)
    return value if value > 0 else 1


def _safe_fts_query(query: str) -> str:
    return " OR ".join(f'"{token}"' for token in _FTS_TOKEN.findall(query))


def _write_fsynced(path: Path, value: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
