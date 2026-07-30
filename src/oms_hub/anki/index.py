import json
import os
import shutil
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import numpy as np

from oms_hub.anki.domains import assign_domains
from oms_hub.anki.embeddings import (
    AtomicVectorStore,
    Embedder,
    FileLock,
    normalize_vectors,
)
from oms_hub.anki.normalize import MediaReference, NormalizedNote


@dataclass(frozen=True, slots=True)
class SearchHit:
    note_id: int
    score: float


class AnkiIndex:
    def __init__(self, root: Path, *, embedder: Embedder) -> None:
        self.root = root
        self.embedder = embedder

    @property
    def database_path(self) -> Path:
        return self.root / "cards.sqlite3"

    @property
    def vector_store(self) -> AtomicVectorStore:
        return AtomicVectorStore(self.root)

    def rebuild(
        self,
        notes: Sequence[NormalizedNote],
        *,
        snapshot_id: str,
        fingerprint: str,
    ) -> None:
        if not snapshot_id or len(fingerprint) != 64:
            raise ValueError("snapshot metadata is invalid")
        ordered = sorted(notes, key=lambda note: note.note_id)
        if len({note.note_id for note in ordered}) != len(ordered):
            raise ValueError("index notes must have unique IDs")
        self.root.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.root.parent / f".{self.root.name}.building-{uuid4().hex}"
        lock = self.root.parent / f".{self.root.name}.lock"
        try:
            temporary.mkdir()
            vectors = normalize_vectors(
                self.embedder.embed([note.text for note in ordered])
            )
            if vectors.shape[0] != len(ordered):
                raise ValueError("embedding row count does not match note count")
            _build_database(
                temporary / "cards.sqlite3",
                ordered,
                snapshot_id=snapshot_id,
                fingerprint=fingerprint,
                embedding_model=self.embedder.model_name,
            )
            AtomicVectorStore(temporary).replace(
                [note.note_id for note in ordered],
                vectors,
            )
            _validate_build(temporary, len(ordered), vectors.shape[1])
            with FileLock(lock):
                _replace_directory(temporary, self.root)
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise

    def apply_delta(
        self,
        upserts: Sequence[NormalizedNote],
        *,
        deleted_note_ids: Sequence[int],
        snapshot_id: str,
        fingerprint: str,
    ) -> None:
        notes = {note.note_id: note for note in self._all_notes()}
        for note_id in deleted_note_ids:
            notes.pop(note_id, None)
        for note in upserts:
            notes[note.note_id] = note
        self.rebuild(
            list(notes.values()),
            snapshot_id=snapshot_id,
            fingerprint=fingerprint,
        )

    def snapshot_id(self) -> str | None:
        if not self.database_path.exists():
            return None
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            row = connection.execute(
                "SELECT value FROM index_meta WHERE key = 'snapshot_id'"
            ).fetchone()
        return None if row is None else str(row[0])

    def get_note(self, note_id: int) -> NormalizedNote | None:
        if not self.database_path.exists():
            return None
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            row = connection.execute(
                """
                SELECT note_id, model_name, text, extra, raw_fields_json,
                       tags_json, card_ids_json, token_signature, content_sha256
                FROM notes WHERE note_id = ?
                """,
                (note_id,),
            ).fetchone()
            if row is None:
                return None
            media_rows = connection.execute(
                """
                SELECT field_name, filename, media_type, source_order
                FROM note_media WHERE note_id = ?
                ORDER BY field_order, source_order
                """,
                (note_id,),
            ).fetchall()
        return _row_to_note(row, media_rows)

    def search_tag(self, prefix: str) -> list[int]:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            rows = connection.execute(
                """
                SELECT DISTINCT note_id FROM note_tags
                WHERE tag_prefix = ? ORDER BY note_id
                """,
                (prefix.casefold(),),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def search_fts(self, query: str, *, limit: int = 50) -> list[SearchHit]:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            rows = connection.execute(
                """
                SELECT note_id, bm25(notes_fts) AS rank
                FROM notes_fts
                WHERE notes_fts MATCH ?
                ORDER BY rank, note_id
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        return [SearchHit(note_id=int(row[0]), score=-float(row[1])) for row in rows]

    def search_semantic(
        self,
        query: str,
        *,
        domain: str | None = None,
        limit: int = 50,
    ) -> list[SearchHit]:
        query_vector = normalize_vectors(self.embedder.embed([query]))[0]
        note_ids, vectors = self.vector_store.load()
        allowed: set[int] | None = None
        if domain is not None:
            with closing(sqlite3.connect(self.database_path)) as connection, connection:
                allowed = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT note_id FROM note_domains WHERE domain = ?",
                        (domain,),
                    )
                }
        scored = [
            SearchHit(note_id=note_id, score=float(np.dot(vectors[row], query_vector)))
            for row, note_id in enumerate(note_ids)
            if allowed is None or note_id in allowed
        ]
        return sorted(scored, key=lambda hit: (-hit.score, hit.note_id))[:limit]

    def _all_notes(self) -> list[NormalizedNote]:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            note_ids = [
                int(row[0])
                for row in connection.execute("SELECT note_id FROM notes ORDER BY note_id")
            ]
        return [
            note
            for note_id in note_ids
            if (note := self.get_note(note_id)) is not None
        ]


def _build_database(
    path: Path,
    notes: Sequence[NormalizedNote],
    *,
    snapshot_id: str,
    fingerprint: str,
    embedding_model: str,
) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            """
            PRAGMA journal_mode = DELETE;
            CREATE TABLE notes (
                note_id INTEGER PRIMARY KEY,
                model_name TEXT NOT NULL,
                text TEXT NOT NULL,
                extra TEXT NOT NULL,
                raw_fields_json TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                card_ids_json TEXT NOT NULL,
                source_count INTEGER NOT NULL,
                token_signature TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                embedding_row INTEGER NOT NULL UNIQUE
            );
            CREATE TABLE note_tags (
                note_id INTEGER NOT NULL,
                tag TEXT NOT NULL,
                tag_prefix TEXT NOT NULL,
                PRIMARY KEY (note_id, tag, tag_prefix)
            );
            CREATE INDEX ix_note_tags_prefix ON note_tags(tag_prefix, note_id);
            CREATE TABLE note_domains (
                note_id INTEGER NOT NULL,
                domain TEXT NOT NULL,
                PRIMARY KEY (note_id, domain)
            );
            CREATE INDEX ix_note_domains_domain ON note_domains(domain, note_id);
            CREATE TABLE note_media (
                note_id INTEGER NOT NULL,
                field_name TEXT NOT NULL,
                filename TEXT NOT NULL,
                media_type TEXT NOT NULL,
                field_order INTEGER NOT NULL,
                source_order INTEGER NOT NULL,
                PRIMARY KEY (note_id, field_name, filename, source_order)
            );
            CREATE VIRTUAL TABLE notes_fts USING fts5(note_id UNINDEXED, text, extra);
            CREATE TABLE index_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        for row_index, note in enumerate(notes):
            connection.execute(
                """
                INSERT INTO notes (
                    note_id, model_name, text, extra, raw_fields_json, tags_json,
                    card_ids_json, source_count, token_signature, content_sha256,
                    embedding_row
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    note.note_id,
                    note.model_name,
                    note.text,
                    note.extra,
                    _json(note.raw_fields),
                    _json(note.tags),
                    _json(note.card_ids),
                    len(note.tags),
                    note.token_signature,
                    note.content_sha256,
                    row_index,
                ),
            )
            connection.execute(
                "INSERT INTO notes_fts (note_id, text, extra) VALUES (?, ?, ?)",
                (note.note_id, note.text, note.extra),
            )
            for tag in note.tags:
                for prefix in _tag_prefixes(tag):
                    connection.execute(
                        """
                        INSERT INTO note_tags (note_id, tag, tag_prefix)
                        VALUES (?, ?, ?)
                        """,
                        (note.note_id, tag, prefix.casefold()),
                    )
            for domain in assign_domains(note.tags):
                connection.execute(
                    "INSERT INTO note_domains (note_id, domain) VALUES (?, ?)",
                    (note.note_id, domain),
                )
            field_order = {name: order for order, name in enumerate(note.raw_fields)}
            for media in note.media:
                connection.execute(
                    """
                    INSERT INTO note_media (
                        note_id, field_name, filename, media_type, field_order, source_order
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        note.note_id,
                        media.field_name,
                        media.filename,
                        media.media_type,
                        field_order.get(media.field_name, len(field_order)),
                        media.source_order,
                    ),
                )
        metadata = {
            "snapshot_id": snapshot_id,
            "fingerprint": fingerprint,
            "note_count": str(len(notes)),
            "built_at": datetime.now(UTC).isoformat(),
            "embedding_model": embedding_model,
        }
        connection.executemany(
            "INSERT INTO index_meta (key, value) VALUES (?, ?)",
            sorted(metadata.items()),
        )
    _fsync_file(path)


def _row_to_note(
    row: tuple[object, ...],
    media_rows: Sequence[tuple[object, ...]],
) -> NormalizedNote:
    return NormalizedNote(
        note_id=int(str(row[0])),
        model_name=str(row[1]),
        text=str(row[2]),
        extra=str(row[3]),
        raw_fields=dict(json.loads(str(row[4]))),
        tags=tuple(json.loads(str(row[5]))),
        card_ids=tuple(json.loads(str(row[6]))),
        token_signature=str(row[7]),
        content_sha256=str(row[8]),
        media=tuple(
            MediaReference(
                field_name=str(media[0]),
                filename=str(media[1]),
                media_type=str(media[2]),  # type: ignore[arg-type]
                source_order=int(str(media[3])),
            )
            for media in media_rows
        ),
    )


def _tag_prefixes(tag: str) -> tuple[str, ...]:
    parts = tag.split("::")
    return tuple("::".join(parts[:index]) for index in range(1, len(parts) + 1))


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _validate_build(root: Path, expected_count: int, expected_dimensions: int) -> None:
    with closing(
        sqlite3.connect(root / "cards.sqlite3")
    ) as connection, connection:
        count = int(connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0])
    note_ids, vectors = AtomicVectorStore(root).load()
    if count != expected_count or len(note_ids) != count:
        raise ValueError("built index note count does not reconcile")
    if vectors.ndim != 2 or vectors.shape != (count, expected_dimensions):
        raise ValueError("built index vector dimensions do not reconcile")


def _replace_directory(source: Path, target: Path) -> None:
    backup = target.with_name(f".{target.name}.previous")
    if backup.exists():
        shutil.rmtree(backup)
    if target.exists():
        target.rename(backup)
    try:
        source.rename(target)
        _fsync_directory(target.parent)
    except Exception:
        if backup.exists() and not target.exists():
            backup.rename(target)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
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
