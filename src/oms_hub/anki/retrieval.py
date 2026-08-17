from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol

from oms_hub.anki.domain import Candidate, RetrievalPass
from oms_hub.anki.index import CompanionFilters, SearchHit
from oms_hub.anki.lcl import LectureConcept
from oms_hub.anki.normalize import NormalizedNote, semantic_text
from oms_hub.anki.semantic.domain import SemanticHit
from oms_hub.anki.semantic.service import content_hash

_VARIANT_WEIGHTS = (1.0, 0.9, 0.8, 0.8)
_RRF_K = 60


@dataclass(frozen=True, slots=True)
class HybridFusionRow:
    """The deterministic two-level RRF result shared by v2 and v3."""

    note_id: int
    semantic_variant_scores: dict[str, float]
    semantic_variant_ranks: dict[str, int]
    aggregate_semantic_rank: int | None
    lexical_rank: int | None
    base_rrf: float


def hybrid_rank_fusion(
    semantic_rankings: (
        Mapping[str, Sequence[int | None]] | Sequence[tuple[str, Sequence[int | None]]]
    ),
    lexical_note_ids: Sequence[int],
    *,
    variant_weights: Mapping[str, float] | Sequence[float],
    rrf_k: int = _RRF_K,
) -> tuple[HybridFusionRow, ...]:
    """Fuse named semantic rankings, then fuse that order with lexical ranks."""
    if rrf_k < 0:
        raise ValueError("rrf_k must be nonnegative")
    items = (
        tuple(semantic_rankings.items())
        if isinstance(semantic_rankings, Mapping)
        else tuple(semantic_rankings)
    )
    if len({variant for variant, _ in items}) != len(items):
        raise ValueError("semantic variant IDs must be unique")
    if len(set(lexical_note_ids)) != len(lexical_note_ids):
        raise ValueError("lexical note IDs must be unique")
    if isinstance(variant_weights, Mapping):
        weights = {variant: variant_weights[variant] for variant, _ in items}
    else:
        if not variant_weights:
            raise ValueError("variant weights cannot be empty")
        weights = {
            variant: variant_weights[min(index, len(variant_weights) - 1)]
            for index, (variant, _) in enumerate(items)
        }
    if any(weight < 0 for weight in weights.values()):
        raise ValueError("variant weights cannot be negative")
    scores: defaultdict[int, float] = defaultdict(float)
    ranks: defaultdict[int, dict[str, int]] = defaultdict(dict)
    variant_scores: defaultdict[int, dict[str, float]] = defaultdict(dict)
    for variant, note_ids in items:
        present_note_ids = tuple(note_id for note_id in note_ids if note_id is not None)
        if len(set(present_note_ids)) != len(present_note_ids):
            raise ValueError("semantic ranking note IDs must be unique")
        weight = weights[variant]
        if weight == 0:
            continue
        for rank, note_id in enumerate(note_ids, start=1):
            if note_id is None:
                continue
            contribution = weight / (rrf_k + rank)
            scores[note_id] += contribution
            ranks[note_id][variant] = rank
            variant_scores[note_id][variant] = contribution
    semantic_order = tuple(sorted(scores, key=lambda note_id: (-scores[note_id], note_id)))
    semantic_ranks = {note_id: rank for rank, note_id in enumerate(semantic_order, start=1)}
    lexical_ranks = {note_id: rank for rank, note_id in enumerate(lexical_note_ids, start=1)}
    rows = []
    for note_id in sorted(set(semantic_ranks) | set(lexical_ranks)):
        semantic_rank = semantic_ranks.get(note_id)
        lexical_rank = lexical_ranks.get(note_id)
        base_rrf = ((1.0 / (rrf_k + semantic_rank)) if semantic_rank is not None else 0.0) + (
            (1.0 / (rrf_k + lexical_rank)) if lexical_rank is not None else 0.0
        )
        rows.append(
            HybridFusionRow(
                note_id=note_id,
                semantic_variant_scores=dict(variant_scores.get(note_id, {})),
                semantic_variant_ranks=dict(ranks.get(note_id, {})),
                aggregate_semantic_rank=semantic_rank,
                lexical_rank=lexical_rank,
                base_rrf=base_rrf,
            )
        )
    return tuple(sorted(rows, key=lambda row: (-row.base_rrf, row.note_id)))


def candidate_boost(
    note: NormalizedNote,
    *,
    lecture_tag_prefix: str | None,
    block_tag_prefix: str | None,
    weights: Mapping[str, float] | None = None,
) -> tuple[float, tuple[str, ...]]:
    """The legacy boost calculation, kept byte-for-byte compatible."""
    resolved = weights or {
        "lecture_tag": 0.02,
        "block_tag": 0.015,
        "trusted_source": 0.005,
        "cap": 0.05,
    }
    reasons: list[str] = []
    boost_total = 0.0
    if _has_tag_prefix(note.tags, lecture_tag_prefix):
        boost_total += resolved["lecture_tag"]
        reasons.append("lecture_tag")
    if _has_tag_prefix(note.tags, block_tag_prefix):
        boost_total += resolved["block_tag"]
        reasons.append("block_tag")
    if note.source_families:
        boost_total += min(len(set(note.source_families)), 3) * resolved["trusted_source"]
        reasons.append("trusted_source")
    return min(boost_total, resolved["cap"]), tuple(reasons)


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


