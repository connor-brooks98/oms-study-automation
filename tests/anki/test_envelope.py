from uuid import UUID

import pytest

from oms_hub.anki.domain import ReviewChangeSet, TagPatch
from oms_hub.anki.envelope import (
    CurrentCollectionNote,
    EnvelopeBuilder,
    field_hash,
)
from oms_hub.anki.gaps import GapCardProposal
from oms_hub.anki.tag_policy import (
    StaleTagPatch,
    TagPolicy,
    TagPolicyError,
    tag_hash,
)
from oms_hub.llm.domain import ProviderName

ENVELOPE_ID = UUID("5dc4f15e-df92-4a32-964e-026b5d518a80")
TARGET_TAG = "AnkiHub_Optional::LMU_OMS_II::Heme::Lecture_3"


def _policy() -> TagPolicy:
    return TagPolicy(
        pipeline_owned_roots=("OMS",),
        approved_optional_roots=("AnkiHub_Optional::LMU_OMS_II",),
        source_managed_roots=("#Pathoma", "#AK_Step"),
        version="tags-v1",
    )


def _current_note() -> CurrentCollectionNote:
    return CurrentCollectionNote(
        note_id=42,
        fields={
            "Text": "{{c1::Iron deficiency}} causes microcytic anemia.",
            "Extra": "Ferritin is low.",
        },
        tags=("#Pathoma::Hematology", "OMS::Old"),
    )


def _proposal(
    *,
    initial_tags: tuple[str, ...] = ("OMS::Generated",),
) -> GapCardProposal:
    return GapCardProposal(
        concept_id="iron-absorption",
        note_type="Cloze",
        fields={
            "Text": "Iron is absorbed in the {{c1::duodenum}}.",
            "Extra": "Lecture slide 12.",
        },
        source_refs=(),
        evidence_ids=("slide-12",),
        initial_tags=initial_tags,
        provider=ProviderName.ANTHROPIC,
        model="claude-sonnet",
        prompt_version="gap-v1",
        confidence=0.97,
        content_hash="a" * 64,
        provenance={},
    )


def _changeset(note: CurrentCollectionNote) -> ReviewChangeSet:
    after = (
        "#Pathoma::Hematology",
        "OMS::Reviewed",
    )
    return ReviewChangeSet(
        expected_revision=3,
        candidate_selections={note.note_id: True},
        tag_patches=(
            TagPatch(
                note_id=note.note_id,
                before=note.tags,
                after=after,
                add_tags=("OMS::Reviewed",),
                remove_tags=("OMS::Old",),
                expected_tag_hash=tag_hash(note.tags),
                tag_policy_version="tags-v1",
            ),
        ),
    )


def _build(
    note: CurrentCollectionNote,
    *,
    proposal: GapCardProposal | None = None,
):
    return EnvelopeBuilder(_policy()).build(
        _changeset(note),
        {note.note_id: note},
        envelope_id=ENVELOPE_ID,
        snapshot_id="snapshot-1",
        target_deck="OMS::Heme::Lecture 3",
        target_tag=TARGET_TAG,
        generated_cards=() if proposal is None else (proposal,),
    )


