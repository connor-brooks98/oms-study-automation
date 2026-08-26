import asyncio
from collections.abc import Sequence
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from oms_hub.anki.ankiconnect import (
    AnkiConnectActionError,
    AnkiConnectUnavailable,
)
from oms_hub.anki.apply import (
    ApplyCoordinator,
    InMemoryApplyStore,
)
from oms_hub.anki.contracts import (
    ActionEnvelope,
    ActionEnvelopeV2,
    canonical_payload_sha256,
)
from oms_hub.anki.domain import ApplyState, ReviewChangeSet, TagPatch
from oms_hub.anki.envelope import CurrentCollectionNote, EnvelopeBuilder
from oms_hub.anki.gaps import GapCardProposal
from oms_hub.anki.runtime import AnkiPreflight
from oms_hub.anki.tag_policy import TagPolicy, tag_hash
from oms_hub.llm.domain import ProviderName

ENVELOPE_ID = UUID("5dc4f15e-df92-4a32-964e-026b5d518a80")
TARGET_TAG = "AnkiHub_Optional::LMU_OMS_II::Heme::Lecture_3"


class FakeGateway:
    def __init__(self) -> None:
        self.notes: dict[int, dict[str, Any]] = {
            42: {
                "noteId": 42,
                "modelName": "AnKingOverhaul",
                "fields": {
                    "Text": {
                        "value": "{{c1::Iron deficiency}} causes anemia.",
                        "order": 0,
                    },
                    "Extra": {"value": "Ferritin is low.", "order": 1},
                },
                "tags": ["#Pathoma::Hematology", "OMS::Old"],
                "cards": [1_042],
            }
        }
        self.next_note_id = 100
        self.sync_calls = 0
        self.notes_info_calls = 0
        self.find_notes_calls = 0
        self.mutation_calls: list[str] = []
        self.sync_failures: dict[int, Exception] = {}
        self.mutation_failures: dict[str, Exception] = {}
        self.ignore_mutation: set[str] = set()
        self.reject_generated_duplicates = False
        self.model_fields = ["Text", "Extra"]
        self.created_model_name: str | None = None
        self.media: dict[str, str] = {}

    async def store_media_file(self, filename: str, data_base64: str) -> str:
        self.mutation_calls.append("store_media")
        failure = self.mutation_failures.get("store_media")
        if failure is not None:
            raise failure
        self.media[filename] = data_base64
        return filename

    async def sync(self) -> None:
        self.sync_calls += 1
        failure = self.sync_failures.get(self.sync_calls)
        if failure is not None:
            raise failure

    async def notes_info(
        self,
        note_ids: Sequence[int],
    ) -> list[dict[str, Any]]:
        self.notes_info_calls += 1
        return [self.notes[note_id] for note_id in note_ids if note_id in self.notes]

    async def find_notes(self, query: str) -> list[int]:
        self.find_notes_calls += 1
        assert query.startswith("tag:")
        marker = query.removeprefix("tag:")
        return [note_id for note_id, note in self.notes.items() if marker in note["tags"]]

    async def model_field_names(self, model_name: str) -> list[str]:
        del model_name
        return list(self.model_fields)

    async def remove_tags(
        self,
        note_ids: Sequence[int],
        tags: Sequence[str],
    ) -> None:
        await self._mutate("remove_tags", note_ids, tags)

    async def add_tags(
        self,
        note_ids: Sequence[int],
        tags: Sequence[str],
    ) -> None:
        await self._mutate("add_tags", note_ids, tags)

    async def _mutate(
        self,
        kind: str,
        note_ids: Sequence[int],
        tags: Sequence[str],
    ) -> None:
        self.mutation_calls.append(kind)
        failure = self.mutation_failures.get(kind)
        if failure is not None:
            raise failure
        if kind in self.ignore_mutation:
            return
        for note_id in note_ids:
            current = list(self.notes[note_id]["tags"])
            if kind == "remove_tags":
                current = [
                    tag
                    for tag in current
                    if tag.casefold() not in {value.casefold() for value in tags}
                ]
            else:
                known = {tag.casefold() for tag in current}
                current.extend(tag for tag in tags if tag.casefold() not in known)
            self.notes[note_id]["tags"] = current

    async def add_notes(
        self,
        notes: Sequence[dict[str, Any]],
    ) -> list[int | None]:
        self.mutation_calls.append("add_notes")
        failure = self.mutation_failures.get("add_notes")
        if failure is not None:
            raise failure
        created: list[int] = []
        for note in notes:
            if self.reject_generated_duplicates:
                return [None for _ in notes]
            note_id = self.next_note_id
            self.next_note_id += 1
            self.notes[note_id] = {
                "noteId": note_id,
                "modelName": self.created_model_name or note["modelName"],
                "fields": {
                    name: {
                        "value": note["fields"].get(name, ""),
                        "order": order,
                    }
                    for order, name in enumerate(self.model_fields)
                },
                "tags": list(note["tags"]),
                "cards": [note_id + 1_000],
            }
            created.append(note_id)
        return created


