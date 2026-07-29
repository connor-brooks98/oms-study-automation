from datetime import UTC, datetime, timedelta
from pathlib import Path

from oms_hub.anki.ledger import AnkiLedger
from oms_hub.anki.snapshot_export import (
    DeltaSnapshotPlanner,
    SnapshotVersions,
)

NOW = datetime(2026, 7, 27, 12, tzinfo=UTC)
VERSIONS = SnapshotVersions(
    export_version="1",
    normalizer_version="1",
    embedding_model="fixed-v1",
)


def test_delta_planner_falls_back_without_safe_compatible_ledger(
    tmp_path: Path,
) -> None:
    ledger = AnkiLedger(tmp_path / "ledger.sqlite3")
    planner = DeltaSnapshotPlanner(ledger)

    absent = planner.plan(current_note_ids=[1, 2], now=NOW, versions=VERSIONS)
    assert absent.full_export is True
    assert absent.reason == "ledger_absent"

    ledger.replace_note_hashes({1: "a" * 64, 2: "b" * 64})
    ledger.set_snapshot_state(
        exported_at=NOW - timedelta(hours=2),
        note_count=2,
        versions=SnapshotVersions(
            export_version="old",
            normalizer_version="1",
            embedding_model="fixed-v1",
        ),
    )
    incompatible = planner.plan(
        current_note_ids=[1, 2],
        now=NOW,
        versions=VERSIONS,
    )
    assert incompatible.full_export is True
    assert incompatible.reason == "version_changed"


def test_delta_planner_detects_adds_deletes_and_uses_safety_margin(
    tmp_path: Path,
) -> None:
    ledger = AnkiLedger(tmp_path / "ledger.sqlite3")
    ledger.replace_note_hashes({1: "a" * 64, 2: "b" * 64, 3: "c" * 64})
    ledger.set_snapshot_state(
        exported_at=NOW - timedelta(hours=25),
        note_count=3,
        versions=VERSIONS,
    )

    plan = DeltaSnapshotPlanner(
        ledger,
        safety_margin=timedelta(hours=2),
        maximum_window=timedelta(days=7),
    ).plan(current_note_ids=[2, 3, 4], now=NOW, versions=VERSIONS)

    assert plan.full_export is False
    assert plan.added_note_ids == (4,)
    assert plan.deleted_note_ids == (1,)
    assert plan.edit_query == 'deck:"Anking Step Deck" edited:2'


def test_delta_planner_falls_back_for_unsafe_window_or_count_drift(
    tmp_path: Path,
) -> None:
    ledger = AnkiLedger(tmp_path / "ledger.sqlite3")
    ledger.replace_note_hashes({1: "a" * 64})
    ledger.set_snapshot_state(
        exported_at=NOW - timedelta(days=8),
        note_count=1,
        versions=VERSIONS,
    )
    planner = DeltaSnapshotPlanner(
        ledger,
        maximum_window=timedelta(days=7),
    )

    assert planner.plan(
        current_note_ids=[1],
        now=NOW,
        versions=VERSIONS,
    ).reason == "unsafe_window"

    ledger.set_snapshot_state(
        exported_at=NOW - timedelta(hours=1),
        note_count=2,
        versions=VERSIONS,
    )
    assert planner.plan(
        current_note_ids=[1],
        now=NOW,
        versions=VERSIONS,
    ).reason == "ledger_count_mismatch"
