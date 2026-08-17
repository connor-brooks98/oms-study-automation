"""Small deterministic facade for v3's frozen-index hybrid retrieval."""

from __future__ import annotations

import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from oms_hub.anki.calibration import (
    QUERY_CHARACTER_LIMIT,
    QUERY_VARIANT_LIMIT,
    RAW_LIMIT,
    exact_term_matches,
)
from oms_hub.anki.index import CompanionFilters
from oms_hub.anki.normalize import semantic_text
from oms_hub.anki.retrieval import HybridFusionRow, candidate_boost, hybrid_rank_fusion
from oms_hub.anki.semantic.service import content_hash


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def query_variants(
    *,
    fact_statement: str,
    canonical_statement: str,
    primary_entity: str,
    aliases: Sequence[str],
    exact_terms: Sequence[str],
    professor_policy_basis: Sequence[str],
    retrieval_queries: Sequence[str],
    max_variants: int = QUERY_VARIANT_LIMIT,
    max_characters: int = QUERY_CHARACTER_LIMIT,
) -> tuple[tuple[str, ...], tuple[dict[str, str], ...]]:
    raw = (
        ("exact_terms", " ".join(exact_terms)),
        ("entity_aliases", " ".join((primary_entity, *aliases))),
        ("fact_statement", fact_statement),
        ("canonical_statement", canonical_statement),
        *(("professor_policy_basis", value) for value in sorted(professor_policy_basis)[:2]),
        *(("retrieval_query", value) for value in sorted(retrieval_queries)[:2]),
    )
    kept: list[str] = []
    trace: list[dict[str, str]] = []
    seen: set[str] = set()
    for kind, value in raw:
        normalized = _normalize(value)
        if not normalized:
            trace.append({"kind": kind, "reason": "omitted_blank"})
        elif len(normalized) > max_characters:
            trace.append({"kind": kind, "reason": "omitted_too_long"})
        elif normalized.casefold() in seen:
            trace.append({"kind": kind, "reason": "omitted_duplicate"})
        elif len(kept) == max_variants:
            trace.append({"kind": kind, "reason": "omitted_cap"})
        else:
            seen.add(normalized.casefold())
            kept.append(normalized)
            trace.append({"kind": kind, "query": normalized})
    return tuple(kept), tuple(trace)


@dataclass(frozen=True, slots=True)
class HybridCard:
    note_id: int
    content_sha256: str
    text: str
    extra: str
    tags: tuple[str, ...]
    decks: tuple[str, ...]
    fusion: HybridFusionRow
    semantic_score: float | None
    semantic_variant_scores: dict[str, float]
    exact_match_reasons: tuple[str, ...]
    boost_total: float
    boost_reasons: tuple[str, ...]


class CardCentricHybridRetriever:
    def __init__(self, companion: Any, semantic: Any) -> None:
        self.companion = companion
        self.semantic = semantic
        self.last_semantic_trace: tuple[tuple[dict[str, object], ...], ...] = ()
        self.last_lexical_trace: tuple[dict[str, object], ...] = ()

    async def retrieve(
        self,
        *,
        variants: Sequence[str],
        exact_terms: Sequence[str],
        filters: CompanionFilters,
        expected_generation: str,
        variant_weights: Mapping[str, float] | Sequence[float],
        semantic_eligible_note_ids: Collection[int] | None = None,
        raw_limit: int = RAW_LIMIT,
        rrf_k: int = 60,
        lecture_tag_prefix: str | None = None,
        block_tag_prefix: str | None = None,
        boost_weights: Mapping[str, float] | None = None,
    ) -> tuple[HybridCard, ...]:
        if not variants:
            return ()
        eligible: Collection[int] = self.companion.eligible_note_ids(filters)
        semantic_eligible = (
            set(eligible)
            if semantic_eligible_note_ids is None
            else set(eligible).intersection(semantic_eligible_note_ids)
        )
        semantic_lists = await self.semantic.search(
            variants,
            eligible_note_ids=semantic_eligible,
            limit=raw_limit,
            expected_generation=expected_generation,
        )
        if len(semantic_lists) != len(variants):
            raise ValueError("semantic result count does not match variants")
        self.last_semantic_trace = tuple(
            tuple(
                {
                    "note_id": hit.note_id,
                    "score": hit.score,
                    "content_hash": hit.content_hash,
                }
                for hit in hits
            )
            for hits in semantic_lists
        )
        rankings: dict[str, tuple[int, ...]] = {}
        scores: dict[int, float] = {}
        variant_scores: dict[int, dict[str, float]] = {}
        for index, hits in enumerate(semantic_lists):
            valid: list[int] = []
            for hit in hits:
                note = self.companion.get_note(hit.note_id)
                if (
                    hit.note_id in semantic_eligible
                    and note is not None
                    and content_hash(semantic_text(note)) == hit.content_hash
                ):
                    valid.append(hit.note_id)
                    scores[hit.note_id] = max(scores.get(hit.note_id, float("-inf")), hit.score)
                    variant_scores.setdefault(hit.note_id, {})[f"variant_{index + 1}"] = hit.score
            rankings[f"variant_{index + 1}"] = tuple(valid)
        lexical_query = _normalize(" ".join(exact_terms)) or variants[0]
        lexical = self.companion.search_fts(lexical_query, filters=filters, limit=raw_limit)
        self.last_lexical_trace = tuple(
            {"note_id": hit.note_id, "score": hit.score} for hit in lexical
        )
        fusion = hybrid_rank_fusion(
            rankings,
            tuple(hit.note_id for hit in lexical if hit.note_id in eligible),
            variant_weights=variant_weights,
            rrf_k=rrf_k,
        )
        cards: list[HybridCard] = []
        for row in fusion:
            note = self.companion.get_note(row.note_id)
            if note is None:
                continue
            reasons = tuple(
                term for term in exact_terms if exact_term_matches(term, note.text, note.extra)
            )
            boost, boost_reasons = candidate_boost(
                note,
                lecture_tag_prefix=lecture_tag_prefix,
                block_tag_prefix=block_tag_prefix,
                weights=boost_weights,
            )
            cards.append(
                HybridCard(
                    note.note_id,
                    note.content_sha256,
                    note.text,
                    note.extra,
                    note.tags,
                    note.deck_names,
                    row,
                    scores.get(note.note_id),
                    dict(variant_scores.get(note.note_id, {})),
                    reasons,
                    boost,
                    boost_reasons,
                )
            )
        return tuple(cards)