class CrashOnceAfterResultStore(InMemoryApplyStore):
    def __init__(
        self,
        envelope: ActionEnvelope,
        operation_ids: set[UUID],
    ) -> None:
        super().__init__((envelope,))
        self.operation_ids = operation_ids
        self.crashed: set[UUID] = set()

    def complete_operation(
        self,
        envelope_id: UUID,
        operation_id: UUID,
        result: dict[str, Any],
    ) -> None:
        super().complete_operation(envelope_id, operation_id, result)
        if operation_id in self.operation_ids and operation_id not in self.crashed:
            self.crashed.add(operation_id)
            raise SimulatedProcessExit


class CrashOnceBeforeResultStore(InMemoryApplyStore):
    def __init__(
        self,
        envelope: ActionEnvelope,
        operation_id: UUID,
    ) -> None:
        super().__init__((envelope,))
        self.operation_id = operation_id
        self.crashed = False

    def complete_operation(
        self,
        envelope_id: UUID,
        operation_id: UUID,
        result: dict[str, Any],
    ) -> None:
        if operation_id == self.operation_id and not self.crashed:
            self.crashed = True
            raise SimulatedProcessExit
        super().complete_operation(envelope_id, operation_id, result)


class SimulatedProcessExit(RuntimeError):
    pass


class FakeRuntime:
    def __init__(self) -> None:
        self.preflight_calls = 0

    async def ensure_running(self) -> AnkiPreflight:
        self.preflight_calls += 1
        return AnkiPreflight(
            reachable=True,
            ankiconnect_version=6,
            active_profile="Main",
            collection_accessible=True,
            sync_available=True,
            blocking_reason=None,
        )


def _policy() -> TagPolicy:
    return TagPolicy(
        pipeline_owned_roots=("OMS",),
        approved_optional_roots=("AnkiHub_Optional::LMU_OMS_II",),
        source_managed_roots=("#Pathoma",),
        version="tags-v1",
    )


def _current(gateway: FakeGateway) -> CurrentCollectionNote:
    raw = gateway.notes[42]
    return CurrentCollectionNote(
        note_id=42,
        fields={name: str(value["value"]) for name, value in raw["fields"].items()},
        tags=tuple(raw["tags"]),
    )


def _proposal() -> GapCardProposal:
    return GapCardProposal(
        concept_id="iron-absorption",
        note_type="Cloze",
        fields={
            "Text": "Iron is absorbed in the {{c1::duodenum}}.",
            "Extra": "Lecture slide 12.",
        },
        source_refs=(),
        evidence_ids=("slide-12",),
        initial_tags=("OMS::Generated",),
        provider=ProviderName.ANTHROPIC,
        model="claude-sonnet",
        prompt_version="gap-v1",
        confidence=0.97,
        content_hash="a" * 64,
        provenance={},
    )


