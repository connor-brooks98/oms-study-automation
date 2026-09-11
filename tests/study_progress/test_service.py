from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from oms_hub.question_bank.contracts import AttemptFact, QuestionKey, TopicLabel
from oms_hub.study_progress.service import ProgressService, summarize


def fact(number, result="incorrect", *, time=None, question="q", topics=()):
    return AttemptFact(
        number,
        "owner",
        QuestionKey(source="study_hub", product="lecture_quiz", question_id=question),
        str(number),
        result,
        time,
        None,
        datetime(2026, 9, 11, tzinfo=UTC),
        "import",
        topics,
    )


def test_first_repeat_omitted_unknown_and_event_deduplication():
    now = datetime(2026, 9, 11, tzinfo=UTC)
    first = fact(1, time=now)
    repeat = fact(2, "correct", time=now + timedelta(minutes=1))
    omitted = fact(3, "omitted", question="o")
    unknown = fact(4, "unknown", question="u")
    values = (repeat, first, first, omitted, unknown)
    all_attempts = summarize(values, first_only=False)
    assert (
        all_attempts.graded,
        all_attempts.correct,
        all_attempts.incorrect,
        all_attempts.omitted,
        all_attempts.unknown,
        all_attempts.repeated,
    ) == (2, 1, 1, 1, 1, 1)
    first_only = summarize(values, first_only=True)
    assert (first_only.graded, first_only.correct, first_only.repeated) == (1, 0, 1)


def test_ambiguous_order_does_not_guess_first_and_conflicting_duplicates_fail():
    now = datetime(2026, 9, 11, tzinfo=UTC)
    for time in (None, now):
        assert (
            summarize((fact(1, time=now), fact(2, "correct", time=time)), first_only=True).graded
            == 0
        )
    with pytest.raises(ValueError, match="conflict"):
        summarize((fact(1), replace(fact(1), result="correct")), first_only=False)
    with pytest.raises(ValueError, match="owner"):
        summarize((fact(1), replace(fact(2), learner_id="other")), first_only=False)


def test_progress_uses_actual_bank_and_accepted_topics_without_double_counting(tmp_path):
    from oms_hub.db import Database
    from oms_hub.question_bank.repository import BankRepository

    with Database(f"sqlite:///{tmp_path / 'progress.db'}") as database:
        database.migrate()
        bank = BankRepository(database.session)
        topics = (
            TopicLabel(axis="system", label="Blood", method="user", review_state="accepted"),
            TopicLabel(axis="topic", label="Iron", method="user", review_state="accepted"),
            TopicLabel(axis="topic", label="Unreviewed", method="model"),
        )
        bank.record_attempt(
            learner_id="owner",
            key=fact(1).key,
            attempt_id="one",
            result="correct",
            occurred_at=None,
            elapsed_ms=None,
            topics=topics,
        )
        report = ProgressService(bank).report("owner")
        assert report.summary.graded == 1 and report.undated == 1
        assert len(report.topics) == 2
        assert all(group.summary.graded == 1 for group in report.topics)
        assert ProgressService(bank).report("other").summary.graded == 0


def test_topic_view_cannot_reclassify_a_repeat_as_a_first():
    now = datetime(2026, 9, 11, tzinfo=UTC)
    first = fact(1, time=now)
    repeat = fact(
        2,
        "correct",
        time=now + timedelta(minutes=1),
        topics=(
            TopicLabel(axis="topic", label="New label", method="user", review_state="accepted"),
        ),
    )

    class Bank:
        def iter_attempts(self, *, learner_id, after_id=0):
            return (first, repeat) if after_id == 0 else ()

    report = ProgressService(Bank()).report("owner", first_only=True)
    assert report.summary.graded == 1 and report.summary.correct == 0
    assert report.topics[0].summary.graded == 0
    assert report.date_start == now
    assert report.last_activity == now + timedelta(minutes=1)
    assert report.sources[0][1].repeated == 1


def test_versioned_official_taxonomies_preserve_identifiers():
    from oms_hub.study_progress.taxonomy import TAXONOMIES

    nbome, usmle = TAXONOMIES
    assert (nbome.namespace, nbome.version, len(nbome.categories)) == (
        "nbome_comlex_level1",
        "2025-02",
        17,
    )
    assert (usmle.namespace, usmle.version, len(usmle.categories)) == ("usmle_step1", "2026", 18)
    assert nbome.categories[7].source_id == "dimension2:1"
    assert usmle.categories[0].source_id == "Human Development"
    ids = [category.canonical_id for taxonomy in TAXONOMIES for category in taxonomy.categories]
    assert len(set(ids)) == 35 and all(len(value) <= 200 for value in ids)
