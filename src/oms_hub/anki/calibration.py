"""Deterministic card-centric-v3 retrieval calibration and clustering."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

SEMANTIC_THRESHOLD = 0.50
RRF_FLOOR = 1 / 110
POLLUTION_CEILING = 25
POLLUTION_RATIO = 0.60
COSINE_CLUSTER_THRESHOLD = 0.88
FINAL_CANDIDATES_PER_FACT = 20
GLOBAL_UNIQUE_CANDIDATES = 200
SEMANTIC_VARIANT_WEIGHTS = (1.0, 0.9, 0.8)
RAW_LIMIT = 50
QUERY_VARIANT_LIMIT = 8
QUERY_CHARACTER_LIMIT = 1000
BOOST_PARAMETERS = {
    "lecture_tag": 0.02,
    "block_tag": 0.015,
    "trusted_source": 0.005,
    "cap": 0.05,
}
TAG_MODE_VERSION = 1


def normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def exact_term_matches(term: str, text: str, extra: str = "") -> bool:
    """Require contiguous normalized token sequences, never a substring match."""
    needle = _token_text(term).split()
    haystack = _token_text(f"{text} {extra}").split()
    return bool(needle) and any(
        haystack[index : index + len(needle)] == needle for index in range(len(haystack))
    )


def _token_text(value: str) -> str:
    return "".join(
        " " if unicodedata.category(character).startswith("P") else character
        for character in normalized_text(value)
    )


def frozen_config_payload() -> dict[str, object]:
    return {
        "version": 1,
        "semantic_variant_weights": list(SEMANTIC_VARIANT_WEIGHTS),
        "raw_limit": RAW_LIMIT,
        "query_variant_limit": QUERY_VARIANT_LIMIT,
        "query_character_limit": QUERY_CHARACTER_LIMIT,
        "semantic_threshold": SEMANTIC_THRESHOLD,
        "rrf_k": 60,
        "rrf_floor": RRF_FLOOR,
        "pollution_ceiling": POLLUTION_CEILING,
        "pollution_ratio": POLLUTION_RATIO,
        "per_fact_limit": FINAL_CANDIDATES_PER_FACT,
        "global_unique_limit": GLOBAL_UNIQUE_CANDIDATES,
        "boost_parameters": dict(BOOST_PARAMETERS),
        "cosine_cluster_threshold": COSINE_CLUSTER_THRESHOLD,
        "tag_mode_version": TAG_MODE_VERSION,
    }


@dataclass(frozen=True, slots=True)
class PollutionDiagnostic:
    above_threshold_count: int
    off_scope_count: int
    ratio: float
    polluted: bool
    dominant_pattern: tuple[str, str] | None


def pollution_diagnostic(
    rows: Sequence[Mapping[str, Any]],
    *,
    threshold: float = SEMANTIC_THRESHOLD,
    ceiling: int = POLLUTION_CEILING,
    ratio_limit: float = POLLUTION_RATIO,
) -> PollutionDiagnostic:
    above = [row for row in rows if float(row.get("semantic_score", 0.0)) >= threshold]
    off_scope = [row for row in above if not bool(row.get("in_scope", False))]
    pattern_counts = Counter(
        (str(row.get("deck", "")), str(row.get("tag_root", "<untagged>"))) for row in off_scope
    )
    pattern = (
        min(pattern_counts, key=lambda item: (-pattern_counts[item], item))
        if pattern_counts
        else None
    )
    ratio = len(off_scope) / len(above) if above else 0.0
    return PollutionDiagnostic(
        len(above),
        len(off_scope),
        ratio,
        len(above) >= ceiling and ratio >= ratio_limit,
        pattern,
    )


def effective_tag_mode(requested: str, *, census_trusted: bool) -> str:
    if requested not in {"hard_filter", "prior_boost", "disabled"}:
        raise ValueError("unknown tag scope mode")
    return requested if requested != "hard_filter" or census_trusted else "prior_boost"


def deck_and_tag_eligible(category: str, *, mode: str) -> bool:
    if category == "deck_excluded":
        return False
    return mode != "hard_filter" or category == "target_tagged"


def calibrated_score(
    *,
    base_rrf: float,
    boost: float,
    semantic_score: float | None,
    exact_match: bool,
    polluted: bool,
    threshold: float = SEMANTIC_THRESHOLD,
) -> tuple[float, str]:
    if exact_match:
        return base_rrf + boost, "exact_survives"
    if semantic_score is not None and semantic_score < threshold:
        return base_rrf + boost, "below_semantic_threshold"
    return base_rrf + boost, "retained"


def cluster_note_ids(
    rows: Sequence[Mapping[str, Any]],
    *,
    vectors: Mapping[int, Sequence[float]],
    cosine_threshold: float = COSINE_CLUSTER_THRESHOLD,
) -> tuple[tuple[int, ...], ...]:
    """Union exact content then cosine-near note IDs; singleton uncovered rows survive."""
    ids = tuple(sorted({int(row["note_id"]) for row in rows}))
    parent = {note_id: note_id for note_id in ids}

    def root(note_id: int) -> int:
        while parent[note_id] != note_id:
            parent[note_id] = parent[parent[note_id]]
            note_id = parent[note_id]
        return note_id

    def union(left: int, right: int) -> None:
        left_root, right_root = root(left), root(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    by_hash: dict[str, int] = {}
    for row in sorted(rows, key=lambda item: int(item["note_id"])):
        note_id, content = int(row["note_id"]), str(row["content_sha256"])
        if content in by_hash:
            union(note_id, by_hash[content])
        else:
            by_hash[content] = note_id
    for index, left in enumerate(ids):
        if left not in vectors:
            continue
        for right in ids[index + 1 :]:
            if right not in vectors:
                continue
            left_vector, right_vector = vectors[left], vectors[right]
            dot = sum(a * b for a, b in zip(left_vector, right_vector, strict=True))
            left_norm = math.sqrt(sum(value * value for value in left_vector))
            right_norm = math.sqrt(sum(value * value for value in right_vector))
            if left_norm and right_norm and dot / (left_norm * right_norm) >= cosine_threshold:
                union(left, right)
    groups: dict[int, list[int]] = {}
    for note_id in ids:
        groups.setdefault(root(note_id), []).append(note_id)
    return tuple(tuple(values) for _, values in sorted(groups.items()))
