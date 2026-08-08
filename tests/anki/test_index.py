import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest

from oms_hub.anki.index import AnkiIndex
from oms_hub.anki.normalize import MediaReference, NormalizedNote


class FixedEmbedder:
    model_name = "fixed-v1"

    def __init__(self) -> None:
        self.values = {
            "warfarin anticoagulant": [1.0, 0.0, 0.0],
            "iron anemia": [0.0, 1.0, 0.0],
            "staph bacteria": [0.0, 0.0, 1.0],
            "hemostasis": [1.0, 0.0, 0.0],
            "blood": [0.7, 0.7, 0.0],
            "infection": [0.0, 0.0, 1.0],
            "explode": [1.0, 1.0, 1.0],
        }

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if "explode" in texts:
            raise RuntimeError("injected embedding failure")
        return np.asarray([self.values[text] for text in texts], dtype=np.float32)


def _note(
    note_id: int,
    text: str,
    *,
    tags: tuple[str, ...],
    media: tuple[MediaReference, ...] = (),
    deck_names: tuple[str, ...] = (),
) -> NormalizedNote:
    return NormalizedNote(
        note_id=note_id,
        model_name="AnKingOverhaul",
        text=text,
        extra=f"extra {text}",
        raw_fields={"Text": text, "Extra": f"extra {text}"},
        tags=tags,
        card_ids=(note_id + 1_000,),
        media=media,
        token_signature=" ".join(sorted(text.split())),
        content_sha256=f"{note_id:064x}",
        deck_names=deck_names,
    )


def _initial_notes() -> list[NormalizedNote]:
    return [
        _note(
            101,
            "warfarin anticoagulant",
            tags=("#AK_Step1_v12::Pharmacology::Hematology",),
            media=(MediaReference("Extra", "warfarin.png", "image", 0),),
        ),
        _note(102, "iron anemia", tags=("#Pathoma::Hematology::Anemia",)),
        _note(103, "staph bacteria", tags=("#Sketchy::Micro::Bacteria",)),
    ]


def test_rebuild_populates_hybrid_index_and_queries(tmp_path: Path) -> None:
    index = AnkiIndex(tmp_path / "index", embedder=FixedEmbedder())
    index.rebuild(_initial_notes(), snapshot_id="snapshot-1", fingerprint="a" * 64)

    with closing(
        sqlite3.connect(tmp_path / "index" / "cards.sqlite3")
    ) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
    assert {
        "notes",
        "note_tags",
        "note_domains",
        "note_media",
        "notes_fts",
        "index_meta",
    } <= tables
    assert index.snapshot_id() == "snapshot-1"
    assert index.get_note(101).text == "warfarin anticoagulant"  # type: ignore[union-attr]
    assert index.search_tag("#AK_Step1_v12::Pharmacology") == [101]
    assert [hit.note_id for hit in index.search_fts("anemia")] == [102]
    assert [
        hit.note_id for hit in index.search_semantic("hemostasis", domain="Heme")
    ] == [101, 102]
    assert [hit.note_id for hit in index.search_semantic("infection")] == [103, 101, 102]


def test_list_deck_names_returns_distinct_case_insensitive_order(tmp_path: Path) -> None:
    index = AnkiIndex(tmp_path / "index", embedder=FixedEmbedder())
    notes = [
        _note(201, "iron anemia", tags=(), deck_names=("Sketchy Pepper", "AnKing Step Deck")),
        _note(202, "staph bacteria", tags=(), deck_names=("Zanki::Micro", "AnKing Step Deck")),
    ]
    index.rebuild(notes, snapshot_id="snapshot-decks", fingerprint="d" * 64)
    assert index.list_deck_names() == (
        "AnKing Step Deck",
        "Sketchy Pepper",
        "Zanki::Micro",
    )


def test_delta_updates_adds_and_deletes_with_compact_vectors(tmp_path: Path) -> None:
    index = AnkiIndex(tmp_path / "index", embedder=FixedEmbedder())
    index.rebuild(_initial_notes(), snapshot_id="snapshot-1", fingerprint="a" * 64)
    updated = _note(
        102,
        "warfarin anticoagulant",
        tags=("#AK_Step1_v12::Pharmacology::Hematology",),
    )
    added = _note(104, "staph bacteria", tags=("#Sketchy::Micro::Bacteria",))

    index.apply_delta(
        [updated, added],
        deleted_note_ids=[101, 103],
        snapshot_id="snapshot-2",
        fingerprint="b" * 64,
    )

    assert index.snapshot_id() == "snapshot-2"
    assert index.get_note(101) is None
    assert index.get_note(102).text == "warfarin anticoagulant"  # type: ignore[union-attr]
    assert index.get_note(104) is not None
    note_ids, vectors = index.vector_store.load()
    assert note_ids == [102, 104]
    assert vectors.shape == (2, 3)


def test_failed_rebuild_keeps_prior_snapshot_usable(tmp_path: Path) -> None:
    index = AnkiIndex(tmp_path / "index", embedder=FixedEmbedder())
    index.rebuild(_initial_notes(), snapshot_id="snapshot-1", fingerprint="a" * 64)

    with pytest.raises(RuntimeError, match="injected"):
        index.rebuild(
            [_note(999, "explode", tags=("#Pathoma::Hematology",))],
            snapshot_id="snapshot-bad",
            fingerprint="f" * 64,
        )

    assert index.snapshot_id() == "snapshot-1"
    assert index.get_note(101) is not None
    assert not list(tmp_path.glob(".index.building-*"))
