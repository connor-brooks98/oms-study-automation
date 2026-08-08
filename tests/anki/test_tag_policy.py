import pytest

from oms_hub.anki.domain import TagPatch
from oms_hub.anki.tag_policy import (
    StaleTagPatch,
    TagPolicy,
    TagPolicyError,
    normalize_tag,
    tag_hash,
)


def _policy() -> TagPolicy:
    return TagPolicy(
        pipeline_owned_roots=("OMS",),
        approved_optional_roots=(
            "AnkiHub_Optional::LMU_OMS_II",
        ),
        source_managed_roots=(
            "#AK_Step",
            "#Pathoma",
            "AnkiHub_",
        ),
        version="tags-v1",
    )


def _patch(
    before: tuple[str, ...],
    after: tuple[str, ...],
    *,
    add: tuple[str, ...] = (),
    remove: tuple[str, ...] = (),
) -> TagPatch:
    return TagPatch(
        note_id=42,
        before=before,
        after=after,
        add_tags=add,
        remove_tags=remove,
        expected_tag_hash=tag_hash(before),
        tag_policy_version="tags-v1",
    )


def test_classifies_nested_tags_with_specific_root_precedence() -> None:
    policy = _policy()

    assert policy.classify("OMS::Generated::Heme") == "pipeline_owned"
    assert (
        policy.classify(
            "ankihub_optional::lmu_oms_ii::Heme::Lecture_3"
        )
        == "approved_optional"
    )
    assert policy.classify("#Pathoma::Hematology") == "source_managed"
    assert policy.classify("personal::favorite") == "unknown"


def test_tag_normalization_rejects_anki_unsafe_values() -> None:
    assert normalize_tag("  OMS::Generated  ") == "OMS::Generated"
    with pytest.raises(TagPolicyError):
        normalize_tag("OMS::has spaces")
    with pytest.raises(TagPolicyError):
        normalize_tag("OMS::::Broken")


def test_allows_exact_owned_and_optional_tag_changes() -> None:
    policy = _policy()
    before = (
        "#Pathoma::Hematology",
        "OMS::Old",
        "personal::favorite",
    )
    after = (
        "#Pathoma::Hematology",
        "OMS::New",
        "personal::favorite",
        "AnkiHub_Optional::LMU_OMS_II::Lecture_3",
    )
    patch = _patch(
        before,
        after,
        add=(
            "OMS::New",
            "AnkiHub_Optional::LMU_OMS_II::Lecture_3",
        ),
        remove=("OMS::Old",),
    )

    validated = policy.validate_tag_patch(before, patch)

    assert validated.add_tags == patch.add_tags
    assert validated.remove_tags == patch.remove_tags


@pytest.mark.parametrize(
    "removed",
    ["#Pathoma::Hematology", "personal::favorite"],
)
def test_rejects_removal_of_source_managed_or_unknown_tag(
    removed: str,
) -> None:
    policy = _policy()
    before = (removed, "OMS::Generated")
    patch = _patch(
        before,
        ("OMS::Generated",),
        remove=(removed,),
    )

    with pytest.raises(TagPolicyError, match="remove"):
        policy.validate_tag_patch(before, patch)


def test_rejects_inexact_unchanged_and_stale_patches() -> None:
    policy = _policy()
    before = ("OMS::Old",)
    inexact = _patch(
        before,
        ("OMS::New",),
        add=("OMS::New",),
    )
    unchanged = _patch(before, before)
    stale = _patch(
        before,
        ("OMS::New",),
        add=("OMS::New",),
        remove=("OMS::Old",),
    )

    with pytest.raises(TagPolicyError, match="exact"):
        policy.validate_tag_patch(before, inexact)
    with pytest.raises(TagPolicyError, match="no changes"):
        policy.validate_tag_patch(before, unchanged)
    with pytest.raises(StaleTagPatch):
        policy.validate_tag_patch(("OMS::ChangedElsewhere",), stale)


def test_generated_note_initial_tags_use_same_policy() -> None:
    policy = _policy()

    assert policy.validate_initial_tags(
        (
            "OMS::Generated",
            "AnkiHub_Optional::LMU_OMS_II::Lecture_3",
        )
    ) == (
        "AnkiHub_Optional::LMU_OMS_II::Lecture_3",
        "OMS::Generated",
    )
    with pytest.raises(TagPolicyError):
        policy.validate_initial_tags(("#Pathoma::Hematology",))
