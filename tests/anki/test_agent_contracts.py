from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from oms_hub.anki.contracts import (
    ActionEnvelope,
    AddNotesOperation,
    AddTagsOperation,
    AgentCommand,
    AgentHeartbeat,
    EnvelopeReceipt,
    MediaFetchRequest,
    MediaUpload,
    OperationReceipt,
    SnapshotDelta,
    SnapshotManifest,
    SnapshotNote,
    StoreMediaOperation,
    SyncOperation,
    VerifyOperation,
    canonical_payload_sha256,
)
from oms_hub.anki.domain import AgentCommandType


def test_agent_command_round_trips_and_forbids_extra_fields() -> None:
    payload = {"reason": "manual full reconciliation"}
    command = AgentCommand(
        command_id=UUID("11768ac8-ff59-4732-b6f6-aeebfbc88841"),
        command_type=AgentCommandType.FULL_SNAPSHOT,
        payload=payload,
        payload_sha256=canonical_payload_sha256(payload),
        created_at=datetime(2026, 7, 27, 15, 0, tzinfo=UTC),
    )

    restored = AgentCommand.model_validate_json(command.model_dump_json())

    assert restored == command
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AgentCommand.model_validate(
            {
                **command.model_dump(mode="json"),
                "unexpected_secret": "must-not-be-accepted",
            }
        )


def test_snapshot_contract_round_trips_with_note_and_delta_hashes() -> None:
    note_payload = {
        "note_id": 1479430487028,
        "model_name": "AnKingOverhaul",
        "fields": {"Text": "{{c1::anemia}}", "Extra": "Explanation"},
        "tags": ["AnkiHub_Optional::LMU_OMS_II"],
        "card_ids": [1479430487029],
        "media": ["anemia.png"],
    }
    note = SnapshotNote(
        **note_payload,
        content_sha256=canonical_payload_sha256(note_payload),
    )
    manifest_payload = {
        "snapshot_id": "snapshot-20260727",
        "source_deck": "Anking Step Deck",
        "note_count": 1,
        "id_set_sha256": "a" * 64,
        "content_sha256": "b" * 64,
        "export_version": "1",
        "producer_version": "0.1.0",
        "ankiconnect_version": 6,
        "exported_at": "2026-07-27T15:00:00Z",
    }
    manifest = SnapshotManifest(
        **manifest_payload,
        payload_sha256=canonical_payload_sha256(manifest_payload),
    )
    delta_payload = {
        "manifest": manifest.model_dump(mode="json"),
        "upserts": [note.model_dump(mode="json")],
        "deleted_note_ids": [],
    }
    delta = SnapshotDelta(
        manifest=manifest,
        upserts=(note,),
        deleted_note_ids=(),
        payload_sha256=canonical_payload_sha256(delta_payload),
    )

    restored = SnapshotDelta.model_validate_json(delta.model_dump_json())

    assert restored == delta
    assert restored.upserts[0].note_id == 1479430487028


def test_media_contracts_reject_path_components_and_bad_hashes() -> None:
    with pytest.raises(ValidationError, match="filename"):
        MediaFetchRequest(
            command_id=UUID("11768ac8-ff59-4732-b6f6-aeebfbc88841"),
            filenames=("../collection.anki2",),
            max_bytes=1024,
        )
    with pytest.raises(ValidationError, match="sha256"):
        MediaUpload(
            command_id=UUID("11768ac8-ff59-4732-b6f6-aeebfbc88841"),
            filename="anemia.png",
            mime_type="image/png",
            content_base64="aGVsbG8=",
            byte_count=5,
            sha256="not-a-hash",
        )


