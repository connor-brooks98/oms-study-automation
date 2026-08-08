import json
import os
import re
import shutil
import sqlite3
from collections.abc import Collection, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import numpy as np

from oms_hub.anki.contracts import SnapshotNote
from oms_hub.anki.domains import assign_domains
from oms_hub.anki.embeddings import (
    AtomicVectorStore,
    Embedder,
    FileLock,
    normalize_vectors,
)
from oms_hub.anki.normalize import (
    MediaReference,
    NormalizedNote,
    normalize_snapshot_note,
    semantic_text,
)
from oms_hub.anki.semantic.domain import DocumentRecord
from oms_hub.anki.semantic.service import content_hash

_FTS_TOKEN = re.compile(r"[\w#]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class SearchHit:
    note_id: int
    score: float


@dataclass(frozen=True, slots=True)
class SemanticAlignment:
    eligible_count: int
    compatible_count: int
    coverage: float
    missing_or_stale_note_ids: tuple[int, ...]
    unexpected_note_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CompanionFilters:
    deck_allowlist: tuple[str, ...] = ()
    tag_allowlist: tuple[str, ...] = ()
    excluded_tag_prefixes: tuple[str, ...] = ()


_NO_FILTERS = CompanionFilters()


class LocalAnkiReader(Protocol):
    async def find_notes(self, query: str) -> list[int]: ...

    async def notes_info(
        self,
        note_ids: Sequence[int],
    ) -> list[dict[str, Any]]: ...

    async def find_cards(self, query: str) -> list[int]: ...

    async def cards_info(
        self,
        card_ids: Sequence[int],
    ) -> list[dict[str, Any]]: ...


class SemanticRefresher(Protocol):
    async def refresh(
        self,
        records: Sequence[DocumentRecord],
        *,
        expected_note_ids: Collection[int] | None = None,
    ) -> object: ...


class AnkiIndex:
    def __init__(
        self,
        root: Path,
        *,
        embedder: Embedder | None = None,
    ) -> None:
        self.root = root
        self.embedder = embedder

    @property
    def database_path(self) -> Path:
        return self.root / "cards.sqlite3"

    @property
    def vector_store(self) -> AtomicVectorStore:
        return AtomicVectorStore(self.root)

    def list_deck_names(self) -> tuple[str, ...]:
        if not self.database_path.is_file():
            return ()
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                "SELECT DISTINCT deck_name FROM note_decks "
                "ORDER BY deck_name COLLATE NOCASE"
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def rebuild(
        self,
        notes: Sequence[NormalizedNote],
        *,
        snapshot_id: str,
        fingerprint: str,
    ) -> None:
        embedder = self._require_embedder()
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
                embedder.embed([note.text for note in ordered])
            )
            if vectors.shape[0] != len(ordered):
                raise ValueError("embedding row count does not match note count")
            _build_database(
                temporary / "cards.sqlite3",
                ordered,
                snapshot_id=snapshot_id,
                fingerprint=fingerprint,
                embedding_model=embedder.model_name,
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

    def rebuild_companion(
        self,
        notes: Sequence[NormalizedNote],
        *,
        snapshot_id: str,
        fingerprint: str,
        embedding_model: str = "voyage-4-large",
    ) -> None:
        """Atomically publish note metadata without coupling it to vectors."""
        if not snapshot_id or len(fingerprint) != 64:
            raise ValueError("snapshot metadata is invalid")
        ordered = sorted(notes, key=lambda note: note.note_id)
        if len({note.note_id for note in ordered}) != len(ordered):
            raise ValueError("index notes must have unique IDs")
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / f".cards.sqlite3.building-{uuid4().hex}"
        lock = self.root / ".cards.sqlite3.lock"
        try:
            _build_database(
                temporary,
                ordered,
                snapshot_id=snapshot_id,
                fingerprint=fingerprint,
                embedding_model=embedding_model,
            )
            _validate_database(temporary, len(ordered))
            with FileLock(lock):
                os.replace(temporary, self.database_path)
                _fsync_directory(self.root)
        finally:
            temporary.unlink(missing_ok=True)

    async def refresh_from_anki(
        self,
        gateway: LocalAnkiReader,
        *,
        snapshot_id: str,
        fingerprint: str,
        query: str = "",
        semantic_refresher: SemanticRefresher | None = None,
        metadata_batch_size: int = 500,
    ) -> list[NormalizedNote]:
        """Read current note/card metadata from local Anki and publish it."""
        if metadata_batch_size < 1:
            raise ValueError("Anki metadata batch size must be positive")
        note_ids = await gateway.find_notes(query)
        found_card_ids = await gateway.find_cards(query)
        if len(set(note_ids)) != len(note_ids):
            raise ValueError("Anki returned duplicate note IDs")
        if len(set(found_card_ids)) != len(found_card_ids):
            raise ValueError("Anki returned duplicate card IDs")
        raw_notes: list[dict[str, Any]] = []
        for batch in _integer_batches(note_ids, metadata_batch_size):
            raw_notes.extend(await gateway.notes_info(batch))
        if len(raw_notes) != len(note_ids):
            raise ValueError("Anki note metadata count does not reconcile")

        card_ids_by_note: dict[int, tuple[int, ...]] = {}
        parsed_notes: list[
            tuple[int, str, dict[str, str], tuple[str, ...], int]
        ] = []
        all_card_ids: list[int] = []
        for raw_note in raw_notes:
            note_id = _positive_int(raw_note.get("noteId"), "note ID")
            model_name = _nonempty_string(
                raw_note.get("modelName"),
                "model name",
            )
            fields = _anki_fields(raw_note.get("fields"))
            tags = _string_tuple(raw_note.get("tags"), "tags")
            modified_at = _positive_int(
                raw_note.get("mod"),
                "note modification time",
            )
            card_ids = tuple(
                _positive_int(value, "card ID")
                for value in _sequence(raw_note.get("cards"), "cards")
            )
            card_ids_by_note[note_id] = card_ids
            all_card_ids.extend(card_ids)
            parsed_notes.append(
                (note_id, model_name, fields, tags, modified_at)
            )

        deck_names_by_note: dict[int, set[str]] = {
            note_id: set() for note_id, *_ in parsed_notes
        }
        all_card_ids = list(dict.fromkeys((*found_card_ids, *all_card_ids)))
        if all_card_ids:
            raw_cards: list[dict[str, Any]] = []
            for batch in _integer_batches(
                all_card_ids,
                metadata_batch_size,
            ):
                raw_cards.extend(await gateway.cards_info(batch))
            if len(raw_cards) != len(all_card_ids):
                raise ValueError("Anki card metadata count does not reconcile")
            for raw_card in raw_cards:
                card_id = _positive_int(raw_card.get("cardId"), "card ID")
                note_id = _positive_int(raw_card.get("note"), "card note ID")
                deck_name = _nonempty_string(
                    raw_card.get("deckName"),
                    "deck name",
                )
                if card_id not in card_ids_by_note.get(note_id, ()):
                    raise ValueError("Anki card metadata is inconsistent")
                if note_id not in deck_names_by_note:
                    raise ValueError("Anki card references an unknown note")
                deck_names_by_note[note_id].add(deck_name)

        normalized = [
            replace(
                normalize_snapshot_note(
                    SnapshotNote(
                        note_id=note_id,
                        model_name=model_name,
                        fields=fields,
                        tags=tags,
                        card_ids=card_ids_by_note[note_id],
                        media=(),
                        content_sha256="0" * 64,
                    )
                ),
                deck_names=tuple(
                    sorted(deck_names_by_note[note_id], key=str.casefold)
                ),
                modified_at=modified_at,
            )
            for note_id, model_name, fields, tags, modified_at in parsed_notes
        ]
        self.rebuild_companion(
            normalized,
            snapshot_id=snapshot_id,
            fingerprint=fingerprint,
        )
        if semantic_refresher is not None:
            semantic_records = [
                (note, semantic_text(note))
                for note in normalized
            ]
            semantic_records = [
                (note, text)
                for note, text in semantic_records
                if text.strip()
            ]
            await semantic_refresher.refresh(
                [
                    DocumentRecord(
                        note_id=note.note_id,
                        text=text,
                        content_hash=content_hash(text),
                    )
                    for note, text in semantic_records
                ],
                expected_note_ids={
                    note.note_id for note, _ in semantic_records
                },
            )
        return normalized

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
                       tags_json, card_ids_json, token_signature,
                       content_sha256, modified_at
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
            deck_rows = connection.execute(
                """
                SELECT deck_name FROM note_decks
                WHERE note_id = ? ORDER BY deck_name COLLATE NOCASE
                """,
                (note_id,),
            ).fetchall()
            source_rows = connection.execute(
                """
                SELECT source_family FROM note_source_families
                WHERE note_id = ? ORDER BY source_family
                """,
                (note_id,),
            ).fetchall()
        return _row_to_note(row, media_rows, deck_rows, source_rows)

    def list_notes(self) -> tuple[NormalizedNote, ...]:
        """Return the immutable companion snapshot in stable note-ID order."""
        return tuple(self._all_notes())

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

    def eligible_note_ids(
        self,
        filters: CompanionFilters,
    ) -> set[int]:
        clauses, parameters = _filter_sql(filters, note_alias="notes")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            rows = connection.execute(
                f"SELECT notes.note_id FROM notes{where}",
                parameters,
            ).fetchall()
        return {int(row[0]) for row in rows}

    def semantic_alignment(
        self,
        *,
        note_ids: Sequence[int],
        content_hashes: Sequence[str],
    ) -> SemanticAlignment:
        if len(note_ids) != len(content_hashes):
            raise ValueError(
                "semantic IDs and content hashes do not reconcile"
            )
        if len(set(note_ids)) != len(note_ids):
            raise ValueError("semantic note IDs must be unique")
        semantic = dict(zip(note_ids, content_hashes, strict=True))
        with closing(
            sqlite3.connect(self.database_path)
        ) as connection, connection:
            rows = connection.execute(
                """
                SELECT note_id,
                       CASE WHEN TRIM(text) <> '' THEN text ELSE extra END
                FROM notes
                WHERE TRIM(text) <> '' OR TRIM(extra) <> ''
                ORDER BY note_id
                """
            ).fetchall()
        companion_ids = {int(row[0]) for row in rows}
        compatible = {
            int(row[0])
            for row in rows
            if semantic.get(int(row[0])) == content_hash(str(row[1]))
        }
        missing_or_stale = tuple(sorted(companion_ids - compatible))
        unexpected = tuple(sorted(set(semantic) - companion_ids))
        return SemanticAlignment(
            eligible_count=len(companion_ids),
            compatible_count=len(compatible),
            coverage=(
                len(compatible) / len(companion_ids)
                if companion_ids
                else 1.0
            ),
            missing_or_stale_note_ids=missing_or_stale,
            unexpected_note_ids=unexpected,
        )

    def search_fts(
        self,
        query: str,
        *,
        filters: CompanionFilters = _NO_FILTERS,
        limit: int = 50,
    ) -> list[SearchHit]:
        if limit <= 0:
            return []
        safe_query = _safe_fts_query(query)
        if not safe_query:
            return []
        clauses, filter_parameters = _filter_sql(
            filters,
            note_alias="notes",
        )
        where_filters = (
            f" AND {' AND '.join(clauses)}" if clauses else ""
        )
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            rows = connection.execute(
                f"""
                SELECT notes_fts.note_id, bm25(notes_fts) AS rank
                FROM notes_fts
                JOIN notes ON notes.note_id = notes_fts.note_id
                WHERE notes_fts MATCH ?
                {where_filters}
                ORDER BY rank, notes_fts.note_id
                LIMIT ?
                """,
                (safe_query, *filter_parameters, limit),
            ).fetchall()
        return [SearchHit(note_id=int(row[0]), score=-float(row[1])) for row in rows]

    def search_semantic(
        self,
        query: str,
        *,
        domain: str | None = None,
        limit: int = 50,
    ) -> list[SearchHit]:
        embedder = self._require_embedder()
        query_vector = normalize_vectors(embedder.embed([query]))[0]
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

    def source_families(self, note_id: int) -> tuple[str, ...]:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            rows = connection.execute(
                """
                SELECT source_family FROM note_source_families
                WHERE note_id = ? ORDER BY source_family
                """,
                (note_id,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def source_count(self, note_id: int) -> int:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            row = connection.execute(
                "SELECT source_count FROM notes WHERE note_id = ?",
                (note_id,),
            ).fetchone()
        if row is None:
            raise KeyError(note_id)
        return int(row[0])

    def _require_embedder(self) -> Embedder:
        if self.embedder is None:
            raise RuntimeError(
                "the legacy semantic path requires an embedder"
            )
        return self.embedder


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
                modified_at INTEGER,
                embedding_row INTEGER NOT NULL UNIQUE
            );
            CREATE TABLE note_tags (
                note_id INTEGER NOT NULL,
                tag TEXT NOT NULL,
                tag_prefix TEXT NOT NULL,
                PRIMARY KEY (note_id, tag, tag_prefix)
            );
            CREATE INDEX ix_note_tags_prefix ON note_tags(tag_prefix, note_id);
            CREATE TABLE note_decks (
                note_id INTEGER NOT NULL,
                deck_name TEXT NOT NULL,
                PRIMARY KEY (note_id, deck_name)
            );
            CREATE INDEX ix_note_decks_name ON note_decks(deck_name, note_id);
            CREATE TABLE note_source_families (
                note_id INTEGER NOT NULL,
                source_family TEXT NOT NULL,
                PRIMARY KEY (note_id, source_family)
            );
            CREATE INDEX ix_note_source_families_family
                ON note_source_families(source_family, note_id);
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
                    modified_at, embedding_row
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    note.note_id,
                    note.model_name,
                    note.text,
                    note.extra,
                    _json(note.raw_fields),
                    _json(note.tags),
                    _json(note.card_ids),
                    len(set(note.source_families)),
                    note.token_signature,
                    note.content_sha256,
                    note.modified_at,
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
            for deck_name in sorted(set(note.deck_names), key=str.casefold):
                connection.execute(
                    """
                    INSERT INTO note_decks (note_id, deck_name)
                    VALUES (?, ?)
                    """,
                    (note.note_id, deck_name),
                )
            for source_family in sorted(set(note.source_families)):
                connection.execute(
                    """
                    INSERT INTO note_source_families (note_id, source_family)
                    VALUES (?, ?)
                    """,
                    (note.note_id, source_family),
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
    deck_rows: Sequence[tuple[object, ...]],
    source_rows: Sequence[tuple[object, ...]],
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
        deck_names=tuple(str(deck[0]) for deck in deck_rows),
        source_families=tuple(str(source[0]) for source in source_rows),
        modified_at=None if row[9] is None else int(str(row[9])),
    )


def _tag_prefixes(tag: str) -> tuple[str, ...]:
    parts = tag.split("::")
    return tuple("::".join(parts[:index]) for index in range(1, len(parts) + 1))


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _safe_fts_query(query: str) -> str:
    return " OR ".join(f'"{token}"' for token in _FTS_TOKEN.findall(query))


def _filter_sql(
    filters: CompanionFilters,
    *,
    note_alias: str,
) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    parameters: list[object] = []
    decks = tuple(
        dict.fromkeys(
            value.strip()
            for value in filters.deck_allowlist
            if value.strip()
        )
    )
    if decks:
        matches = []
        for deck in decks:
            matches.append(
                "(decks.deck_name = ? COLLATE NOCASE "
                "OR decks.deck_name LIKE ? ESCAPE '\\' COLLATE NOCASE)"
            )
            parameters.extend((deck, f"{_escape_like(deck)}::%"))
        clauses.append(
            "EXISTS (SELECT 1 FROM note_decks AS decks "
            f"WHERE decks.note_id = {note_alias}.note_id "
            f"AND ({' OR '.join(matches)}))"
        )

    allowed_tags = tuple(
        dict.fromkeys(
            value.strip().casefold()
            for value in filters.tag_allowlist
            if value.strip()
        )
    )
    if allowed_tags:
        placeholders = ", ".join("?" for _ in allowed_tags)
        clauses.append(
            "EXISTS (SELECT 1 FROM note_tags AS allowed_tags "
            f"WHERE allowed_tags.note_id = {note_alias}.note_id "
            f"AND allowed_tags.tag_prefix IN ({placeholders}))"
        )
        parameters.extend(allowed_tags)

    excluded_tags = tuple(
        dict.fromkeys(
            value.strip().casefold()
            for value in filters.excluded_tag_prefixes
            if value.strip()
        )
    )
    if excluded_tags:
        placeholders = ", ".join("?" for _ in excluded_tags)
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM note_tags AS excluded_tags "
            f"WHERE excluded_tags.note_id = {note_alias}.note_id "
            f"AND excluded_tags.tag_prefix IN ({placeholders}))"
        )
        parameters.extend(excluded_tags)
    return clauses, parameters


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _sequence(value: object, description: str) -> Sequence[object]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"Anki returned invalid {description}")
    return value


