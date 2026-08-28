import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID, uuid5

from oms_hub.anki.contracts import (
    ActionEnvelope,
    ActionEnvelopeV2,
    AddNotesOperation,
    AddTagsOperation,
    Operation,
    RemoveTagsOperation,
    StoreMediaOperation,
    SyncOperation,
    VerifyOperation,
    canonical_payload_sha256,
)
from oms_hub.anki.domain import ReviewChangeSet
from oms_hub.anki.gaps import GapCardProposal
from oms_hub.anki.tag_policy import TagPolicy, normalize_tag, tag_hash


class EnvelopeBuildError(ValueError):
    """A reviewed change set cannot be converted into a safe mutation plan."""


@dataclass(frozen=True, slots=True)
class CurrentCollectionNote:
    note_id: int
    fields: dict[str, str]
    tags: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.note_id <= 0:
            raise ValueError("current collection note ID must be positive")
        if not self.fields:
            raise ValueError("current collection note fields cannot be empty")


def field_hash(fields: Mapping[str, str]) -> str:
    canonical = json.dumps(
        dict(fields),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def rebind_add_only_envelope(
    envelope: ActionEnvelopeV2,
    current_collection: Mapping[int, CurrentCollectionNote],
) -> ActionEnvelopeV2:
    """Rebase a never-started add-only plan over tag-only collection drift."""
    # ponytail: explicit tag patches still require a new reviewed curation run.
    touched = set(envelope.touched_note_hashes)
    if not touched or set(current_collection) != touched:
        raise EnvelopeBuildError("rebind requires every touched note and no others")
    added: set[int] = set()
    for operation in envelope.operations:
        if isinstance(operation, RemoveTagsOperation):
            raise EnvelopeBuildError("rebind does not support tag removals")
        if isinstance(operation, AddTagsOperation):
            if operation.tag.casefold() != envelope.target_tag.casefold():
                raise EnvelopeBuildError("rebind supports only the frozen target tag")
            added.update(operation.note_ids)
    if added != touched:
        raise EnvelopeBuildError("rebind requires one add-only target-tag plan")

    expected_tag_hashes: dict[int, str] = {}
    expected_note_tags: dict[int, tuple[str, ...]] = {}
    for note_id in sorted(touched):
        current = current_collection[note_id]
        if current.note_id != note_id:
            raise EnvelopeBuildError("current collection note identity changed")
        if field_hash(current.fields) != envelope.touched_note_hashes[note_id]:
            raise EnvelopeBuildError(f"note {note_id} fields changed after review")
        tags = _canonical_tags(tuple(normalize_tag(tag) for tag in current.tags))
        expected_tag_hashes[note_id] = tag_hash(tags)
        expected_note_tags[note_id] = _canonical_tags(
            _add_case_insensitive(tags, envelope.target_tag)
        )

    rebound = envelope.model_copy(
        update={
            "expected_tag_hashes": expected_tag_hashes,
            "expected_note_tags": expected_note_tags,
            "payload_sha256": "0" * 64,
        }
    )
    return rebound.model_copy(
        update={"payload_sha256": canonical_payload_sha256(rebound)}
    )


class EnvelopeBuilder:
    def __init__(self, tag_policy: TagPolicy) -> None:
        self.tag_policy = tag_policy

    def build(
        self,
        changeset: ReviewChangeSet,
        current_collection: Mapping[int, CurrentCollectionNote],
        *,
        envelope_id: UUID,
        snapshot_id: str,
        target_deck: str,
        target_tag: str,
        generated_cards: Sequence[GapCardProposal] = (),
    ) -> ActionEnvelope:
        if not snapshot_id.strip():
            raise EnvelopeBuildError("snapshot ID cannot be blank")
        if not target_deck.strip():
            raise EnvelopeBuildError("target deck cannot be blank")
        target_tag = normalize_tag(target_tag)
        self.tag_policy.validate_initial_tags((target_tag,))

        patches_by_note = {}
        for patch in changeset.tag_patches:
            if patch.note_id in patches_by_note:
                raise EnvelopeBuildError(f"note {patch.note_id} has more than one tag patch")
            patches_by_note[patch.note_id] = patch

        selected_note_ids = {
            note_id for note_id, selected in changeset.candidate_selections.items() if selected
        }
        affected_note_ids = selected_note_ids | set(patches_by_note)
        missing = affected_note_ids - set(current_collection)
        if missing:
            raise EnvelopeBuildError(
                "review references notes missing from the current collection: "
                + ", ".join(str(note_id) for note_id in sorted(missing))
            )

        original_tags: dict[int, tuple[str, ...]] = {}
        resulting_tags: dict[int, tuple[str, ...]] = {}
        for note_id in sorted(affected_note_ids):
            current = current_collection[note_id]
            if current.note_id != note_id:
                raise EnvelopeBuildError(
                    f"current collection key {note_id} does not match its note"
                )
            original = tuple(normalize_tag(tag) for tag in current.tags)
            result = original
            review_patch = patches_by_note.get(note_id)
            if review_patch is not None:
                result = self.tag_policy.validate_tag_patch(
                    original,
                    review_patch,
                ).resulting_tags
            if note_id in selected_note_ids:
                result = _add_case_insensitive(result, target_tag)
            original_tags[note_id] = _canonical_tags(original)
            resulting_tags[note_id] = _canonical_tags(result)

        changed_note_ids = {
            note_id
            for note_id in affected_note_ids
            if _tag_keys(original_tags[note_id]) != _tag_keys(resulting_tags[note_id])
        }
        touched_hashes = {
            note_id: field_hash(current_collection[note_id].fields)
            for note_id in sorted(changed_note_ids)
        }
        expected_tag_hashes = {
            note_id: tag_hash(original_tags[note_id]) for note_id in sorted(changed_note_ids)
        }
        expected_note_tags = {
            note_id: resulting_tags[note_id] for note_id in sorted(changed_note_ids)
        }

        operations: list[Operation] = []
        media_by_filename: dict[str, dict[str, str]] = {}
        for proposal in generated_cards:
            for media in proposal.media:
                filename = media["filename"]
                existing = media_by_filename.get(filename)
                if existing is not None and existing != media:
                    raise EnvelopeBuildError(
                        f"generated media filename collision: {filename}"
                    )
                media_by_filename[filename] = media
        for filename, media in sorted(media_by_filename.items()):
            payload: dict[str, Any] = {
                "filename": filename,
                "content_base64": media["content_base64"],
                "sha256": media["sha256"],
            }
            digest, operation_id = _operation_identity(
                envelope_id,
                "store_media",
                filename,
                payload,
            )
            operations.append(
                StoreMediaOperation(
                    operation_id=operation_id,
                    content_sha256=digest,
                    filename=filename,
                    content_base64=media["content_base64"],
                    sha256=media["sha256"],
                )
            )
        removals: dict[str, list[int]] = defaultdict(list)
        additions: dict[str, list[int]] = defaultdict(list)
        display_tags: dict[str, str] = {}
        for note_id in sorted(changed_note_ids):
            before_by_key = {tag.casefold(): tag for tag in original_tags[note_id]}
            after_by_key = {tag.casefold(): tag for tag in resulting_tags[note_id]}
            for key in sorted(before_by_key.keys() - after_by_key.keys()):
                tag = before_by_key[key]
                display_tags[key] = tag
                removals[key].append(note_id)
            for key in sorted(after_by_key.keys() - before_by_key.keys()):
                tag = after_by_key[key]
                display_tags[key] = tag
                additions[key].append(note_id)

        for key in sorted(removals):
            tag = display_tags[key]
            note_ids = tuple(removals[key])
            payload = {"note_ids": note_ids, "tag": tag}
            digest, operation_id = _operation_identity(
                envelope_id,
                "remove_tags",
                f"{key}:{','.join(str(value) for value in note_ids)}",
                payload,
            )
            operations.append(
                RemoveTagsOperation(
                    operation_id=operation_id,
                    content_sha256=digest,
                    note_ids=note_ids,
                    tag=tag,
                )
            )

        for key in sorted(additions):
            tag = display_tags[key]
            note_ids = tuple(additions[key])
            payload = {"note_ids": note_ids, "tag": tag}
            digest, operation_id = _operation_identity(
                envelope_id,
                "add_tags",
                f"{key}:{','.join(str(value) for value in note_ids)}",
                payload,
            )
            operations.append(
                AddTagsOperation(
                    operation_id=operation_id,
                    content_sha256=digest,
                    note_ids=note_ids,
                    tag=tag,
                )
            )

        generated_notes = self._generated_notes(
            envelope_id,
            target_deck=target_deck.strip(),
            target_tag=target_tag,
            generated_cards=generated_cards,
        )
        if generated_notes:
            payload = {"notes": generated_notes}
            stable_target = ",".join(_marker_tag(note["tags"]) for note in generated_notes)
            digest, operation_id = _operation_identity(
                envelope_id,
                "add_notes",
                stable_target,
                payload,
            )
            operations.append(
                AddNotesOperation(
                    operation_id=operation_id,
                    content_sha256=digest,
                    notes=generated_notes,
                )
            )

        sync_payload = {"snapshot_id": snapshot_id}
        sync_digest, sync_id = _operation_identity(
            envelope_id,
            "sync",
            "collection",
            sync_payload,
        )
        operations.append(
            SyncOperation(
                operation_id=sync_id,
                content_sha256=sync_digest,
            )
        )
        verified_ids = tuple(sorted(changed_note_ids))
        verify_payload = {"note_ids": verified_ids}
        verify_digest, verify_id = _operation_identity(
            envelope_id,
            "verify",
            ",".join(str(value) for value in verified_ids) or "generated",
            verify_payload,
        )
        operations.append(
            VerifyOperation(
                operation_id=verify_id,
                content_sha256=verify_digest,
                note_ids=verified_ids,
            )
        )

        envelope = ActionEnvelope(
            envelope_id=envelope_id,
            snapshot_id=snapshot_id.strip(),
            target_deck=target_deck.strip(),
            target_tag=target_tag,
            touched_note_hashes=touched_hashes,
            expected_tag_hashes=expected_tag_hashes,
            expected_note_tags=expected_note_tags,
            operations=tuple(operations),
            payload_sha256="0" * 64,
        )
        return envelope.model_copy(update={"payload_sha256": canonical_payload_sha256(envelope)})

    def build_v2(
        self,
        *args: Any,
        job_id: UUID,
        pipeline_contract_version: Literal[
            "card_centric_v1", "card_centric_v2", "card_centric_v3"
        ] = "card_centric_v1",
        model_config_sha256: str,
        resolved_model_config: dict[str, Any] | None = None,
        reconciliation_contract_version: str,
        review_revision: int,
        overflow_acknowledgement_provenance: dict[str, Any],
        policy_sha256: str | None = None,
        scope_sha256: str | None = None,
        r11_artifact_sha256: str | None = None,
        r11_snapshot_sha256: str | None = None,
        rate_table_sha256: str | None = None,
        cost_ledger_sha256: str | None = None,
        **kwargs: Any,
    ) -> ActionEnvelopeV2:
        """Lift the immutable V1 mutation plan into the job-bound V2 envelope."""
        if not overflow_acknowledgement_provenance:
            overflow_acknowledgement_provenance = {"required": False}
        base = self.build(*args, **kwargs)
        value = base.model_dump(mode="python")
        value.update(
            {
                "contract_version": 2,
                "job_id": job_id,
                "pipeline_contract_version": pipeline_contract_version,
                "model_config_sha256": model_config_sha256,
                "resolved_model_config": resolved_model_config or {},
                "reconciliation_contract_version": reconciliation_contract_version,
                "review_revision": review_revision,
                "overflow_acknowledgement_provenance": overflow_acknowledgement_provenance,
                "policy_sha256": policy_sha256,
                "scope_sha256": scope_sha256,
                "r11_artifact_sha256": r11_artifact_sha256,
                "r11_snapshot_sha256": r11_snapshot_sha256,
                "rate_table_sha256": rate_table_sha256,
                "cost_ledger_sha256": cost_ledger_sha256,
                "payload_sha256": "0" * 64,
            }
        )
        envelope = ActionEnvelopeV2.model_validate(value)
        return envelope.model_copy(update={"payload_sha256": canonical_payload_sha256(envelope)})

    def _generated_notes(
        self,
        envelope_id: UUID,
        *,
        target_deck: str,
        target_tag: str,
        generated_cards: Sequence[GapCardProposal],
    ) -> tuple[dict[str, Any], ...]:
        ordered = sorted(
            generated_cards,
            key=lambda card: (card.concept_id, card.content_hash),
        )
        stable_ids: set[tuple[str, str]] = set()
        notes: list[dict[str, Any]] = []
        for proposal in ordered:
            stable_id = (proposal.concept_id, proposal.content_hash)
            if stable_id in stable_ids:
                raise EnvelopeBuildError(f"duplicate generated proposal {proposal.concept_id}")
            stable_ids.add(stable_id)
            marker = f"OMS::Curation::Envelope_{envelope_id.hex}::Card_{proposal.content_hash}"
            tags = self.tag_policy.validate_initial_tags(
                (*proposal.initial_tags, target_tag, marker)
            )
            notes.append(
                {
                    "deckName": target_deck,
                    "modelName": proposal.note_type,
                    "fields": dict(proposal.fields),
                    "tags": list(tags),
                    "options": {"allowDuplicate": False},
                }
            )
        return tuple(notes)


def _operation_identity(
    envelope_id: UUID,
    kind: str,
    stable_target: str,
    payload: Mapping[str, Any],
) -> tuple[str, UUID]:
    digest = canonical_payload_sha256(dict(payload))
    return digest, uuid5(
        envelope_id,
        f"{kind}:{stable_target}:{digest}",
    )


def _canonical_tags(tags: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(tags, key=lambda tag: (tag.casefold(), tag)))


def _tag_keys(tags: Sequence[str]) -> set[str]:
    return {tag.casefold() for tag in tags}


def _add_case_insensitive(tags: Sequence[str], tag: str) -> tuple[str, ...]:
    if tag.casefold() in _tag_keys(tags):
        return tuple(tags)
    return (*tags, tag)


def _marker_tag(tags: object) -> str:
    if not isinstance(tags, list):
        raise EnvelopeBuildError("generated note tags are malformed")
    markers = [
        tag for tag in tags if isinstance(tag, str) and tag.startswith("OMS::Curation::Envelope_")
    ]
    if len(markers) != 1:
        raise EnvelopeBuildError("generated note requires one deterministic marker tag")
    return markers[0]