def test_action_envelope_and_receipt_round_trip() -> None:
    operations = (
        StoreMediaOperation(
            operation_id=UUID("46b479b2-e574-4bb8-a8d0-0b58170df646"),
            filename="oms_anki_0123456789abcdef.png",
            content_base64="aGVsbG8=",
            sha256="a" * 64,
            content_sha256="b" * 64,
        ),
        AddTagsOperation(
            operation_id=UUID("12dbcf36-32fc-42a2-979d-31096ab4f413"),
            note_ids=(1479430487028,),
            tag="AnkiHub_Optional::LMU_OMS_II::HemeLymph::Block1::Lec4_Anemia_I",
            content_sha256="c" * 64,
        ),
        AddNotesOperation(
            operation_id=UUID("2867f393-0ae3-4935-bc85-55bfdbfbec82"),
            notes=(
                {
                    "deckName": "OMS-II_Custom_Cards::Heme_Lymph::Exam_1::Lec4_Anemia_I",
                    "modelName": "AnKingOverhaul (OMS_II_Extra/JCBrooks)",
                    "fields": {"Text": "{{c1::Anemia}}", "Extra": "Explanation"},
                },
            ),
            content_sha256="d" * 64,
        ),
        SyncOperation(
            operation_id=UUID("10d845ad-4837-4992-b6d5-ed2fc3855672"),
            content_sha256="e" * 64,
        ),
        VerifyOperation(
            operation_id=UUID("8f58e8c9-3e80-4d78-a879-c07243e56a88"),
            note_ids=(1479430487028,),
            content_sha256="f" * 64,
        ),
    )
    envelope_payload = {
        "envelope_id": "0a0de74a-a60b-41e3-808e-e89974b0f615",
        "snapshot_id": "snapshot-20260727",
        "target_deck": "OMS-II_Custom_Cards::Heme_Lymph::Exam_1::Lec4_Anemia_I",
        "target_tag": (
            "AnkiHub_Optional::LMU_OMS_II::HemeLymph::Block1::Lec4_Anemia_I"
        ),
        "touched_note_hashes": {"1479430487028": "1" * 64},
        "operations": [operation.model_dump(mode="json") for operation in operations],
    }
    envelope = ActionEnvelope(
        envelope_id=UUID(envelope_payload["envelope_id"]),
        snapshot_id=str(envelope_payload["snapshot_id"]),
        target_deck=str(envelope_payload["target_deck"]),
        target_tag=str(envelope_payload["target_tag"]),
        touched_note_hashes={1479430487028: "1" * 64},
        operations=operations,
        payload_sha256=canonical_payload_sha256(envelope_payload),
    )
    receipt_payload = {
        "envelope_id": str(envelope.envelope_id),
        "agent_id": "connor-mac",
        "operations": [
            {
                "operation_id": str(operation.operation_id),
                "status": "complete",
                "result": {},
                "error": None,
            }
            for operation in operations
        ],
        "sync_status": "complete",
        "verified": True,
        "created_note_ids": [1556732827182],
        "media_filenames": ["oms_anki_0123456789abcdef.png"],
        "safe_error": None,
    }
    receipt = EnvelopeReceipt(
        envelope_id=envelope.envelope_id,
        agent_id="connor-mac",
        operations=tuple(
            OperationReceipt(
                operation_id=operation.operation_id,
                status="complete",
                result={},
            )
            for operation in operations
        ),
        sync_status="complete",
        verified=True,
        created_note_ids=(1556732827182,),
        media_filenames=("oms_anki_0123456789abcdef.png",),
        payload_sha256=canonical_payload_sha256(receipt_payload),
    )

    assert ActionEnvelope.model_validate_json(envelope.model_dump_json()) == envelope
    assert EnvelopeReceipt.model_validate_json(receipt.model_dump_json()) == receipt


def test_action_envelope_rejects_unsafe_tag_chunks_and_operation_order() -> None:
    with pytest.raises(ValidationError, match="1000"):
        AddTagsOperation(
            operation_id=UUID("12dbcf36-32fc-42a2-979d-31096ab4f413"),
            note_ids=tuple(range(1, 1_002)),
            tag="AnkiHub_Optional::LMU_OMS_II",
            content_sha256="c" * 64,
        )
    add_tags = AddTagsOperation(
        operation_id=UUID("12dbcf36-32fc-42a2-979d-31096ab4f413"),
        note_ids=(1479430487028,),
        tag="AnkiHub_Optional::LMU_OMS_II",
        content_sha256="c" * 64,
    )
    sync = SyncOperation(
        operation_id=UUID("10d845ad-4837-4992-b6d5-ed2fc3855672"),
        content_sha256="e" * 64,
    )
    verify = VerifyOperation(
        operation_id=UUID("8f58e8c9-3e80-4d78-a879-c07243e56a88"),
        note_ids=(1479430487028,),
        content_sha256="f" * 64,
    )

    with pytest.raises(ValidationError, match="order"):
        ActionEnvelope(
            envelope_id=UUID("0a0de74a-a60b-41e3-808e-e89974b0f615"),
            snapshot_id="snapshot-1",
            target_deck="custom-deck",
            target_tag="lecture-tag",
            touched_note_hashes={1479430487028: "1" * 64},
            operations=(sync, add_tags, verify),
            payload_sha256="2" * 64,
        )


def test_heartbeat_contract_is_strict() -> None:
    heartbeat = AgentHeartbeat(
        agent_id="connor-mac",
        agent_version="0.1.0",
        anki_version="25.02",
        ankiconnect_version=6,
        active_snapshot_id=None,
        health="ok",
        observed_at=datetime(2026, 7, 27, 15, 0, tzinfo=UTC),
    )

    assert heartbeat.contract_version == 1
    with pytest.raises(ValidationError):
        AgentHeartbeat.model_validate(
            {**heartbeat.model_dump(), "health": "contains-token-secret"}
        )