def _positive_int(value: object, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Anki returned invalid {description}")
    return value


def _nonempty_string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Anki returned invalid {description}")
    return value.strip()


def _string_tuple(value: object, description: str) -> tuple[str, ...]:
    values = _sequence(value, description)
    if not all(isinstance(item, str) and item.strip() for item in values):
        raise ValueError(f"Anki returned invalid {description}")
    return tuple(str(item).strip() for item in values)


def _anki_fields(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise TypeError("Anki returned invalid fields")
    fields: dict[str, str] = {}
    for name, payload in value.items():
        if (
            not isinstance(name, str)
            or not isinstance(payload, dict)
            or not isinstance(payload.get("value"), str)
        ):
            raise TypeError("Anki returned invalid fields")
        fields[name] = payload["value"]
    return fields


def _integer_batches(
    values: Sequence[int],
    batch_size: int,
) -> list[Sequence[int]]:
    return [
        values[offset : offset + batch_size]
        for offset in range(0, len(values), batch_size)
    ]


def _validate_database(path: Path, expected_count: int) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        count = int(
            connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        )
        metadata_count_row = connection.execute(
            "SELECT value FROM index_meta WHERE key = 'note_count'"
        ).fetchone()
    if (
        count != expected_count
        or metadata_count_row is None
        or int(metadata_count_row[0]) != count
    ):
        raise ValueError("built companion index note count does not reconcile")


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
