from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from oms_hub.anki.index import AnkiIndex
from oms_hub.anki.normalize import NormalizedNote, semantic_text
from oms_hub.question_bank.contracts import Axis, QuestionKey, Source, TopicLabel

_MAX_ID = 9223372036854775807
_NID_QUERY = re.compile(r"nid:[0-9]+(?:,[0-9]+)*(?:[ \t]+(?i:or)[ \t]+nid:[0-9]+(?:,[0-9]+)*)*")


@dataclass(frozen=True)
class TagRule:
    source: Source
    product: str
    prefix: str


@dataclass(frozen=True)
class AnkiCandidate:
    key: QuestionKey
    note_id: str
    card_ids: tuple[str, ...]
    evidence_tag: str
    confidence: Literal["exact_tag"]
    review_state: Literal["pending"] = "pending"
    snapshot_status: Literal["unverified"] = "unverified"


@dataclass(frozen=True)
class CandidateNotePreview:
    note_id: str
    card_ids: tuple[str, ...]
    text: str
    missing: bool
    provenance: Literal["user_pasted_amboss_candidates"]
    review_state: Literal["pending"] = "pending"
    snapshot_id: str | None = None
    snapshot_status: Literal["unverified"] = "unverified"


def _note_ids(note: NormalizedNote) -> tuple[str, ...]:
    if any(
        type(value) is not int or not 1 <= value <= _MAX_ID
        for value in (note.note_id, *note.card_ids)
    ):
        raise ValueError("Invalid snapshot note/card ID")
    return tuple(str(value) for value in sorted(note.card_ids))


def match_qid_tags(
    keys: Sequence[QuestionKey],
    notes: Sequence[NormalizedNote],
    rules: Sequence[TagRule],
) -> tuple[AnkiCandidate, ...]:
    prefixes: dict[tuple[Source, str], str] = {}
    for rule in rules:
        qualified = QuestionKey(source=rule.source, product=rule.product, question_id="validation")
        if (
            not isinstance(rule.prefix, str)
            or not rule.prefix.endswith("::")
            or not all(rule.prefix.split("::")[:-1])
            or any(c.isspace() for c in rule.prefix)
        ):
            raise ValueError("Tag rule requires an exact complete parent prefix ending in ::")
        identity = (qualified.source, qualified.product)
        for other, prefix in prefixes.items():
            if (other == identity and prefix != rule.prefix) or (
                other != identity
                and (prefix.startswith(rule.prefix) or rule.prefix.startswith(prefix))
            ):
                raise ValueError("Conflicting source/product tag rules")
        prefixes[identity] = rule.prefix

    requested: dict[str, QuestionKey] = {}
    for key in keys:
        key = QuestionKey.model_validate_json(key.model_dump_json())
        requested_prefix = prefixes.get((key.source, key.product))
        if requested_prefix is not None:
            requested[requested_prefix + key.question_id] = key
    unique_notes: dict[int, NormalizedNote] = {}
    for note in notes:
        if note.note_id in unique_notes and unique_notes[note.note_id] != note:
            raise ValueError("Conflicting snapshots for the same note ID")
        unique_notes[note.note_id] = note

    matches = []
    for note in unique_notes.values():
        cards = _note_ids(note)
        for tag in set(note.tags).intersection(requested):
            matches.append(
                AnkiCandidate(requested[tag], str(note.note_id), cards, tag, "exact_tag")
            )
    return tuple(
        sorted(
            matches,
            key=lambda candidate: (
                candidate.key.source,
                candidate.key.product,
                candidate.key.question_id,
                int(candidate.note_id),
                candidate.evidence_tag,
            ),
        )
    )


def topic_proposals(labels: Sequence[tuple[Axis, str]]) -> tuple[TopicLabel, ...]:
    """Use caller-supplied metadata only; neither QIDs nor exact matches infer topics."""
    return tuple(TopicLabel(axis=axis, label=label) for axis, label in dict.fromkeys(labels))


def parse_candidate_query(raw: str) -> tuple[int, ...]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 64 * 1024:
        raise ValueError("Candidate query must be text of at most 64 KiB")
    query = raw.strip()
    if _NID_QUERY.fullmatch(query) is None:
        raise ValueError("Only nid:ID lists joined by OR are supported")
    tokens = re.findall(r"[0-9]+", query)
    if len(tokens) > 2000:
        raise ValueError("Candidate query exceeds 2000 IDs")
    ids = []
    for token in tokens:
        digits = token.lstrip("0")
        if not digits or len(digits) > 19 or int(digits) > _MAX_ID:
            raise ValueError("Candidate note ID is outside the supported integer range")
        ids.append(int(digits))
    return tuple(dict.fromkeys(ids))


def preview_candidate_notes(raw: str, index: AnkiIndex) -> tuple[CandidateNotePreview, ...]:
    ids = parse_candidate_query(raw)
    snapshot_id = index.snapshot_id()
    result = []
    for note_id in ids:
        note = index.get_note(note_id)
        result.append(
            CandidateNotePreview(
                note_id=str(note_id),
                card_ids=_note_ids(note) if note is not None else (),
                text=semantic_text(note) if note is not None else "",
                missing=note is None,
                provenance="user_pasted_amboss_candidates",
                snapshot_id=snapshot_id,
            )
        )
    if index.snapshot_id() != snapshot_id:
        raise ValueError("Index snapshot changed; preview again")
    return tuple(result)
