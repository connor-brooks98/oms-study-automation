from oms_hub.anki.calibration import (
    COSINE_CLUSTER_THRESHOLD,
    canonical_sha256,
    cluster_note_ids,
    deck_and_tag_eligible,
    effective_tag_mode,
    exact_term_matches,
    frozen_config_payload,
    pollution_diagnostic,
)
from oms_hub.anki.retrieval import hybrid_rank_fusion


def test_exact_terms_are_contiguous_normalized_tokens() -> None:
    assert exact_term_matches("Von\u00a0Willebrand", "Von Willebrand factor")
    assert exact_term_matches("Wiskott-Aldrich", "Wiskott Aldrich syndrome")
    assert not exact_term_matches("von factor", "von Willebrand factor")
    assert not exact_term_matches("ACE", "GRACE")


def test_pollution_boundary_and_transitive_clusters() -> None:
    rows = [
        {"semantic_score": 0.5, "in_scope": False, "deck": "A", "tag_root": "x"} for _ in range(26)
    ]
    assert pollution_diagnostic(rows).polluted
    assert COSINE_CLUSTER_THRESHOLD == 0.88
    clustered = cluster_note_ids(
        [
            {"note_id": 1, "content_sha256": "a"},
            {"note_id": 2, "content_sha256": "b"},
            {"note_id": 3, "content_sha256": "c"},
        ],
        vectors={1: (1.0, 0.0), 2: (0.9, 0.435), 3: (0.7, 0.714)},
    )
    assert clustered == ((1, 2, 3),)


def test_frozen_config_hashes_every_critical_group_with_unicode_json() -> None:
    config = frozen_config_payload()
    baseline = canonical_sha256(config)
    assert canonical_sha256({"word": "é"}) != canonical_sha256({"word": "e"})
    for key in (
        "semantic_variant_weights",
        "raw_limit",
        "query_variant_limit",
        "query_character_limit",
        "semantic_threshold",
        "rrf_k",
        "rrf_floor",
        "pollution_ceiling",
        "pollution_ratio",
        "per_fact_limit",
        "global_unique_limit",
        "boost_parameters",
        "cosine_cluster_threshold",
        "tag_mode_version",
    ):
        changed = dict(config)
        changed[key] = ["changed"] if isinstance(changed[key], list) else "changed"
        assert canonical_sha256(changed) != baseline


def test_tag_modes_and_mixed_pollution_lanes_are_independent() -> None:
    assert effective_tag_mode("hard_filter", census_trusted=True) == "hard_filter"
    assert effective_tag_mode("hard_filter", census_trusted=False) == "prior_boost"
    assert deck_and_tag_eligible("target_tagged", mode="hard_filter")
    assert not deck_and_tag_eligible("untagged", mode="hard_filter")
    for mode in ("prior_boost", "disabled"):
        assert deck_and_tag_eligible("other_system_excluded", mode=mode)
        assert deck_and_tag_eligible("untagged", mode=mode)
    polluted = pollution_diagnostic(
        [{"semantic_score": 0.5, "in_scope": False, "deck": "B", "tag_root": "z"}] * 25
    )
    clean = pollution_diagnostic([{"semantic_score": 0.9, "in_scope": True}])
    assert polluted.polluted and polluted.dominant_pattern == ("B", "z") and not clean.polluted
    rows = hybrid_rank_fusion(
        {"polluted": (1,), "clean": (1,)}, (), variant_weights={"polluted": 0.0, "clean": 0.8}
    )
    assert rows[0].note_id == 1 and rows[0].base_rrf > 0
    assert hybrid_rank_fusion({"off": (1,)}, (), variant_weights={"off": 0.0}) == ()
    threshold_rows = [{"semantic_score": 0.6, "in_scope": False}] * 25
    assert pollution_diagnostic(threshold_rows, threshold=0.7).above_threshold_count == 0
