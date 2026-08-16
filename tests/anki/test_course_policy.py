import json
from pathlib import Path

import pytest
from sqlalchemy import text

from oms_hub.anki.course_policy import CourseCurationPolicy, PolicyEmphasisColor
from oms_hub.anki.repository import AnkiCurationRepository
from oms_hub.db import Database


def _policy(
    *, revision: int = 1, instruction: str = "Prioritize red text."
) -> CourseCurationPolicy:
    return CourseCurationPolicy(
        policy_id="heme-lymph",
        revision=revision,
        course_id="oms-ii",
        course_label="OMS II",
        professor_label="Professor",
        scope_instruction=instruction,
        emphasis_mode="colored_text",
        emphasis_colors=(PolicyEmphasisColor(rgb="FF0000", label="red"),),
        missing_emphasis_fallback="block",
        tag_scope_mode="hard_filter",
        classification_strictness="strict",
        generation_style_profile="cloze",
        ordinary_cost_limit_microusd=500_000,
        hard_stop_cost_limit_microusd=10_000_000,
    )


def test_policy_hash_is_stable_and_bound_to_payload() -> None:
    policy = _policy()
    assert policy.policy_sha256 == _policy().policy_sha256
    assert policy.policy_sha256 != _policy(instruction="Different scope.").policy_sha256
    with pytest.raises(ValueError, match="hash"):
        CourseCurationPolicy.model_validate({**policy.model_dump(), "policy_sha256": "0" * 64})


def test_policy_canonicalizes_colors_and_rejects_unused_or_unordered_colors() -> None:
    color = PolicyEmphasisColor(rgb=" ff0000 ", theme_ref=" accent ", label=" red ")
    assert (color.rgb, color.theme_ref, color.label) == ("FF0000", "accent", "red")
    with pytest.raises(ValueError, match="require"):
        _policy().model_copy(update={"emphasis_colors": ()})._validate_identity_and_hash()
    with pytest.raises(ValueError, match="non-colored"):
        _policy().model_copy(
            update={"emphasis_mode": "outline_depth"}
        )._validate_identity_and_hash()


def test_policy_revision_is_append_only_and_exactly_idempotent(tmp_path: Path) -> None:
    with Database(f"sqlite:///{tmp_path / 'policy.db'}") as database:
        database.migrate()
        repository = AnkiCurationRepository(database)
        policy = _policy()
        assert repository.create_policy_revision(policy) == policy
        assert repository.create_policy_revision(policy) == policy
        with pytest.raises(ValueError, match="already exists"):
            repository.create_policy_revision(_policy(instruction="Different scope."))
        assert repository.list_policy_revisions("heme-lymph") == (policy,)
        assert repository.get_policy_revision("heme-lymph", 1) == policy


@pytest.mark.parametrize(
    "payload_update", ({"policy_id": " heme-lymph "}, {"policy_sha256": "x" * 64})
)
def test_policy_read_rejects_canonical_payload_drift(
    tmp_path: Path, payload_update: dict[str, str]
) -> None:
    with Database(f"sqlite:///{tmp_path / 'policy-read.db'}") as database:
        database.migrate()
        repository = AnkiCurationRepository(database)
        policy = _policy()
        repository.create_policy_revision(policy)
        payload = {**policy.canonical_payload(), **payload_update}
        with database.engine.begin() as connection:
            connection.execute(
                text("UPDATE course_curation_policy SET payload_json = :payload"),
                {"payload": json.dumps(payload, sort_keys=True, separators=(",", ":"))},
            )
        with pytest.raises(ValueError, match="canonical integrity"):
            repository.get_policy_revision(policy.policy_id, policy.revision)