def test_envelope_is_deterministic_ordered_and_self_contained() -> None:
    note = _current_note()

    first = _build(note, proposal=_proposal())
    second = _build(note, proposal=_proposal())

    assert first == second
    assert [operation.operation_type for operation in first.operations] == [
        "remove_tags",
        "add_tags",
        "add_tags",
        "add_notes",
        "sync",
        "verify",
    ]
    assert len({operation.operation_id for operation in first.operations}) == len(first.operations)
    assert first.touched_note_hashes == {
        note.note_id: field_hash(note.fields),
    }
    assert first.expected_tag_hashes == {
        note.note_id: tag_hash(note.tags),
    }
    assert set(first.expected_note_tags[note.note_id]) == {
        "#Pathoma::Hematology",
        "OMS::Reviewed",
        TARGET_TAG,
    }

    add_notes = next(
        operation for operation in first.operations if operation.operation_type == "add_notes"
    )
    generated = add_notes.notes[0]  # type: ignore[union-attr]
    assert generated["deckName"] == "OMS::Heme::Lecture 3"
    assert generated["fields"] == _proposal().fields
    assert TARGET_TAG in generated["tags"]
    assert "OMS::Generated" in generated["tags"]
    marker_tags = [tag for tag in generated["tags"] if tag.startswith("OMS::Curation::Envelope_")]
    assert len(marker_tags) == 1

    existing_note_tag_operations = [
        operation
        for operation in first.operations
        if operation.operation_type in {"add_tags", "remove_tags"}
    ]
    assert all(
        operation.note_ids == (note.note_id,)  # type: ignore[union-attr]
        for operation in existing_note_tag_operations
    )


def test_v2_envelope_binds_card_centric_job_and_reconciliation() -> None:
    note = _current_note()
    envelope = EnvelopeBuilder(_policy()).build_v2(
        _changeset(note),
        {note.note_id: note},
        envelope_id=ENVELOPE_ID,
        snapshot_id="snapshot-1",
        target_deck="OMS::Heme::Lecture 3",
        target_tag=TARGET_TAG,
        job_id=UUID("924ab797-23ac-4f14-a622-ded77fe8d701"),
        model_config_sha256="b" * 64,
        reconciliation_contract_version="card_centric_s9_v1",
        review_revision=3,
        overflow_acknowledgement_provenance={"required": False},
    )

    assert envelope.contract_version == 2
    assert envelope.pipeline_contract_version == "card_centric_v1"
    assert envelope.payload_sha256 != "0" * 64


def test_v2_envelope_preserves_the_v2_pipeline_contract() -> None:
    note = _current_note()
    envelope = EnvelopeBuilder(_policy()).build_v2(
        _changeset(note),
        {note.note_id: note},
        envelope_id=ENVELOPE_ID,
        snapshot_id="snapshot-1",
        target_deck="OMS::Heme::Lecture 3",
        target_tag=TARGET_TAG,
        job_id=UUID("924ab797-23ac-4f14-a622-ded77fe8d701"),
        pipeline_contract_version="card_centric_v2",
        model_config_sha256="b" * 64,
        reconciliation_contract_version="card_centric_s9_v1",
        review_revision=3,
        overflow_acknowledgement_provenance={"required": False},
    )

    assert envelope.pipeline_contract_version == "card_centric_v2"


def test_operation_ids_change_when_the_canonical_payload_changes() -> None:
    note = _current_note()
    first = _build(note, proposal=_proposal())
    changed = _proposal(initial_tags=("OMS::Generated", "OMS::HighYield"))
    second = _build(note, proposal=changed)
    first_add = next(
        operation for operation in first.operations if operation.operation_type == "add_notes"
    )
    second_add = next(
        operation for operation in second.operations if operation.operation_type == "add_notes"
    )

    assert first_add.operation_id != second_add.operation_id
    assert first_add.content_sha256 != second_add.content_sha256


def test_envelope_rejects_stale_patches_and_unapproved_generated_tags() -> None:
    note = _current_note()
    stale = CurrentCollectionNote(
        note_id=note.note_id,
        fields=note.fields,
        tags=(*note.tags, "OMS::ChangedElsewhere"),
    )

    with pytest.raises(StaleTagPatch):
        EnvelopeBuilder(_policy()).build(
            _changeset(note),
            {stale.note_id: stale},
            envelope_id=ENVELOPE_ID,
            snapshot_id="snapshot-1",
            target_deck="OMS::Heme::Lecture 3",
            target_tag=TARGET_TAG,
        )
    with pytest.raises(TagPolicyError, match="non-editable"):
        _build(
            note,
            proposal=_proposal(initial_tags=("#Pathoma::Hematology",)),
        )
