import math

import pytest

from oms_hub.anki.domain import CurationStage
from oms_hub.anki.prompts import AnkiPromptLibrary
from oms_hub.anki.replay_identity import (
    build_resolved_stage_model_identity,
    prompt_snapshot_identity,
)


def _identity(tmp_path, **changes):
    prompt_path = tmp_path / "prompts"
    prompt_path.mkdir(exist_ok=True)
    (prompt_path / "classifier.md").write_text(
        '---\nid: classifier\nversion: "2.0"\n---\n\nPinned instruction.',
        encoding="utf-8",
    )
    prompt = AnkiPromptLibrary(prompt_path).load("classifier")
    values = {
        "stage": CurationStage.CARD_CLASSIFY,
        "prompts": (prompt,),
        "provider": "anthropic",
        "model": "claude-test",
        "generation_parameters": {"temperature": 0, "max_tokens": 800},
        "batch_size": 30,
        "concurrency": 2,
    }
    values.update(changes)
    return build_resolved_stage_model_identity(**values)


def test_prompt_identity_is_immutable_after_asset_edit(tmp_path) -> None:
    identity = _identity(tmp_path)
    prompt = identity.prompts[0]
    (tmp_path / "prompts" / "classifier.md").write_text(
        '---\nid: classifier\nversion: "2.1"\n---\n\nChanged instruction.',
        encoding="utf-8",
    )

    changed_prompt = AnkiPromptLibrary(tmp_path / "prompts").load("classifier")
    changed = build_resolved_stage_model_identity(
        stage=CurationStage.CARD_CLASSIFY,
        prompts=(changed_prompt,),
        provider="anthropic",
        model="claude-test",
        generation_parameters={"temperature": 0, "max_tokens": 800},
        batch_size=30,
        concurrency=2,
    )

    assert prompt.content == "Pinned instruction."
    assert prompt.content_sha256 != changed_prompt.content_sha256
    assert identity.identity_sha256 != changed.identity_sha256


@pytest.mark.parametrize(
    ("changes",),
    [
        ({"provider": "openai"},),
        ({"model": "gpt-test"},),
        ({"generation_parameters": {"temperature": 0.1, "max_tokens": 800}},),
        ({"batch_size": 31},),
        ({"concurrency": 3},),
    ],
)
def test_every_supplied_resolved_model_field_changes_identity(tmp_path, changes) -> None:
    assert _identity(tmp_path).identity_sha256 != _identity(tmp_path, **changes).identity_sha256


def test_identity_uses_canonical_finite_json_and_exact_prompt_hash(tmp_path) -> None:
    first = _identity(
        tmp_path,
        generation_parameters={"max_tokens": 800, "temperature": 0},
    )
    second = _identity(
        tmp_path,
        generation_parameters={"temperature": 0, "max_tokens": 800},
    )

    assert first.identity_sha256 == second.identity_sha256
    assert first.prompts[0] == prompt_snapshot_identity(
        AnkiPromptLibrary(tmp_path / "prompts").load("classifier")
    )
    with pytest.raises(ValueError, match="finite JSON"):
        _identity(tmp_path, generation_parameters={"temperature": math.nan})


def test_persisted_batch_and_concurrency_may_be_supplied_in_parameters(tmp_path) -> None:
    identity = _identity(
        tmp_path,
        batch_size=None,
        concurrency=None,
        generation_parameters={
            "temperature": 0,
            "max_tokens": 800,
            "batch_size": 30,
            "concurrency": 2,
        },
    )

    assert identity.generation_parameters.as_dict()["batch_size"] == 30
    assert identity.generation_parameters.as_dict()["concurrency"] == 2
