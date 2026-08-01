import pytest
from pydantic import ValidationError

from oms_hub.anki.contracts import CreateCurationJobRequest, TagPatchContract


def _job_payload() -> dict[str, object]:
    return {
        "lecture_id": 7,
        "block_id": "heme-block-1",
        "source_revision_ids": [101, 102],
        "deck_allowlist": ["AnKing Step Deck"],
        "tag_allowlist": ["#AK_Step2_v12::Hematology"],
        "target_deck": "OMS-II_Custom_Cards::Heme::Lecture_4",
        "target_tag": "OMS::Heme::Lecture_4",
        "index_snapshot_id": "snapshot-1",
        "instruction_text": "Focus on lecture-emphasized mechanisms.",
        "lcl_prompt_version": "lcl-v4",
        "judgment_rubric_version": "judgment-v4",
        "gap_prompt_version": "gap-v4",
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "summary_outline_id": 91,
        "summary_outline_sha256": "b" * 64,
    }


def test_create_job_rejects_amboss_input() -> None:
    payload = _job_payload()
    payload["amboss_input"] = "legacy text"

    with pytest.raises(ValidationError):
        CreateCurationJobRequest.model_validate(payload)


def test_create_job_normalizes_scope_lists() -> None:
    payload = _job_payload()
    payload["deck_allowlist"] = [
        " AnKing Step Deck ",
        "Sketchy Pepper",
        "anking step deck",
    ]
    payload["tag_allowlist"] = ["#AK_Step2_v12::Hematology", "  "]

    request = CreateCurationJobRequest.model_validate(payload)

    assert request.deck_allowlist == ("AnKing Step Deck", "Sketchy Pepper")
    assert request.tag_allowlist == ("#AK_Step2_v12::Hematology",)
    assert request.source_revision_ids == (101, 102)
    assert request.summary_outline_id == 91
    assert request.summary_outline_sha256 == "b" * 64


def test_create_job_requires_complete_summary_pin() -> None:
    payload = _job_payload()
    payload["summary_outline_sha256"] = None

    with pytest.raises(ValidationError, match="summary outline"):
        CreateCurationJobRequest.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "unsupported"),
        ("source_revision_ids", []),
        ("deck_allowlist", []),
        ("target_tag", "unsafe tag"),
    ],
)
def test_create_job_rejects_invalid_run_scope(
    field: str,
    value: object,
) -> None:
    payload = _job_payload()
    payload[field] = value

    with pytest.raises(ValidationError):
        CreateCurationJobRequest.model_validate(payload)


def test_tag_patch_round_trips_exact_diff() -> None:
    patch = TagPatchContract(
        note_id=42,
        before=("lecture::03", "source::ankihub"),
        after=("lecture::03", "review::high_yield", "source::ankihub"),
        add_tags=("review::high_yield",),
        remove_tags=(),
        expected_tag_hash="a" * 64,
        tag_policy_version="tag-policy-v1",
    )

    assert TagPatchContract.model_validate(patch.model_dump()) == patch


def test_tag_patch_rejects_inexact_or_conflicting_diff() -> None:
    with pytest.raises(ValidationError):
        TagPatchContract(
            note_id=42,
            before=("lecture::03",),
            after=("lecture::03",),
            add_tags=("review::high_yield",),
            remove_tags=("review::high_yield",),
            expected_tag_hash="a" * 64,
            tag_policy_version="tag-policy-v1",
        )


def test_tag_patch_contract_normalizes_surrounding_whitespace() -> None:
    patch = TagPatchContract(
        note_id=42,
        before=(" OMS::Old ",),
        after=("OMS::New",),
        add_tags=(" OMS::New ",),
        remove_tags=("OMS::Old",),
        expected_tag_hash="a" * 64,
        tag_policy_version="tags-v1",
    )

    assert patch.before == ("OMS::Old",)
    assert patch.add_tags == ("OMS::New",)
