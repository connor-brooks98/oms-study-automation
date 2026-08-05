"""Deterministic Lecture 07 downgrade gate for S4/S6 classifier profiles."""

from dataclasses import dataclass
from typing import Literal

from oms_hub.anki.card_centric_contracts import CardClassification


@dataclass(frozen=True, slots=True)
class FixtureCase:
    note_id: int
    expected: Literal["YES", "NO"]
    category: Literal["missed_concept", "off_topic", "disease_name_absent", "coverage_guard"]


@dataclass(frozen=True, slots=True)
class FixtureReport:
    agreement: float
    false_keeps: tuple[int, ...]
    false_drops: tuple[int, ...]
    passed: bool


LECTURE07_FIXTURE = (
    FixtureCase(7001, "YES", "missed_concept"),  # Mitapivat
    FixtureCase(7002, "YES", "missed_concept"),  # C. perfringens
    FixtureCase(7003, "YES", "missed_concept"),  # graft-vs-host
    FixtureCase(7004, "YES", "missed_concept"),  # AHTR phase sequence
    FixtureCase(7005, "YES", "missed_concept"),  # warm AIHA secondary causes
    FixtureCase(7006, "YES", "missed_concept"),  # G6PD treatment
    FixtureCase(7101, "NO", "off_topic"),  # cortisol/PNH
    FixtureCase(7102, "NO", "off_topic"),  # hemophilia/G6PD inheritance trap
    FixtureCase(7201, "YES", "disease_name_absent"),  # Heinz bodies
    FixtureCase(7202, "YES", "disease_name_absent"),  # bite cells
    FixtureCase(7301, "NO", "coverage_guard"),  # flagged-only cannot cover
    FixtureCase(7302, "NO", "coverage_guard"),  # summary-only cannot cover
)


def evaluate_lecture07_fixture(
    classifications: tuple[CardClassification, ...],
    *,
    minimum_agreement: float = 1.0,
) -> FixtureReport:
    """Gate cheaper defaults: no known false keep/drop and exact fixture coverage."""
    expected = {case.note_id: case for case in LECTURE07_FIXTURE}
    observed = {item.note_id: item for item in classifications}
    if set(observed) != set(expected):
        return FixtureReport(0.0, (), tuple(sorted(set(expected) - set(observed))), False)
    false_keeps = tuple(
        case.note_id
        for case in LECTURE07_FIXTURE
        if case.expected == "NO" and observed[case.note_id].verdict == "YES"
    )
    false_drops = tuple(
        case.note_id
        for case in LECTURE07_FIXTURE
        if case.expected == "YES" and observed[case.note_id].verdict != "YES"
    )
    correct = len(LECTURE07_FIXTURE) - len(false_keeps) - len(false_drops)
    agreement = correct / len(LECTURE07_FIXTURE)
    return FixtureReport(
        agreement=agreement,
        false_keeps=false_keeps,
        false_drops=false_drops,
        passed=(agreement >= minimum_agreement and not false_keeps and not false_drops),
    )
