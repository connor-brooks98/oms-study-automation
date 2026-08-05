import hashlib
import json

import pytest

from oms_hub.anki.card_centric import build_source_index
from oms_hub.anki.card_centric_contracts import CardClassification
from oms_hub.anki.card_centric_fixture import (
    FixtureUnavailable,
    evaluate_lecture07_fixture,
    load_lecture07_fixture,
)
from oms_hub.anki.domain import SourceKind
from oms_hub.anki.sources import SourcePassage


def _artifact(tmp_path):
    passage = SourcePassage.create(
        revision_id=1,
        lecture_id=7,
        artifact_id="slides",
        source_kind=SourceKind.SLIDE,
        locator="slide:1",
        text="Lecture07 source evidence",
    )
    source = build_source_index(
        [passage], snapshot_id="fixture", source_revision_hashes={1: "a" * 64}
    )
    cards = [
        {
            "note_id": 10_000 + index,
            "content_sha256": f"{index + 1:064x}",
            "text": f"real card {index}",
            "extra": "",
            "tags": ["#AK::Heme"],
        }
        for index in range(124)
    ]
    payload = {
        "fixture_version": "private-v1",
        "source_index": source.model_dump(mode="json"),
        "cards": cards,
        "baseline_verdicts": {str(card["note_id"]): "YES" for card in cards},
        "missed_concept_ids": [f"C{index:02d}" for index in range(1, 7)],
        "named_cases": {"real_missed_concepts": [card["note_id"] for card in cards[:6]]},
    }
    payload["sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path = tmp_path / "lecture07.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_external_fixture_requires_real_structural_minimums_and_hash(tmp_path) -> None:
    fixture = load_lecture07_fixture(_artifact(tmp_path))
    observed = tuple(
        CardClassification(
            note_id=card["note_id"],
            verdict="YES",
            primary_subject="fixture",
            reason="fixture",
            covered_concept_ids=(f"C{index + 1:02d}",) if index < 6 else (),
            supporting_passage_ids=(fixture.source_index.passages[0].passage_id,),
        )
        for index, card in enumerate(fixture.cards)
    )
    passed, metrics = evaluate_lecture07_fixture(fixture, observed)
    assert passed and metrics["fixture_note_count"] == 124

    data = json.loads(_artifact(tmp_path).read_text())
    data["cards"] = data["cards"][:12]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(FixtureUnavailable):
        load_lecture07_fixture(bad)


def test_fixture_is_unavailable_without_private_artifact() -> None:
    with pytest.raises(FixtureUnavailable, match="unavailable"):
        load_lecture07_fixture(None)
