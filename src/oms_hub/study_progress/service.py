from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from oms_hub.question_bank.contracts import AttemptFact, QuestionKey, TopicLabel
from oms_hub.question_bank.repository import BankRepository


@dataclass(frozen=True)
class ProgressSummary:
    graded: int
    correct: int
    incorrect: int
    omitted: int
    unknown: int
    repeated: int


@dataclass(frozen=True)
class TopicProgress:
    topic: TopicLabel
    summary: ProgressSummary


@dataclass(frozen=True)
class ProgressReport:
    summary: ProgressSummary
    first_only: bool
    sample_count: int
    question_count: int
    undated: int
    ambiguous_questions: int
    date_start: datetime | None
    last_activity: datetime | None
    last_import: datetime | None
    sources: tuple[tuple[str, ProgressSummary], ...]
    topics: tuple[TopicProgress, ...]


def _unique(attempts: tuple[AttemptFact, ...]) -> tuple[AttemptFact, ...]:
    if len({a.learner_id for a in attempts}) > 1:
        raise ValueError("Progress cannot combine owners")
    unique: dict[tuple[QuestionKey, str], AttemptFact] = {}
    for attempt in attempts:
        identity = (attempt.key, attempt.attempt_id)
        if identity in unique and unique[identity] != attempt:
            raise ValueError("Attempt identity conflict")
        unique[identity] = attempt
    return tuple(unique.values())


def _first(attempts: tuple[AttemptFact, ...]) -> tuple[tuple[AttemptFact, ...], int, int]:
    groups: dict[QuestionKey, list[AttemptFact]] = defaultdict(list)
    for attempt in attempts:
        groups[attempt.key].append(attempt)
    first: list[AttemptFact] = []
    ambiguous = 0
    for values in groups.values():
        if len(values) == 1:
            first.append(values[0])
            continue
        times = [a.occurred_at for a in values if a.occurred_at is not None]
        if len(times) != len(values) or times.count(min(times)) != 1:
            ambiguous += 1
            continue
        first.append(next(a for a in values if a.occurred_at == min(times)))
    return tuple(first), len(attempts) - len(groups), ambiguous


def _counts(attempts: tuple[AttemptFact, ...], repeated: int) -> ProgressSummary:
    correct = sum(a.result == "correct" for a in attempts)
    incorrect = sum(a.result == "incorrect" for a in attempts)
    return ProgressSummary(
        correct + incorrect,
        correct,
        incorrect,
        sum(a.result == "omitted" for a in attempts),
        sum(a.result == "unknown" for a in attempts),
        repeated,
    )


def summarize(attempts: tuple[AttemptFact, ...], *, first_only: bool) -> ProgressSummary:
    unique = _unique(attempts)
    first, repeated, _ = _first(unique)
    return _counts(first if first_only else unique, repeated)


class ProgressService:
    def __init__(self, bank: BankRepository):
        self.bank = bank

    def report(self, owner_id: str, *, first_only: bool = False) -> ProgressReport:
        # ponytail: scan one owner's history; add DB aggregation when history size warrants it.
        attempts: list[AttemptFact] = []
        cursor = 0
        while page := self.bank.iter_attempts(learner_id=owner_id, after_id=cursor):
            attempts.extend(page)
            cursor = page[-1].id
        unique = _unique(tuple(attempts))
        first, repeated, ambiguous = _first(unique)
        selected = first if first_only else unique
        selected_ids = {(a.key, a.attempt_id) for a in selected}
        topics: dict[tuple[str, str], tuple[TopicLabel, list[AttemptFact]]] = {}
        sources: dict[str, list[AttemptFact]] = defaultdict(list)
        for attempt in unique:
            sources[f"{attempt.key.source} / {attempt.key.product}"].append(attempt)
            seen = set()
            for topic in attempt.topics:
                if topic.review_state != "accepted" or topic.method == "unmapped":
                    continue
                identity = (topic.axis, topic.canonical_id or topic.label)
                if identity in seen:
                    continue
                seen.add(identity)
                topics.setdefault(identity, (topic, []))[1].append(attempt)

        def group_counts(values: list[AttemptFact]) -> ProgressSummary:
            _, repeats, _ = _first(tuple(values))
            return _counts(
                tuple(a for a in values if (a.key, a.attempt_id) in selected_ids), repeats
            )

        times = [a.occurred_at for a in unique if a.occurred_at is not None]
        return ProgressReport(
            _counts(selected, repeated),
            first_only,
            len(unique),
            len({a.key for a in unique}),
            len(unique) - len(times),
            ambiguous,
            min(times, default=None),
            max(times, default=None),
            max((a.imported_at for a in unique), default=None),
            tuple((label, group_counts(values)) for label, values in sorted(sources.items())),
            tuple(
                TopicProgress(topic, group_counts(values))
                for _, (topic, values) in sorted(topics.items())
            ),
        )