def _envelope(
    gateway: FakeGateway,
    *,
    generated: bool = False,
    media: bool = False,
) -> ActionEnvelope:
    note = _current(gateway)
    after = ("#Pathoma::Hematology", "OMS::Reviewed")
    changeset = ReviewChangeSet(
        expected_revision=1,
        candidate_selections={42: True},
        tag_patches=(
            TagPatch(
                note_id=42,
                before=note.tags,
                after=after,
                add_tags=("OMS::Reviewed",),
                remove_tags=("OMS::Old",),
                expected_tag_hash=tag_hash(note.tags),
                tag_policy_version="tags-v1",
            ),
        ),
    )
    proposal = _proposal()
    if media:
        proposal = replace(
            proposal,
            media=(
                {
                    "filename": "oms_anki_0123456789abcdef.png",
                    "content_base64": "aGVsbG8=",
                    "sha256": "a" * 64,
                },
            ),
        )
    return EnvelopeBuilder(_policy()).build(
        changeset,
        {42: note},
        envelope_id=ENVELOPE_ID,
        snapshot_id="snapshot-1",
        target_deck="OMS::Heme::Lecture 3",
        target_tag=TARGET_TAG,
        generated_cards=(proposal,) if generated else (),
    )


def _v2_envelope(gateway: FakeGateway) -> ActionEnvelopeV2:
    v1 = _envelope(gateway)
    payload = v1.model_dump(mode="json")
    payload.update(
        {
            "contract_version": 2,
            "job_id": "ab43d53a-55aa-4ef5-909b-0833b10254d7",
            "pipeline_contract_version": "card_centric_v1",
            "model_config_sha256": "a" * 64,
            "reconciliation_contract_version": "reconciliation-v1",
            "review_revision": 1,
            "overflow_acknowledgement_provenance": {"reviewer": "local"},
        }
    )
    v2 = ActionEnvelopeV2.model_validate(payload)
    return v2.model_copy(update={"payload_sha256": canonical_payload_sha256(v2)})


def test_leading_sync_failure_makes_zero_mutation_calls() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        envelope = _envelope(gateway)
        store = InMemoryApplyStore((envelope,))
        gateway.sync_failures[1] = AnkiConnectUnavailable("offline")

        result = await ApplyCoordinator(store, gateway).apply(envelope.envelope_id)

        assert result.state is ApplyState.FAILED_BEFORE_APPLY
        assert gateway.mutation_calls == []
        assert store.state(envelope.envelope_id) is ApplyState.FAILED_BEFORE_APPLY

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "supported_versions",
    [None, frozenset({1})],
    ids=["default-v1-only", "explicit-v1-only"],
)
def test_v2_without_capability_fails_before_preflight_or_gateway_activity(
    supported_versions: frozenset[int] | None,
) -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        runtime = FakeRuntime()
        v2 = _v2_envelope(gateway)
        store = InMemoryApplyStore((v2,))

        coordinator = (
            ApplyCoordinator(store, gateway, runtime=runtime)
            if supported_versions is None
            else ApplyCoordinator(
                store,
                gateway,
                runtime=runtime,
                supported_envelope_versions=supported_versions,
            )
        )
        result = await coordinator.apply(v2.envelope_id)

        assert result.state is ApplyState.FAILED_BEFORE_APPLY
        assert runtime.preflight_calls == 0
        assert gateway.sync_calls == 0
        assert gateway.notes_info_calls == 0
        assert gateway.find_notes_calls == 0
        assert gateway.mutation_calls == []

    asyncio.run(scenario())


def test_v2_with_explicit_capability_applies_idempotently() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        v2 = _v2_envelope(gateway)
        store = InMemoryApplyStore((v2,))
        coordinator = ApplyCoordinator(
            store, gateway, supported_envelope_versions=frozenset({1, 2})
        )
        assert (await coordinator.apply(v2.envelope_id)).state is ApplyState.COMPLETE
        calls = list(gateway.mutation_calls)
        assert (await coordinator.apply(v2.envelope_id)).state is ApplyState.COMPLETE
        assert gateway.mutation_calls == calls

    asyncio.run(scenario())


