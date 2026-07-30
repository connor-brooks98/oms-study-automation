from collections import defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from oms_hub.anki.domain import Candidate, RetrievalPass
from oms_hub.anki.index import CompanionFilters, SearchHit
from oms_hub.anki.lcl import LectureConcept
from oms_hub.anki.normalize import NormalizedNote
from oms_hub.anki.semantic.domain import SemanticHit

_VARIANT_WEIGHTS = (1.0, 0.9, 0.8, 0.8)
_RRF_K = 60


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    filters: CompanionFilters = field(default_factory=CompanionFilters)
    lecture_tag_prefix: str | None = None
    block_tag_prefix: str | None = None


class CompanionSearch(Protocol):
    def eligible_note_ids(
        self,
        filters: CompanionFilters,
    ) -> set[int]: ...

    def search_fts(
        self,
        query: str,
        *,
        filters: CompanionFilters,
        limit: int,
    ) -> list[SearchHit]: ...

    def get_note(self, note_id: int) -> NormalizedNote | None: ...


class SemanticSearch(Protocol):
    async def search(
        self,
        queries: Sequence[str],
        *,
        eligible_note_ids: Collection[int] | None = None,
        limit: int,
    ) -> list[list[SemanticHit]]: ...


class RetrievalService:
    def __init__(
        self,
        companion: CompanionSearch,
        semantic: SemanticSearch,
        *,
        per_concept_limit: int,
        global_limit: int,
        candidate_pool_limit: int | None = None,
    ) -> None:
        if per_concept_limit < 1 or global_limit < 1:
            raise ValueError("retrieval limits must be positive")
        self.companion = companion
        self.semantic = semantic
        self.per_concept_limit = per_concept_limit
        self.global_limit = global_limit
        self.candidate_pool_limit = candidate_pool_limit or max(
            per_concept_limit,
            global_limit,
        ) * 4

    async def retrieve_pass_1(
        self,
        concept: LectureConcept,
        scope: RetrievalScope,
    ) -> list[Candidate]:
        return await self._retrieve(
            concept,
            concept.queries,
            scope,
            retrieval_pass=RetrievalPass.PASS_1,
        )

    async def _retrieve(
        self,
        concept: LectureConcept,
        queries: Sequence[str],
        scope: RetrievalScope,
        *,
        retrieval_pass: RetrievalPass,
    ) -> list[Candidate]:
        eligible = self.companion.eligible_note_ids(scope.filters)
        if not eligible:
            return []
        semantic_lists = await self.semantic.search(
            queries,
            eligible_note_ids=eligible,
            limit=self.candidate_pool_limit,
        )
        if len(semantic_lists) != len(queries):
            raise ValueError("semantic result count does not match queries")
        semantic_scores: defaultdict[int, float] = defaultdict(float)
        variant_ranks: defaultdict[int, dict[str, int]] = defaultdict(dict)
        for variant, hits in enumerate(semantic_lists):
            weight = (
                _VARIANT_WEIGHTS[variant]
                if variant < len(_VARIANT_WEIGHTS)
                else _VARIANT_WEIGHTS[-1]
            )
            for rank, hit in enumerate(hits, start=1):
                if hit.note_id not in eligible:
                    continue
                note = self.companion.get_note(hit.note_id)
                if note is None or note.content_sha256 != hit.content_hash:
                    continue
                semantic_scores[hit.note_id] += weight / (
                    _RRF_K + rank
                )
                variant_ranks[hit.note_id][f"variant_{variant + 1}"] = (
                    rank
                )
        semantic_order = sorted(
            semantic_scores,
            key=lambda note_id: (-semantic_scores[note_id], note_id),
        )
        semantic_ranks = {
            note_id: rank
            for rank, note_id in enumerate(semantic_order, start=1)
        }
        lexical_hits = self.companion.search_fts(
            concept.statement,
            filters=scope.filters,
            limit=self.candidate_pool_limit,
        )
        lexical_ranks = {
            hit.note_id: rank
            for rank, hit in enumerate(lexical_hits, start=1)
            if hit.note_id in eligible
            and self.companion.get_note(hit.note_id) is not None
        }
        candidate_ids = set(semantic_ranks) | set(lexical_ranks)
        candidates = [
            self._candidate(
                note_id,
                concept,
                scope,
                semantic_scores=semantic_scores,
                semantic_ranks=semantic_ranks,
                lexical_ranks=lexical_ranks,
                variant_ranks=variant_ranks,
                retrieval_pass=retrieval_pass,
            )
            for note_id in candidate_ids
        ]
        ordered = sorted(
            candidates,
            key=lambda candidate: (
                -candidate.scores["boosted_score"],
                -candidate.scores["base_rrf"],
                candidate.note_id,
            ),
        )
        return ordered[
            : min(self.per_concept_limit, self.global_limit)
        ]

    def _candidate(
        self,
        note_id: int,
        concept: LectureConcept,
        scope: RetrievalScope,
        *,
        semantic_scores: dict[int, float],
        semantic_ranks: dict[int, int],
        lexical_ranks: dict[int, int],
        variant_ranks: dict[int, dict[str, int]],
        retrieval_pass: RetrievalPass,
    ) -> Candidate:
        note = self.companion.get_note(note_id)
        if note is None:
            raise ValueError("retrieval candidate is absent from companion")
        semantic_rank = semantic_ranks.get(note_id)
        lexical_rank = lexical_ranks.get(note_id)
        base_rrf = (
            1.0 / (_RRF_K + semantic_rank)
            if semantic_rank is not None
            else 0.0
        ) + (
            1.0 / (_RRF_K + lexical_rank)
            if lexical_rank is not None
            else 0.0
        )
        reasons: list[str] = []
        boost_total = 0.0
        if _has_tag_prefix(note.tags, scope.lecture_tag_prefix):
            boost_total += 0.02
            reasons.append("lecture_tag")
        if _has_tag_prefix(note.tags, scope.block_tag_prefix):
            boost_total += 0.015
            reasons.append("block_tag")
        if note.source_families:
            boost_total += min(len(set(note.source_families)), 3) * 0.005
            reasons.append("trusted_source")
        boost_total = min(boost_total, 0.05)
        return Candidate(
            note_id=note.note_id,
            content_hash=note.content_sha256,
            best_concept_id=concept.concept_id,
            provenance={
                "queries": list(concept.queries),
                "variant_ranks": dict(variant_ranks.get(note_id, {})),
                "semantic_rank": semantic_rank,
                "lexical_rank": lexical_rank,
                "reasons": reasons,
            },
            scores={
                "semantic_variant_fusion": semantic_scores.get(
                    note_id,
                    0.0,
                ),
                "base_rrf": base_rrf,
                "boost_total": boost_total,
                "boosted_score": base_rrf + boost_total,
            },
            predicted_band="unjudged",
            verdict="pending",
            confidence=0.0,
            reason=", ".join(reasons) or "hybrid retrieval",
            context_trap=False,
            recall_direction="unknown",
            mnemonic_classification="unknown",
            dedupe_disposition="pending",
            selected=False,
            retrieval_pass=retrieval_pass,
        )


def _has_tag_prefix(
    tags: Sequence[str],
    prefix: str | None,
) -> bool:
    if prefix is None or not prefix.strip():
        return False
    normalized = prefix.strip().casefold()
    return any(
        tag.casefold() == normalized
        or tag.casefold().startswith(f"{normalized}::")
        for tag in tags
    )
