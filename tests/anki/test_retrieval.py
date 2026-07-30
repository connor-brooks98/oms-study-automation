import asyncio
from collections.abc import Collection, Sequence

from oms_hub.anki.domain import RetrievalPass
from oms_hub.anki.index import CompanionFilters, SearchHit
from oms_hub.anki.lcl import (
    LectureConcept,
    LedgerSourceRef,
)
from oms_hub.anki.normalize import NormalizedNote
from oms_hub.anki.retrieval import RetrievalScope, RetrievalService
from oms_hub.anki.semantic.domain import SemanticHit


def _concept() -> LectureConcept:
    return LectureConcept(
        concept_id="iron-deficiency",
        source_refs=(LedgerSourceRef(passage_id="a" * 64),),
        statement="Iron deficiency causes low ferritin",
        hypothetical_card="Ferritin is low when iron stores are depleted",
        paraphrases=(
            "Early laboratory evidence of depleted iron stores",
            "How ferritin changes during iron deficiency",
        ),
        importance="core",
    )


def _note(
    note_id: int,
    *,
    tags: tuple[str, ...] = (),
    sources: tuple[str, ...] = (),
) -> NormalizedNote:
    return NormalizedNote(
        note_id=note_id,
        model_name="AnKingOverhaul",
        text=f"note {note_id}",
        extra="",
        raw_fields={"Text": f"note {note_id}"},
        tags=tags,
        card_ids=(note_id + 100,),
        media=(),
        token_signature=str(note_id),
        content_sha256=f"{note_id:064x}",
        source_families=sources,
    )


class FakeSemanticSearch:
    def __init__(
        self,
        results: dict[str, list[int]],
    ) -> None:
        self.results = results
        self.eligible_calls: list[set[int] | None] = []

    async def search(
        self,
        queries: Sequence[str],
        *,
        eligible_note_ids: Collection[int] | None = None,
        limit: int,
    ) -> list[list[SemanticHit]]:
        self.eligible_calls.append(
            None
            if eligible_note_ids is None
            else set(eligible_note_ids)
        )
        return [
            [
                SemanticHit(
                    note_id=note_id,
                    score=1.0 - rank / 100,
                    content_hash=f"{note_id:064x}",
                )
                for rank, note_id in enumerate(
                    self.results.get(query, [])[:limit],
                    start=1,
                )
            ]
            for query in queries
        ]


class FakeCompanionIndex:
    def __init__(
        self,
        notes: Sequence[NormalizedNote],
        *,
        eligible: set[int],
        lexical: Sequence[int] = (),
    ) -> None:
        self.notes = {note.note_id: note for note in notes}
        self.eligible = eligible
        self.lexical = list(lexical)
        self.fts_filters: list[CompanionFilters] = []

    def eligible_note_ids(self, filters: CompanionFilters) -> set[int]:
        return set(self.eligible)

    def search_fts(
        self,
        query: str,
        *,
        filters: CompanionFilters,
        limit: int,
    ) -> list[SearchHit]:
        self.fts_filters.append(filters)
        return [
            SearchHit(note_id=note_id, score=1.0 - rank / 100)
            for rank, note_id in enumerate(
                self.lexical[:limit],
                start=1,
            )
            if note_id in self.eligible
        ]

    def get_note(self, note_id: int) -> NormalizedNote | None:
        return self.notes.get(note_id)


def test_semantic_variants_are_fused_before_modalities() -> None:
    async def scenario() -> None:
        concept = _concept()
        semantic = FakeSemanticSearch(
            {
                concept.queries[0]: [2, 1],
                concept.queries[1]: [3, 1],
                concept.queries[2]: [4, 1],
                concept.queries[3]: [5, 1],
            }
        )
        companion = FakeCompanionIndex(
            [_note(note_id) for note_id in range(1, 6)],
            eligible=set(range(1, 6)),
        )
        service = RetrievalService(
            companion,
            semantic,
            per_concept_limit=5,
            global_limit=10,
        )

        candidates = await service.retrieve_pass_1(
            concept,
            RetrievalScope(),
        )

        assert candidates[0].note_id == 1
        assert candidates[0].scores["semantic_variant_fusion"] > (
            candidates[1].scores["semantic_variant_fusion"]
        )

    asyncio.run(scenario())


def test_filters_are_applied_before_semantic_and_lexical_ranking() -> None:
    async def scenario() -> None:
        concept = _concept()
        filters = CompanionFilters(
            deck_allowlist=("AnKing Step Deck::Heme",),
            tag_allowlist=("#Pathoma",),
        )
        semantic = FakeSemanticSearch(
            {query: [99, 1, 2] for query in concept.queries}
        )
        companion = FakeCompanionIndex(
            [_note(1), _note(2), _note(99)],
            eligible={1, 2},
            lexical=(99, 2),
        )
        service = RetrievalService(
            companion,
            semantic,
            per_concept_limit=5,
            global_limit=10,
        )

        candidates = await service.retrieve_pass_1(
            concept,
            RetrievalScope(filters=filters),
        )

        assert semantic.eligible_calls == [{1, 2}]
        assert companion.fts_filters == [filters]
        assert {candidate.note_id for candidate in candidates} <= {1, 2}

    asyncio.run(scenario())


def test_rrf_boosts_are_bounded_and_explainable() -> None:
    async def scenario() -> None:
        concept = _concept()
        semantic = FakeSemanticSearch(
            {query: [1, 2] for query in concept.queries}
        )
        companion = FakeCompanionIndex(
            [
                _note(1),
                _note(
                    2,
                    tags=(
                        "OMS::Heme::Lecture_3",
                        "OMS::Heme::Block_1",
                    ),
                    sources=("pathoma", "sketchy"),
                ),
            ],
            eligible={1, 2},
            lexical=(2, 1),
        )
        service = RetrievalService(
            companion,
            semantic,
            per_concept_limit=5,
            global_limit=10,
        )

        candidates = await service.retrieve_pass_1(
            concept,
            RetrievalScope(
                lecture_tag_prefix="OMS::Heme::Lecture_3",
                block_tag_prefix="OMS::Heme::Block_1",
            ),
        )
        boosted = next(
            candidate for candidate in candidates if candidate.note_id == 2
        )

        assert boosted.scores["boost_total"] <= 0.05
        assert boosted.scores["boosted_score"] == (
            boosted.scores["base_rrf"] + boosted.scores["boost_total"]
        )
        assert "lecture_tag" in boosted.provenance["reasons"]
        assert boosted.retrieval_pass is RetrievalPass.PASS_1
        assert boosted.verdict == "pending"

    asyncio.run(scenario())


def test_deterministic_ties_and_candidate_caps() -> None:
    async def scenario() -> None:
        concept = _concept()
        semantic = FakeSemanticSearch(
            {query: [2, 1, 3] for query in concept.queries}
        )
        companion = FakeCompanionIndex(
            [_note(1), _note(2), _note(3)],
            eligible={1, 2, 3},
            lexical=(1, 2, 3),
        )
        service = RetrievalService(
            companion,
            semantic,
            per_concept_limit=2,
            global_limit=1,
        )

        candidates = await service.retrieve_pass_1(
            concept,
            RetrievalScope(),
        )

        assert len(candidates) == 1
        assert candidates[0].note_id in {1, 2}

    asyncio.run(scenario())
