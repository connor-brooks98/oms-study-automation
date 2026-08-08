"""Fail-closed loader for the private, immutable Lecture 07 S4 fixture."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oms_hub.anki.card_centric_contracts import CardCentricSourceIndex, CardClassification

FIXTURE_VERSION = "lecture07-external-v1"


class FixtureUnavailable(ValueError):
    """The private fixture was not installed or did not satisfy its contract."""


@dataclass(frozen=True, slots=True)
class Lecture07Fixture:
    version: str
    source_index: CardCentricSourceIndex
    cards: tuple[dict[str, Any], ...]
    baseline: dict[int, str]
    missed_concept_ids: tuple[str, ...]
    named_cases: dict[str, tuple[int, ...]]
    sha256: str


def load_lecture07_fixture(
    path: Path | None, required_sha256: str | None = None
) -> Lecture07Fixture:
    if path is None or not path.is_file():
        raise FixtureUnavailable("Lecture07 private fixture artifact is unavailable")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError
        claimed = str(raw.pop("sha256"))
        canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        computed = hashlib.sha256(canonical.encode()).hexdigest()
        if computed != claimed:
            raise FixtureUnavailable("Lecture07 fixture hash does not match immutable contents")
        if required_sha256 is not None and computed != required_sha256:
            raise FixtureUnavailable("Lecture07 fixture does not match configured SHA-256 pin")
        source = CardCentricSourceIndex.model_validate(raw["source_index"])
        cards = tuple(raw["cards"])
        baseline = {int(key): str(value) for key, value in dict(raw["baseline_verdicts"]).items()}
        missed = tuple(str(value) for value in raw["missed_concept_ids"])
        named = {
            str(key): tuple(int(item) for item in value)
            for key, value in dict(raw["named_cases"]).items()
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FixtureUnavailable("Lecture07 private fixture artifact is invalid") from exc
    ids = [int(card.get("note_id", 0)) for card in cards if isinstance(card, dict)]
    if (
        len(cards) < 124
        or len(ids) != len(set(ids))
        or set(ids) != set(baseline)
        or len(missed) != 6
        or len(named) < 1
        or not source.passages
        or any(
            not str(card.get("text", "")).strip() or not isinstance(card.get("tags"), list)
            for card in cards
        )
        or any(value not in {"YES", "MAYBE", "NO"} for value in baseline.values())
    ):
        raise FixtureUnavailable(
            "Lecture07 fixture does not meet real-artifact structural minimums"
        )
    return Lecture07Fixture(
        version=str(raw.get("fixture_version", FIXTURE_VERSION)),
        source_index=source,
        cards=cards,
        baseline=baseline,
        missed_concept_ids=missed,
        named_cases=named,
        sha256=computed,
    )


def evaluate_lecture07_fixture(
    fixture: Lecture07Fixture, classifications: tuple[CardClassification, ...]
) -> tuple[bool, dict[str, object]]:
    observed = {item.note_id: item for item in classifications}
    false_keeps = tuple(
        sorted(
            note_id
            for note_id, expected in fixture.baseline.items()
            if expected != "YES" and observed.get(note_id) and observed[note_id].verdict == "YES"
        )
    )
    false_drops = tuple(
        sorted(
            note_id
            for note_id, expected in fixture.baseline.items()
            if expected == "YES" and (note_id not in observed or observed[note_id].verdict != "YES")
        )
    )
    complete = set(observed) == set(fixture.baseline)
    coverage = {
        concept
        for item in observed.values()
        if item.verdict == "YES" and not item.flags
        for concept in item.covered_concept_ids
    }
    passed = (
        complete
        and not false_keeps
        and not false_drops
        and set(fixture.missed_concept_ids) <= coverage
    )
    return passed, {
        "fixture_version": fixture.version,
        "fixture_sha256": fixture.sha256,
        "fixture_note_count": len(fixture.cards),
        "false_keeps": false_keeps,
        "false_drops": false_drops,
        "six_concept_coverage": set(fixture.missed_concept_ids) <= coverage,
    }