def test_tag_add_remove_and_read_back_verification_complete() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        envelope = _envelope(gateway)
        store = InMemoryApplyStore((envelope,))

        result = await ApplyCoordinator(store, gateway).apply(envelope.envelope_id)

        assert result.state is ApplyState.COMPLETE
        assert set(gateway.notes[42]["tags"]) == {
            "#Pathoma::Hematology",
            "OMS::Reviewed",
            TARGET_TAG,
        }
        assert gateway.sync_calls == 2
        assert result.differences == ()

    asyncio.run(scenario())


def test_generated_note_retry_discovers_marker_and_does_not_duplicate() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        envelope = _envelope(gateway, generated=True)
        add_notes = next(
            operation
            for operation in envelope.operations
            if operation.operation_type == "add_notes"
        )
        store = CrashOnceBeforeResultStore(
            envelope,
            add_notes.operation_id,
        )
        coordinator = ApplyCoordinator(store, gateway)

        with pytest.raises(SimulatedProcessExit):
            await coordinator.apply(envelope.envelope_id)
        assert len(gateway.notes) == 2

        result = await ApplyCoordinator(store, gateway).apply(envelope.envelope_id)

        assert result.state is ApplyState.COMPLETE
        assert len(gateway.notes) == 2
        assert gateway.mutation_calls.count("add_notes") == 1
        assert result.created_note_ids == (100,)

    asyncio.run(scenario())


def test_generated_media_is_stored_before_the_note() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        envelope = _envelope(gateway, generated=True, media=True)
        store = InMemoryApplyStore((envelope,))

        result = await ApplyCoordinator(store, gateway).apply(envelope.envelope_id)

        assert result.state is ApplyState.COMPLETE
        assert gateway.mutation_calls.index("store_media") < gateway.mutation_calls.index(
            "add_notes"
        )
        assert gateway.media == {
            "oms_anki_0123456789abcdef.png": "aGVsbG8="
        }

    asyncio.run(scenario())


def test_duplicate_rejected_generated_note_is_terminal_not_retryable() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        gateway.reject_generated_duplicates = True
        envelope = _envelope(gateway, generated=True)
        store = InMemoryApplyStore((envelope,))

        result = await ApplyCoordinator(store, gateway).apply(envelope.envelope_id)

        assert result.state is ApplyState.COMPLETE
        assert result.created_note_ids == ()
        assert result.rejected_duplicates == (
            {
                "position": 0,
                "status": "rejected_duplicate",
            },
        )
        add_notes = next(
            operation
            for operation in envelope.operations
            if operation.operation_type == "add_notes"
        )
        record = store.operation_record(
            envelope.envelope_id,
            add_notes.operation_id,
        )
        assert record.state == "complete"
        assert record.attempts == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("failure", "expected_state"),
    [
        (
            AnkiConnectUnavailable("network unavailable"),
            ApplyState.APPLIED_LOCAL_SYNC_RETRYABLE,
        ),
        (
            AnkiConnectActionError("sync rejected"),
            ApplyState.APPLIED_LOCAL_SYNC_BLOCKED,
        ),
    ],
)
def test_trailing_sync_failure_is_classified_and_can_resume(
    failure: Exception,
    expected_state: ApplyState,
) -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        envelope = _envelope(gateway)
        store = InMemoryApplyStore((envelope,))
        gateway.sync_failures[2] = failure

        first = await ApplyCoordinator(store, gateway).apply(envelope.envelope_id)

        assert first.state is expected_state
        assert set(gateway.notes[42]["tags"]) == set(envelope.expected_note_tags[42])
        mutation_count = len(gateway.mutation_calls)
        gateway.sync_failures.clear()

        resumed = await ApplyCoordinator(store, gateway).apply(envelope.envelope_id)

        assert resumed.state is ApplyState.COMPLETE
        assert len(gateway.mutation_calls) == mutation_count
        assert gateway.sync_calls == 3

    asyncio.run(scenario())


