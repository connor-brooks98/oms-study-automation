from oms_hub.anki.card_centric_contracts import CardClassification
from oms_hub.anki.card_centric_fixture import (
    LECTURE07_FIXTURE,
    evaluate_lecture07_fixture,
)


def _classification(note_id: int, verdict: str) -> CardClassification:
    return CardClassification(
        note_id=note_id,
        verdict=verdict,  # type: ignore[arg-type]
        primary_subject="fixture",
        reason="fixture result",
        supporting_passage_ids=("SLD:07:P:0001",) if verdict == "YES" else (),
    )


def test_lecture07_fixture_gates_known_false_keeps_and_drops() -> None:
    passing = tuple(_classification(case.note_id, case.expected) for case in LECTURE07_FIXTURE)
    assert evaluate_lecture07_fixture(passing).passed

    false_keep = tuple(_classification(case.note_id, "YES") for case in LECTURE07_FIXTURE)
    report = evaluate_lecture07_fixture(false_keep)
    assert report.passed is False
    assert {7101, 7102, 7301, 7302} <= set(report.false_keeps)
