import asyncio
import errno
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any

import pytest

from oms_hub.anki.index import AnkiIndex, CompanionFilters
from oms_hub.anki.normalize import (
    NormalizedNote,
    trusted_source_families,
)
from oms_hub.anki.semantic.domain import DocumentRecord
from oms_hub.anki.semantic.service import content_hash


def _note(
    note_id: int,
    text: str,
    *,
    tags: tuple[str, ...] = (),
    decks: tuple[str, ...] = (),
    extra: str | None = None,
) -> NormalizedNote:
    resolved_extra = f"extra {text}" if extra is None else extra
    return NormalizedNote(
        note_id=note_id,
        model_name="AnKingOverhaul",
        text=text,
        extra=resolved_extra,
        raw_fields={"Text": text, "Extra": resolved_extra},
        tags=tags,
        card_ids=(note_id + 1_000,),
        media=(),
        token_signature=" ".join(sorted(text.casefold().split())),
        content_sha256=f"{note_id:064x}",
        deck_names=decks,
        source_families=trusted_source_families(tags),
    )


def _built_index(tmp_path: Path) -> AnkiIndex:
    index = AnkiIndex(tmp_path / "companion")
    index.rebuild_companion(
        [
            _note(
                10,
                "iron deficiency anemia",
                tags=(
                    "#Pathoma::Hematology::Anemia",
                    "#Pathoma::Hematology",
                    "OMS::Heme::Lecture_3",
                ),
                decks=("AnKing Step Deck::Heme", "Filtered::Exam 1"),
            ),
            _note(
                20,
                "warfarin anticoagulation",
                tags=(
                    "#Sketchy::Pharm::Warfarin",
                    "OMS::Heme::Lecture_4",
                ),
                decks=("AnKing Step Deck::Heme",),
            ),
            _note(
                30,
                "staphylococcus aureus",
                tags=("#Sketchy::Micro::Bacteria", "suspended::local"),
                decks=("AnKing Step Deck::Micro",),
            ),
        ],
        snapshot_id="companion-1",
        fingerprint="a" * 64,
    )
    return index


def test_companion_index_preserves_multi_deck_note_membership(
    tmp_path: Path,
) -> None:
    index = _built_index(tmp_path)

    assert index.eligible_note_ids(
        CompanionFilters(deck_allowlist=("Filtered::Exam 1",))
    ) == {10}
    assert index.eligible_note_ids(
        CompanionFilters(deck_allowlist=("AnKing Step Deck",))
    ) == {10, 20, 30}
    assert index.get_note(10).deck_names == (  # type: ignore[union-attr]
        "AnKing Step Deck::Heme",
        "Filtered::Exam 1",
    )