def test_partial_mutation_failure_records_local_changes_and_stops() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        envelope = _envelope(gateway)
        store = InMemoryApplyStore((envelope,))
        gateway.mutation_failures["add_tags"] = AnkiConnectActionError("tag rejected")

        result = await ApplyCoordinator(store, gateway).apply(envelope.envelope_id)

        assert result.state is ApplyState.APPLY_PARTIAL
        assert "add_tags" in (result.safe_error or "")
        assert "OMS::Old" not in gateway.notes[42]["tags"]
        assert gateway.sync_calls == 1

    asyncio.run(scenario())


def test_restart_after_every_durable_operation_is_idempotent() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        envelope = _envelope(gateway, generated=True)
        operation_ids = {operation.operation_id for operation in envelope.operations}
        store = CrashOnceAfterResultStore(envelope, operation_ids)

        for _ in envelope.operations:
            with pytest.raises(SimulatedProcessExit):
                await ApplyCoordinator(store, gateway).apply(envelope.envelope_id)
        result = await ApplyCoordinator(store, gateway).apply(envelope.envelope_id)

        assert result.state is ApplyState.COMPLETE
        assert gateway.mutation_calls.count("remove_tags") == 1
        assert gateway.mutation_calls.count("add_notes") == 1
        assert gateway.mutation_calls.count("add_tags") == 2
        assert len(gateway.notes) == 2

    asyncio.run(scenario())


def test_generated_extra_maps_to_back_extra_for_the_active_note_type() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        gateway.model_fields = ["Text", "Back Extra", "Keywords"]
        envelope = _envelope(gateway, generated=True)
        store = InMemoryApplyStore((envelope,))

        result = await ApplyCoordinator(store, gateway).apply(envelope.envelope_id)

        assert result.state is ApplyState.COMPLETE
        generated = gateway.notes[100]["fields"]
        assert generated["Back Extra"]["value"] == "Lecture slide 12."
        assert generated["Keywords"]["value"] == ""

    asyncio.run(scenario())


def test_generated_note_type_mismatch_fails_verification() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        gateway.created_model_name = "Basic"
        envelope = _envelope(gateway, generated=True)
        store = InMemoryApplyStore((envelope,))

        result = await ApplyCoordinator(store, gateway).apply(envelope.envelope_id)

        assert result.state is ApplyState.APPLY_PARTIAL
        assert any(difference["kind"] == "model" for difference in result.differences)

    asyncio.run(scenario())


@pytest.mark.parametrize("stale_part", ["fields", "tags"])
def test_stale_fields_or_tags_block_apply_before_any_mutation(
    stale_part: str,
) -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        envelope = _envelope(gateway)
        store = InMemoryApplyStore((envelope,))
        if stale_part == "fields":
            gateway.notes[42]["fields"]["Extra"]["value"] = "Changed elsewhere"
        else:
            gateway.notes[42]["tags"].append("personal::changed")

        result = await ApplyCoordinator(store, gateway).apply(envelope.envelope_id)

        assert result.state is ApplyState.FAILED_BEFORE_APPLY
        assert result.differences
        assert result.differences[0]["kind"] == stale_part
        assert gateway.mutation_calls == []

    asyncio.run(scenario())


def test_verification_mismatch_reports_expected_and_actual_values() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        envelope = _envelope(gateway)
        store = InMemoryApplyStore((envelope,))
        gateway.ignore_mutation.add("add_tags")

        result = await ApplyCoordinator(store, gateway).apply(envelope.envelope_id)

        assert result.state is ApplyState.APPLY_PARTIAL
        tag_difference = next(
            difference for difference in result.differences if difference["kind"] == "tags"
        )
        assert tag_difference["expected"] == list(envelope.expected_note_tags[42])
        assert "actual" in tag_difference

    asyncio.run(scenario())