class GroundedQuery(Protocol):
    text: str
    evidence_ids: tuple[str, ...]


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
        self.candidate_pool_limit = (
            candidate_pool_limit
            or max(
                per_concept_limit,
                global_limit,
            )
            * 4
        )

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
            evidence_ids=(),
        )

    async def retrieve_pass_2(
        self,
        concept: LectureConcept,
        queries: Sequence[GroundedQuery],
        scope: RetrievalScope,
    ) -> list[Candidate]:
        if not queries:
            raise ValueError("Pass 2 requires grounded rescue queries")
        return await self._retrieve(
            concept,
            [query.text for query in queries],
            scope,
            retrieval_pass=RetrievalPass.PASS_2_RESCUE,
            evidence_ids=tuple(
                dict.fromkeys(
                    evidence_id for query in queries for evidence_id in query.evidence_ids
                )
            ),
        )

    async def retrieve_convergence(
        self,
        concept: LectureConcept,
        queries: Sequence[str],
        scope: RetrievalScope,
        *,
        pass_number: int,
    ) -> list[Candidate]:
        if len(queries) != 3 or not 3 <= pass_number <= 5:
            raise ValueError("convergence retrieval requires three queries for pass 3-5")
        return await self._retrieve(
            concept,
            queries,
            scope,
            retrieval_pass=RetrievalPass.CONVERGENCE,
            evidence_ids=(),
            convergence_pass=pass_number,
        )

    async def _retrieve(
        self,
        concept: LectureConcept,
        queries: Sequence[str],
        scope: RetrievalScope,
        *,
        retrieval_pass: RetrievalPass,
        evidence_ids: tuple[str, ...],
        convergence_pass: int | None = None,
    ) -> list[Candidate]:
        decks = scope.filters.deck_allowlist
        if len(decks) <= 1:
            return await self._retrieve_once(
                concept,
                queries,
                scope,
                retrieval_pass=retrieval_pass,
                evidence_ids=evidence_ids,
                convergence_pass=convergence_pass,
            )
        prioritized: list[Candidate] = []
        for priority, deck in enumerate(decks):
            deck_scope = replace(
                scope,
                filters=replace(scope.filters, deck_allowlist=(deck,)),
            )
            candidates = await self._retrieve_once(
                concept,
                queries,
                deck_scope,
                retrieval_pass=retrieval_pass,
                evidence_ids=evidence_ids,
                convergence_pass=convergence_pass,
            )
            prioritized.extend(
                replace(
                    candidate,
                    provenance={
                        **candidate.provenance,
                        "deck_name": deck,
                        "deck_priority": priority,
                    },
                )
                for candidate in candidates
            )
        return prioritized

    async def _retrieve_once(
        self,
        concept: LectureConcept,
        queries: Sequence[str],
        scope: RetrievalScope,
        *,
        retrieval_pass: RetrievalPass,
        evidence_ids: tuple[str, ...],
        convergence_pass: int | None = None,
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
        semantic_rankings: dict[str, tuple[int | None, ...]] = {}
        for variant, hits in enumerate(semantic_lists):
            note_ids: list[int | None] = []
            for hit in hits:
                if hit.note_id not in eligible:
                    note_ids.append(None)
                    continue
                note = self.companion.get_note(hit.note_id)
                if note is None or content_hash(semantic_text(note)) != hit.content_hash:
                    note_ids.append(None)
                    continue
                note_ids.append(hit.note_id)
            semantic_rankings[f"variant_{variant + 1}"] = tuple(note_ids)
        lexical_hits = self.companion.search_fts(
            queries[0],
            filters=scope.filters,
            limit=self.candidate_pool_limit,
        )
        lexical_note_ids = tuple(
            hit.note_id
            for hit in lexical_hits
            if hit.note_id in eligible and self.companion.get_note(hit.note_id) is not None
        )
        fusion = hybrid_rank_fusion(
            semantic_rankings,
            lexical_note_ids,
            variant_weights=_VARIANT_WEIGHTS,
        )
        candidates = [
            self._candidate(
                row,
                concept,
                scope,
                retrieval_pass=retrieval_pass,
                queries=queries,
                evidence_ids=evidence_ids,
                convergence_pass=convergence_pass,
            )
            for row in fusion
        ]
        ordered = sorted(
            candidates,
            key=lambda candidate: (
                -candidate.scores["boosted_score"],
                -candidate.scores["base_rrf"],
                candidate.note_id,
            ),
        )
        return ordered[: min(self.per_concept_limit, self.global_limit)]

    def _candidate(
        self,
        row: HybridFusionRow,
        concept: LectureConcept,
        scope: RetrievalScope,
        *,
        retrieval_pass: RetrievalPass,
        queries: Sequence[str],
        evidence_ids: tuple[str, ...],
        convergence_pass: int | None,
    ) -> Candidate:
        note = self.companion.get_note(row.note_id)
        if note is None:
            raise ValueError("retrieval candidate is absent from companion")
        semantic_rank = row.aggregate_semantic_rank
        lexical_rank = row.lexical_rank
        base_rrf = row.base_rrf
        boost_total, reasons = candidate_boost(
            note,
            lecture_tag_prefix=scope.lecture_tag_prefix,
            block_tag_prefix=scope.block_tag_prefix,
        )
        provenance = {
            "queries": list(queries),
            "evidence_ids": list(evidence_ids),
            "variant_ranks": dict(row.semantic_variant_ranks),
            "semantic_rank": semantic_rank,
            "lexical_rank": lexical_rank,
            "reasons": list(reasons),
        }
        if convergence_pass is not None:
            provenance["convergence_pass"] = convergence_pass
        return Candidate(
            note_id=note.note_id,
            content_hash=note.content_sha256,
            best_concept_id=concept.concept_id,
            provenance=provenance,
            scores={
                "semantic_variant_fusion": sum(row.semantic_variant_scores.values()),
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
        tag.casefold() == normalized or tag.casefold().startswith(f"{normalized}::") for tag in tags
    )