def test_companion_rebuild_supports_windows_writable_fsync_requirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows rejects fsync when the already-written database is reopened read-only."""
    original_open = Path.open

    def windows_open(
        self: Path,
        mode: str = "r",
        *args: object,
        **kwargs: object,
    ) -> object:
        if self.name.startswith(".cards.sqlite3.building-") and mode == "rb":
            raise OSError(errno.EBADF, "Bad file descriptor")
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", windows_open)
    index = AnkiIndex(tmp_path / "companion")

    index.rebuild_companion(
        [_note(10, "iron deficiency anemia")],
        snapshot_id="companion-1",
        fingerprint="a" * 64,
    )

    assert index.get_note(10) is not None


def test_companion_filters_nested_tags_and_exclusions(tmp_path: Path) -> None:
    index = _built_index(tmp_path)

    assert index.eligible_note_ids(
        CompanionFilters(tag_allowlist=("#Sketchy",))
    ) == {20, 30}
    assert index.eligible_note_ids(
        CompanionFilters(
            deck_allowlist=("AnKing Step Deck",),
            tag_allowlist=("#Sketchy",),
            excluded_tag_prefixes=("suspended",),
        )
    ) == {20}
    assert index.eligible_note_ids(CompanionFilters()) == {10, 20, 30}


def test_companion_fts_escapes_user_syntax_and_filters_before_limit(
    tmp_path: Path,
) -> None:
    index = _built_index(tmp_path)

    hits = index.search_fts(
        'iron deficiency (anemia) OR "unterminated',
        filters=CompanionFilters(
            deck_allowlist=("AnKing Step Deck::Heme",)
        ),
        limit=1,
    )

    assert [hit.note_id for hit in hits] == [10]


def test_trusted_source_families_are_distinct_and_persisted(
    tmp_path: Path,
) -> None:
    index = _built_index(tmp_path)

    assert trusted_source_families(
        (
            "#Pathoma::Heme",
            "#Pathoma::Anemia",
            "#Sketchy::Pharm",
            "OMS::Lecture_3",
        )
    ) == ("pathoma", "sketchy")
    assert index.source_families(10) == ("pathoma",)
    assert index.source_count(10) == 1


def test_semantic_alignment_counts_missing_stale_and_unexpected_rows(
    tmp_path: Path,
) -> None:
    index = _built_index(tmp_path)

    alignment = index.semantic_alignment(
        note_ids=(10, 20, 99),
        content_hashes=(
            content_hash("iron deficiency anemia"),
            content_hash("stale warfarin text"),
            content_hash("unexpected note"),
        ),
    )

    assert alignment.eligible_count == 3
    assert alignment.compatible_count == 1
    assert alignment.coverage == pytest.approx(1 / 3)
    assert alignment.missing_or_stale_note_ids == (20, 30)
    assert alignment.unexpected_note_ids == (99,)


def test_semantic_alignment_excludes_notes_without_searchable_text(
    tmp_path: Path,
) -> None:
    index = AnkiIndex(tmp_path / "companion")
    index.rebuild_companion(
        [
            _note(10, "iron deficiency anemia"),
            _note(
                20,
                "",
                decks=("AnKing Step Deck::Image Occlusion",),
                extra="",
            ),
        ],
        snapshot_id="companion-blank-text",
        fingerprint="e" * 64,
    )

    alignment = index.semantic_alignment(
        note_ids=(10,),
        content_hashes=(content_hash("iron deficiency anemia"),),
    )

    assert alignment.eligible_count == 1
    assert alignment.compatible_count == 1
    assert alignment.coverage == 1.0
    assert alignment.missing_or_stale_note_ids == ()


def test_companion_uses_extra_for_semantic_fallback_and_skips_blank_note(
    tmp_path: Path,
) -> None:
    class ImageOcclusionAnki:
        async def find_notes(self, query: str) -> list[int]:
            assert query == ""
            return [101, 102]

        async def find_cards(self, query: str) -> list[int]:
            assert query == ""
            return [201, 202]

        async def notes_info(
            self,
            note_ids: Sequence[int],
        ) -> list[dict[str, Any]]:
            assert list(note_ids) == [101, 102]
            return [
                {
                    "noteId": 101,
                    "modelName": "IO-one by one",
                    "tags": [],
                    "fields": {"Text": {"value": ""}, "Extra": {"value": "Image label"}},
                    "cards": [201],
                    "mod": 1_752_000_000,
                },
                {
                    "noteId": 102,
                    "modelName": "IO-one by one",
                    "tags": [],
                    "fields": {"Text": {"value": ""}, "Extra": {"value": ""}},
                    "cards": [202],
                    "mod": 1_752_000_000,
                },
            ]

        async def cards_info(
            self,
            card_ids: Sequence[int],
        ) -> list[dict[str, Any]]:
            assert list(card_ids) == [201, 202]
            return [
                {"cardId": 201, "note": 101, "deckName": "AnKing Step Deck"},
                {"cardId": 202, "note": 102, "deckName": "AnKing Step Deck"},
            ]

    async def scenario() -> None:
        index = AnkiIndex(tmp_path / "companion")
        semantic = FakeSemanticRefresher(
            index,
            expected_snapshot_id="local-image-occlusion",
        )
        notes = await index.refresh_from_anki(
            ImageOcclusionAnki(),
            snapshot_id="local-image-occlusion",
            fingerprint="f" * 64,
            semantic_refresher=semantic,
        )

        assert [note.note_id for note in notes] == [101, 102]
        assert [(record.note_id, record.text) for record in semantic.records] == [
            (101, "Image label")
        ]
        assert semantic.expected_note_ids == {101}

    asyncio.run(scenario())


class FakeLocalAnki:
    def __init__(self) -> None:
        self.find_cards_queries: list[str] = []

    async def find_notes(self, query: str) -> list[int]:
        assert query == ""
        return [101]

    async def find_cards(self, query: str) -> list[int]:
        self.find_cards_queries.append(query)
        return [201, 202]

    async def notes_info(
        self,
        note_ids: Sequence[int],
    ) -> list[dict[str, Any]]:
        assert list(note_ids) == [101]
        return [
            {
                "noteId": 101,
                "modelName": "AnKingOverhaul",
                "tags": ["#Pathoma::Hematology::Anemia"],
                "fields": {
                    "Text": {"value": "{{c1::Iron deficiency}} anemia"},
                    "Extra": {"value": "Low ferritin"},
                },
                "cards": [201, 202],
                "mod": 1_752_000_000,
            }
        ]

    async def cards_info(
        self,
        card_ids: Sequence[int],
    ) -> list[dict[str, Any]]:
        assert list(card_ids) == [201, 202]
        return [
            {
                "cardId": 201,
                "note": 101,
                "deckName": "AnKing Step Deck::Heme",
            },
            {
                "cardId": 202,
                "note": 101,
                "deckName": "Filtered::Exam 1",
            },
        ]


class FakeSemanticRefresher:
    def __init__(
        self,
        index: AnkiIndex,
        *,
        expected_snapshot_id: str = "local-1",
    ) -> None:
        self.index = index
        self.expected_snapshot_id = expected_snapshot_id
        self.records: list[DocumentRecord] = []
        self.expected_note_ids: set[int] = set()

    async def refresh(
        self,
        records: Sequence[DocumentRecord],
        *,
        expected_note_ids: Collection[int] | None = None,
    ) -> object:
        assert self.index.snapshot_id() == self.expected_snapshot_id
        self.records = list(records)
        self.expected_note_ids = set(expected_note_ids or ())
        return object()


def test_companion_rebuilds_from_local_ankiconnect_metadata(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        index = AnkiIndex(tmp_path / "companion")
        gateway = FakeLocalAnki()
        semantic = FakeSemanticRefresher(index)

        notes = await index.refresh_from_anki(
            gateway,
            snapshot_id="local-1",
            fingerprint="b" * 64,
            semantic_refresher=semantic,
        )

        assert len(notes) == 1
        assert notes[0].text == "Iron deficiency anemia"
        assert notes[0].deck_names == (
            "AnKing Step Deck::Heme",
            "Filtered::Exam 1",
        )
        assert notes[0].modified_at == 1_752_000_000
        assert gateway.find_cards_queries == [""]
        assert [record.note_id for record in semantic.records] == [101]
        assert semantic.expected_note_ids == {101}
        assert index.eligible_note_ids(
            CompanionFilters(deck_allowlist=("Filtered::Exam 1",))
        ) == {101}

    asyncio.run(scenario())


def test_semantic_refresh_failure_keeps_new_companion_for_alignment_gate(
    tmp_path: Path,
) -> None:
    class FailingSemantic:
        async def refresh(
            self,
            records: Sequence[DocumentRecord],
            *,
            expected_note_ids: Collection[int] | None = None,
        ) -> object:
            del records, expected_note_ids
            raise RuntimeError("injected Voyage failure")

    async def scenario() -> None:
        index = AnkiIndex(tmp_path / "companion")
        gateway = FakeLocalAnki()
        await index.refresh_from_anki(
            gateway,
            snapshot_id="local-1",
            fingerprint="b" * 64,
        )

        with pytest.raises(RuntimeError, match="Voyage"):
            await index.refresh_from_anki(
                gateway,
                snapshot_id="local-2",
                fingerprint="c" * 64,
                semantic_refresher=FailingSemantic(),
            )

        assert index.snapshot_id() == "local-2"

    asyncio.run(scenario())


def test_local_refresh_reads_large_collections_in_bounded_batches(
    tmp_path: Path,
) -> None:
    class BatchedAnki:
        def __init__(self) -> None:
            self.note_batches: list[tuple[int, ...]] = []
            self.card_batches: list[tuple[int, ...]] = []

        async def find_notes(self, query: str) -> list[int]:
            assert query == 'deck:"AnKing Step Deck"'
            return [1, 2, 3, 4, 5]

        async def find_cards(self, query: str) -> list[int]:
            assert query == 'deck:"AnKing Step Deck"'
            return [101, 102, 103, 104, 105]

        async def notes_info(
            self,
            note_ids: Sequence[int],
        ) -> list[dict[str, Any]]:
            self.note_batches.append(tuple(note_ids))
            return [
                {
                    "noteId": note_id,
                    "modelName": "AnKingOverhaul",
                    "tags": ["#AK_Step"],
                    "fields": {
                        "Text": {"value": f"note {note_id}"},
                        "Extra": {"value": ""},
                    },
                    "cards": [note_id + 100],
                    "mod": 1_752_000_000,
                }
                for note_id in note_ids
            ]

        async def cards_info(
            self,
            card_ids: Sequence[int],
        ) -> list[dict[str, Any]]:
            self.card_batches.append(tuple(card_ids))
            return [
                {
                    "cardId": card_id,
                    "note": card_id - 100,
                    "deckName": "AnKing Step Deck",
                }
                for card_id in card_ids
            ]

    async def scenario() -> None:
        gateway = BatchedAnki()
        index = AnkiIndex(tmp_path / "companion")

        notes = await index.refresh_from_anki(
            gateway,
            snapshot_id="local-batched",
            fingerprint="d" * 64,
            query='deck:"AnKing Step Deck"',
            metadata_batch_size=2,
        )

        assert len(notes) == 5
        assert gateway.note_batches == [(1, 2), (3, 4), (5,)]
        assert gateway.card_batches == [
            (101, 102),
            (103, 104),
            (105,),
        ]

    asyncio.run(scenario())


def test_delta_refresh_keeps_note_identity_when_card_moves_decks(
    tmp_path: Path,
) -> None:
    class MovingCardAnki(FakeLocalAnki):
        deck_name = "Original::Deck"

        async def cards_info(
            self,
            card_ids: Sequence[int],
        ) -> list[dict[str, Any]]:
            assert list(card_ids) == [201, 202]
            return [
                {"cardId": 201, "note": 101, "deckName": self.deck_name},
                {"cardId": 202, "note": 101, "deckName": self.deck_name},
            ]

    async def scenario() -> None:
        index = AnkiIndex(tmp_path / "companion")
        gateway = MovingCardAnki()
        await index.refresh_from_anki(
            gateway,
            snapshot_id="local-1",
            fingerprint="b" * 64,
        )
        gateway.deck_name = "Moved::Deck"
        await index.refresh_from_anki(
            gateway,
            snapshot_id="local-2",
            fingerprint="c" * 64,
        )

        assert index.snapshot_id() == "local-2"
        assert index.get_note(101).deck_names == ("Moved::Deck",)  # type: ignore[union-attr]
        assert index.eligible_note_ids(
            CompanionFilters(deck_allowlist=("Original",))
        ) == set()

    asyncio.run(scenario())
