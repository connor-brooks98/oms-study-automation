from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import UUID

from oms_hub.anki.ankiconnect import (
    AnkiConnectError,
    AnkiConnectUnavailable,
)
from oms_hub.anki.contracts import (
    ActionEnvelope,
    ActionEnvelopeV2,
    AddNotesOperation,
    AddTagsOperation,
    RemoveTagsOperation,
    SyncOperation,
    VerifyOperation,
)
from oms_hub.anki.domain import ApplyState
from oms_hub.anki.envelope import field_hash
from oms_hub.anki.runtime import AnkiPreflight
from oms_hub.anki.tag_policy import tag_hash

MutationOperation = RemoveTagsOperation | AddTagsOperation | AddNotesOperation


@dataclass(frozen=True, slots=True)
class ApplyOperationRecord:
    state: str
    attempts: int
    result: dict[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ApplyResult:
    envelope_id: UUID
    state: ApplyState
    created_note_ids: tuple[int, ...] = ()
    rejected_duplicates: tuple[dict[str, Any], ...] = ()
    differences: tuple[dict[str, Any], ...] = ()
    safe_error: str | None = None


class ApplyGateway(Protocol):
    async def sync(self) -> None: ...

    async def notes_info(
        self,
        note_ids: Sequence[int],
    ) -> list[dict[str, Any]]: ...

    async def find_notes(self, query: str) -> list[int]: ...

    async def model_field_names(self, model_name: str) -> list[str]: ...

    async def remove_tags(
        self,
        note_ids: Sequence[int],
        tags: Sequence[str],
    ) -> None: ...

    async def add_tags(
        self,
        note_ids: Sequence[int],
        tags: Sequence[str],
    ) -> None: ...

    async def add_notes(
        self,
        notes: Sequence[dict[str, Any]],
    ) -> list[int | None]: ...


class ApplyRuntime(Protocol):
    async def ensure_running(self) -> AnkiPreflight: ...


class ApplyStore(Protocol):
    def get_envelope(self, envelope_id: UUID) -> ActionEnvelope: ...

    def operation_record(
        self,
        envelope_id: UUID,
        operation_id: UUID,
    ) -> ApplyOperationRecord: ...

    def begin_operation(
        self,
        envelope_id: UUID,
        operation_id: UUID,
    ) -> None: ...

    def complete_operation(
        self,
        envelope_id: UUID,
        operation_id: UUID,
        result: dict[str, Any],
    ) -> None: ...

    def fail_operation(
        self,
        envelope_id: UUID,
        operation_id: UUID,
        error: str,
    ) -> None: ...

    def set_apply_state(
        self,
        envelope_id: UUID,
        state: ApplyState,
        summary: dict[str, Any],
    ) -> None: ...

    def validate_card_centric_envelope_acknowledgement(self, envelope_id: UUID) -> bool: ...


class InMemoryApplyStore:
    """Small durable-store analogue used by state-machine tests and local tools."""

    def __init__(self, envelopes: Sequence[ActionEnvelope] = ()) -> None:
        self._envelopes = {envelope.envelope_id: envelope for envelope in envelopes}
        self._operations = {
            (envelope.envelope_id, operation.operation_id): ApplyOperationRecord(
                state="pending",
                attempts=0,
            )
            for envelope in envelopes
            for operation in envelope.operations
        }
        self._states = {envelope.envelope_id: ApplyState.PENDING for envelope in envelopes}
        self._summaries: dict[UUID, dict[str, Any]] = {}

    def put(self, envelope: ActionEnvelope) -> None:
        if envelope.envelope_id in self._envelopes:
            raise ValueError("envelope already exists")
        self._envelopes[envelope.envelope_id] = envelope
        self._states[envelope.envelope_id] = ApplyState.PENDING
        for operation in envelope.operations:
            self._operations[(envelope.envelope_id, operation.operation_id)] = ApplyOperationRecord(
                state="pending", attempts=0
            )

    def get_envelope(self, envelope_id: UUID) -> ActionEnvelope:
        try:
            return self._envelopes[envelope_id]
        except KeyError:
            raise KeyError(str(envelope_id)) from None

    def operation_record(
        self,
        envelope_id: UUID,
        operation_id: UUID,
    ) -> ApplyOperationRecord:
        try:
            return self._operations[(envelope_id, operation_id)]
        except KeyError:
            raise KeyError(str(operation_id)) from None

    def begin_operation(
        self,
        envelope_id: UUID,
        operation_id: UUID,
    ) -> None:
        current = self.operation_record(envelope_id, operation_id)
        self._operations[(envelope_id, operation_id)] = ApplyOperationRecord(
            state="intent",
            attempts=current.attempts + 1,
            result=current.result,
        )

    def complete_operation(
        self,
        envelope_id: UUID,
        operation_id: UUID,
        result: dict[str, Any],
    ) -> None:
        current = self.operation_record(envelope_id, operation_id)
        self._operations[(envelope_id, operation_id)] = ApplyOperationRecord(
            state="complete",
            attempts=current.attempts,
            result=dict(result),
        )

    def fail_operation(
        self,
        envelope_id: UUID,
        operation_id: UUID,
        error: str,
    ) -> None:
        current = self.operation_record(envelope_id, operation_id)
        self._operations[(envelope_id, operation_id)] = ApplyOperationRecord(
            state="failed",
            attempts=current.attempts,
            result=current.result,
            error=error,
        )

    def set_apply_state(
        self,
        envelope_id: UUID,
        state: ApplyState,
        summary: dict[str, Any],
    ) -> None:
        self.get_envelope(envelope_id)
        self._states[envelope_id] = state
        self._summaries[envelope_id] = dict(summary)

    def state(self, envelope_id: UUID) -> ApplyState:
        return self._states[envelope_id]

    def summary(self, envelope_id: UUID) -> dict[str, Any]:
        return dict(self._summaries.get(envelope_id, {}))

    def validate_card_centric_envelope_acknowledgement(self, envelope_id: UUID) -> bool:
        del envelope_id
        return True


class ApplyCoordinator:
    def __init__(
        self,
        store: ApplyStore,
        gateway: ApplyGateway,
        *,
        runtime: ApplyRuntime | None = None,
        supported_envelope_versions: frozenset[int] | Callable[[], frozenset[int]] = frozenset({1}),
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.runtime = runtime
        self.supported_envelope_versions = supported_envelope_versions

    async def apply(self, envelope_id: UUID) -> ApplyResult:
        envelope = self.store.get_envelope(envelope_id)
        if (
            isinstance(envelope, ActionEnvelopeV2)
            and not self.store.validate_card_centric_envelope_acknowledgement(envelope_id)
        ):
            return self._finish(
                envelope,
                ApplyState.FAILED_BEFORE_APPLY,
                safe_error=("selection overflow acknowledgement is missing, stale, or forged; "
                            "no mutation performed"),
            )
        versions = (
            self.supported_envelope_versions()
            if callable(self.supported_envelope_versions)
            else self.supported_envelope_versions
        )
        if isinstance(envelope, ActionEnvelopeV2) and 2 not in versions:
            return self._finish(
                envelope,
                ApplyState.FAILED_BEFORE_APPLY,
                safe_error=(
                    "envelope contract v2 unsupported; upgrade required; no mutation performed"
                ),
            )
        mutation_started = any(
            self.store.operation_record(
                envelope_id,
                operation.operation_id,
            ).attempts
            > 0
            for operation in envelope.operations
            if operation.operation_type in {"store_media", "remove_tags", "add_tags", "add_notes"}
        )
        if not mutation_started:
            preflight_error = await self._preflight()
            if preflight_error is not None:
                return self._finish(
                    envelope,
                    ApplyState.FAILED_BEFORE_APPLY,
                    safe_error=preflight_error,
                )
            try:
                await self.gateway.sync()
            except AnkiConnectError as exc:
                return self._finish(
                    envelope,
                    ApplyState.FAILED_BEFORE_APPLY,
                    safe_error=_safe_error(exc),
                )
            stale = await self._existing_note_differences(
                envelope,
                before_apply=True,
            )
            if stale:
                return self._finish(
                    envelope,
                    ApplyState.FAILED_BEFORE_APPLY,
                    differences=stale,
                    safe_error="Anki notes changed after review",
                )

        for operation in envelope.operations:
            record = self.store.operation_record(
                envelope_id,
                operation.operation_id,
            )
            if record.state == "complete":
                continue
            if operation.operation_type in {
                "remove_tags",
                "add_tags",
                "add_notes",
            }:
                failed = await self._apply_mutation(
                    envelope,
                    cast(MutationOperation, operation),
                )
                if failed is not None:
                    return failed
                continue
            if isinstance(operation, SyncOperation):
                failed = await self._trailing_sync(envelope, operation)
                if failed is not None:
                    return failed
                continue
            if isinstance(operation, VerifyOperation):
                failed = await self._verify(envelope, operation)
                if failed is not None:
                    return failed
                continue
            error = f"unsupported operation {operation.operation_type}"
            self.store.begin_operation(envelope_id, operation.operation_id)
            self.store.fail_operation(
                envelope_id,
                operation.operation_id,
                error,
            )
            return self._finish(
                envelope,
                ApplyState.APPLY_PARTIAL,
                safe_error=error,
            )

        return self._finish(envelope, ApplyState.COMPLETE)

    async def _preflight(self) -> str | None:
        if self.runtime is None:
            return None
        result = await self.runtime.ensure_running()
        if not result.reachable or not result.collection_accessible or not result.sync_available:
            return result.blocking_reason or "Anki preflight failed"
        return None

    async def _apply_mutation(
        self,
        envelope: ActionEnvelope,
        operation: MutationOperation,
    ) -> ApplyResult | None:
        self.store.begin_operation(
            envelope.envelope_id,
            operation.operation_id,
        )
        try:
            if isinstance(operation, RemoveTagsOperation):
                recovered = await self._tag_operation_already_applied(
                    operation,
                    should_exist=False,
                )
                if not recovered:
                    await self.gateway.remove_tags(
                        operation.note_ids,
                        (operation.tag,),
                    )
                result: dict[str, Any] = {
                    "recovered": recovered,
                    "note_ids": list(operation.note_ids),
                    "tag": operation.tag,
                }
            elif isinstance(operation, AddTagsOperation):
                recovered = await self._tag_operation_already_applied(
                    operation,
                    should_exist=True,
                )
                if not recovered:
                    await self.gateway.add_tags(
                        operation.note_ids,
                        (operation.tag,),
                    )
                result = {
                    "recovered": recovered,
                    "note_ids": list(operation.note_ids),
                    "tag": operation.tag,
                }
            elif isinstance(operation, AddNotesOperation):
                (
                    note_ids,
                    added_count,
                    recovered_count,
                    rejected_duplicates,
                ) = await self._add_missing_notes(operation)
                result = {
                    "note_ids": list(note_ids),
                    "added_count": added_count,
                    "recovered_count": recovered_count,
                    "rejected_duplicates": list(rejected_duplicates),
                }
        except AnkiConnectError as exc:
            error = f"{operation.operation_type}: {_safe_error(exc)}"
            self.store.fail_operation(
                envelope.envelope_id,
                operation.operation_id,
                error,
            )
            return self._finish(
                envelope,
                ApplyState.APPLY_PARTIAL,
                safe_error=error,
            )
        self.store.complete_operation(
            envelope.envelope_id,
            operation.operation_id,
            result,
        )
        return None

    async def _tag_operation_already_applied(
        self,
        operation: AddTagsOperation | RemoveTagsOperation,
        *,
        should_exist: bool,
    ) -> bool:
        raw_notes = await self.gateway.notes_info(operation.note_ids)
        if len(raw_notes) != len(operation.note_ids):
            raise AnkiConnectError("Anki did not return every note targeted by a tag operation")
        target = operation.tag.casefold()
        return all(
            (target in {tag.casefold() for tag in _raw_tags(raw)}) is should_exist
            for raw in raw_notes
        )

    async def _add_missing_notes(
        self,
        operation: AddNotesOperation,
    ) -> tuple[
        tuple[int | None, ...],
        int,
        int,
        tuple[dict[str, Any], ...],
    ]:
        resolved: list[int | None] = []
        missing_notes: list[dict[str, Any]] = []
        missing_positions: list[int] = []
        for position, note in enumerate(operation.notes):
            marker = _generated_marker(note)
            matches = await self.gateway.find_notes(f"tag:{marker}")
            if len(matches) > 1:
                raise AnkiConnectError(f"generated-note marker {marker} matched multiple notes")
            if matches:
                resolved.append(matches[0])
            else:
                resolved.append(None)
                missing_notes.append(note)
                missing_positions.append(position)
        recovered_count = len(operation.notes) - len(missing_notes)
        prepared = await self._prepare_generated_notes(missing_notes)
        created = await self.gateway.add_notes(prepared) if prepared else []
        if len(created) != len(missing_notes):
            raise AnkiConnectError("Anki did not return one ID per generated note")
        for position, note_id in zip(missing_positions, created, strict=True):
            resolved[position] = note_id
        rejected_duplicates = tuple(
            {
                "position": position,
                "status": "rejected_duplicate",
            }
            for position, note_id in enumerate(resolved)
            if note_id is None
        )
        added_count = sum(note_id is not None for note_id in created)
        return (
            tuple(resolved),
            added_count,
            recovered_count,
            rejected_duplicates,
        )

    async def _prepare_generated_notes(
        self,
        notes: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        model_fields: dict[str, set[str]] = {}
        prepared: list[dict[str, Any]] = []
        for note in notes:
            model = str(note.get("modelName", ""))
            if model not in model_fields:
                model_fields[model] = set(await self.gateway.model_field_names(model))
            available = model_fields[model]
            fields = dict(cast(Mapping[str, str], note.get("fields", {})))
            if "Extra" in fields and "Extra" not in available and "Back Extra" in available:
                fields["Back Extra"] = fields.pop("Extra")
            unsupported = set(fields) - available
            if unsupported:
                raise AnkiConnectError(
                    f"note type {model} does not support fields: "
                    + ", ".join(sorted(unsupported))
                )
            prepared.append({**note, "fields": fields})
        return prepared

    async def _trailing_sync(
        self,
        envelope: ActionEnvelope,
        operation: SyncOperation,
    ) -> ApplyResult | None:
        self.store.begin_operation(
            envelope.envelope_id,
            operation.operation_id,
        )
        try:
            await self.gateway.sync()
        except AnkiConnectUnavailable as exc:
            error = _safe_error(exc)
            self.store.fail_operation(
                envelope.envelope_id,
                operation.operation_id,
                error,
            )
            return self._finish(
                envelope,
                ApplyState.APPLIED_LOCAL_SYNC_RETRYABLE,
                safe_error=error,
            )
        except AnkiConnectError as exc:
            error = _safe_error(exc)
            self.store.fail_operation(
                envelope.envelope_id,
                operation.operation_id,
                error,
            )
            return self._finish(
                envelope,
                ApplyState.APPLIED_LOCAL_SYNC_BLOCKED,
                safe_error=error,
            )
        self.store.complete_operation(
            envelope.envelope_id,
            operation.operation_id,
            {"sync_status": "complete"},
        )
        return None

    async def _verify(
        self,
        envelope: ActionEnvelope,
        operation: VerifyOperation,
    ) -> ApplyResult | None:
        self.store.begin_operation(
            envelope.envelope_id,
            operation.operation_id,
        )
        differences = list(
            await self._existing_note_differences(
                envelope,
                before_apply=False,
            )
        )
        differences.extend(await self._generated_note_differences(envelope))
        if differences:
            error = "Anki read-back verification did not match the envelope"
            self.store.fail_operation(
                envelope.envelope_id,
                operation.operation_id,
                error,
            )
            return self._finish(
                envelope,
                ApplyState.APPLY_PARTIAL,
                differences=tuple(differences),
                safe_error=error,
            )
        self.store.complete_operation(
            envelope.envelope_id,
            operation.operation_id,
            {
                "verified_note_ids": list(operation.note_ids),
                "created_note_ids": list(self._created_note_ids(envelope)),
            },
        )
        return None

    async def _existing_note_differences(
        self,
        envelope: ActionEnvelope,
        *,
        before_apply: bool,
    ) -> tuple[dict[str, Any], ...]:
        note_ids = tuple(sorted(envelope.touched_note_hashes))
        if not note_ids:
            return ()
        raw_notes = await self.gateway.notes_info(note_ids)
        by_id = {_raw_note_id(note): note for note in raw_notes}
        differences: list[dict[str, Any]] = []
        for note_id in note_ids:
            raw = by_id.get(note_id)
            if raw is None:
                differences.append(
                    {
                        "note_id": note_id,
                        "kind": "missing",
                        "expected": "present",
                        "actual": "missing",
                    }
                )
                continue
            actual_field_hash = field_hash(_raw_fields(raw))
            expected_field_hash = envelope.touched_note_hashes[note_id]
            if actual_field_hash != expected_field_hash:
                differences.append(
                    {
                        "note_id": note_id,
                        "kind": "fields",
                        "expected": expected_field_hash,
                        "actual": actual_field_hash,
                    }
                )
            actual_tags = _canonical_tags(_raw_tags(raw))
            if before_apply:
                expected_tag_hash = envelope.expected_tag_hashes.get(note_id)
                if expected_tag_hash is not None and tag_hash(actual_tags) != expected_tag_hash:
                    differences.append(
                        {
                            "note_id": note_id,
                            "kind": "tags",
                            "expected": expected_tag_hash,
                            "actual": tag_hash(actual_tags),
                            "actual_tags": list(actual_tags),
                        }
                    )
            else:
                expected_tags = envelope.expected_note_tags.get(note_id)
                if expected_tags is not None and _tag_keys(actual_tags) != _tag_keys(expected_tags):
                    differences.append(
                        {
                            "note_id": note_id,
                            "kind": "tags",
                            "expected": list(expected_tags),
                            "actual": list(actual_tags),
                        }
                    )
        return tuple(differences)

    async def _generated_note_differences(
        self,
        envelope: ActionEnvelope,
    ) -> tuple[dict[str, Any], ...]:
        expected = self._created_note_specs(envelope)
        if not expected:
            return ()
        raw_notes = await self.gateway.notes_info(tuple(expected))
        by_id = {_raw_note_id(note): note for note in raw_notes}
        differences: list[dict[str, Any]] = []
        for note_id, note in expected.items():
            raw = by_id.get(note_id)
            if raw is None:
                differences.append(
                    {
                        "note_id": note_id,
                        "kind": "missing",
                        "expected": "created",
                        "actual": "missing",
                    }
                )
                continue
            expected_fields = cast(
                Mapping[str, str],
                note.get("fields", {}),
            )
            actual_fields = _raw_fields(raw)
            if not _generated_fields_match(expected_fields, actual_fields):
                differences.append(
                    {
                        "note_id": note_id,
                        "kind": "fields",
                        "expected": dict(expected_fields),
                        "actual": actual_fields,
                    }
                )
            expected_tags = _canonical_tags(_note_tags(note))
            actual_tags = _canonical_tags(_raw_tags(raw))
            if _tag_keys(expected_tags) != _tag_keys(actual_tags):
                differences.append(
                    {
                        "note_id": note_id,
                        "kind": "tags",
                        "expected": list(expected_tags),
                        "actual": list(actual_tags),
                    }
                )
        return tuple(differences)

    def _created_note_specs(
        self,
        envelope: ActionEnvelope,
    ) -> dict[int, dict[str, Any]]:
        expected: dict[int, dict[str, Any]] = {}
        for operation in envelope.operations:
            if not isinstance(operation, AddNotesOperation):
                continue
            record = self.store.operation_record(
                envelope.envelope_id,
                operation.operation_id,
            )
            if record.state != "complete" or record.result is None:
                continue
            note_ids = record.result.get("note_ids")
            if not isinstance(note_ids, list) or len(note_ids) != len(operation.notes):
                continue
            for note_id, note in zip(note_ids, operation.notes, strict=True):
                if isinstance(note_id, int) and note_id > 0:
                    expected[note_id] = note
        return expected

    def _created_note_ids(
        self,
        envelope: ActionEnvelope,
    ) -> tuple[int, ...]:
        return tuple(self._created_note_specs(envelope))

    def _rejected_duplicates(
        self,
        envelope: ActionEnvelope,
    ) -> tuple[dict[str, Any], ...]:
        rejected: list[dict[str, Any]] = []
        for operation in envelope.operations:
            if not isinstance(operation, AddNotesOperation):
                continue
            record = self.store.operation_record(
                envelope.envelope_id,
                operation.operation_id,
            )
            if record.state != "complete" or record.result is None:
                continue
            values = record.result.get("rejected_duplicates")
            if isinstance(values, list):
                rejected.extend(value for value in values if isinstance(value, dict))
        return tuple(rejected)

    def _finish(
        self,
        envelope: ActionEnvelope,
        state: ApplyState,
        *,
        differences: tuple[dict[str, Any], ...] = (),
        safe_error: str | None = None,
    ) -> ApplyResult:
        created_note_ids = self._created_note_ids(envelope)
        rejected_duplicates = self._rejected_duplicates(envelope)
        summary = {
            "state": state.value,
            "created_note_ids": list(created_note_ids),
            "rejected_duplicates": list(rejected_duplicates),
            "differences": list(differences),
            "safe_error": safe_error,
        }
        self.store.set_apply_state(
            envelope.envelope_id,
            state,
            summary,
        )
        return ApplyResult(
            envelope_id=envelope.envelope_id,
            state=state,
            created_note_ids=created_note_ids,
            rejected_duplicates=rejected_duplicates,
            differences=differences,
            safe_error=safe_error,
        )


def _safe_error(error: Exception) -> str:
    return (str(error).strip() or type(error).__name__)[:500]


def _generated_fields_match(
    expected: Mapping[str, str],
    actual: Mapping[str, str],
) -> bool:
    matched: set[str] = set()
    for name, value in expected.items():
        resolved = (
            "Back Extra"
            if name == "Extra" and name not in actual and "Back Extra" in actual
            else name
        )
        if actual.get(resolved) != value:
            return False
        matched.add(resolved)
    return all(not value for name, value in actual.items() if name not in matched)


def _raw_note_id(note: Mapping[str, Any]) -> int:
    value = note.get("noteId")
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise AnkiConnectError("Anki returned an invalid note ID")
    return value


def _raw_fields(note: Mapping[str, Any]) -> dict[str, str]:
    fields = note.get("fields")
    if not isinstance(fields, dict):
        raise AnkiConnectError("Anki returned invalid note fields")
    values: dict[str, str] = {}
    for name, value in fields.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            raise AnkiConnectError("Anki returned invalid note fields")
        field_value = value.get("value")
        if not isinstance(field_value, str):
            raise AnkiConnectError("Anki returned invalid note fields")
        values[name] = field_value
    return values


def _raw_tags(note: Mapping[str, Any]) -> tuple[str, ...]:
    tags = note.get("tags")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise AnkiConnectError("Anki returned invalid note tags")
    return tuple(tags)


def _note_tags(note: Mapping[str, Any]) -> tuple[str, ...]:
    tags = note.get("tags")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise AnkiConnectError("generated note has invalid tags")
    return tuple(tags)


def _generated_marker(note: Mapping[str, Any]) -> str:
    markers = [tag for tag in _note_tags(note) if tag.startswith("OMS::Curation::Envelope_")]
    if len(markers) != 1:
        raise AnkiConnectError("generated note does not have exactly one idempotency marker")
    return markers[0]


def _canonical_tags(tags: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(tags, key=lambda tag: (tag.casefold(), tag)))


def _tag_keys(tags: Sequence[str]) -> set[str]:
    return {tag.casefold() for tag in tags}
