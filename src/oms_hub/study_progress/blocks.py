"""Deterministic selection of existing, accepted native questions."""

import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from oms_hub.question_bank.contracts import AttemptFact, QuestionKey, TopicLabel
from oms_hub.question_bank.repository import BankRepository
from oms_hub.study_chat.contracts import OwnerId
from oms_hub.study_generation.domain import PublishedQuizRecord
from oms_hub.study_progress.service import _unique
from oms_hub.study_progress.sessions import (
    NativeQuestionRef,
    SessionView,
    StudySessionService,
    _hash,
    _key,
)


@dataclass(frozen=True)
class BlockCandidate:
    key: str
    graded_attempts: int
    accuracy: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip() or len(self.key) > 200:
            raise ValueError("Invalid candidate key")
        if type(self.graded_attempts) is not int or self.graded_attempts < 0:
            raise ValueError("Graded attempts must be nonnegative")
        if self.accuracy is not None and (
            type(self.accuracy) not in (int, float)
            or not math.isfinite(self.accuracy)
            or not 0 <= self.accuracy <= 1
        ):
            raise ValueError("Accuracy must be between zero and one")


def select_block(candidates: tuple[BlockCandidate, ...], *, count: int) -> tuple[str, ...]:
    if type(count) is not int or not 1 <= count <= 500:
        raise ValueError("count must be between 1 and 500")
    unique: dict[str, BlockCandidate] = {}
    for candidate in candidates:
        if candidate.key in unique and unique[candidate.key] != candidate:
            raise ValueError("Candidate metadata conflict")
        unique[candidate.key] = candidate
    ordered = sorted(
        unique.values(),
        key=lambda c: (
            c.graded_attempts > 0,
            c.accuracy if c.accuracy is not None else -1.0,
            c.graded_attempts,
            c.key,
        ),
    )
    return tuple(c.key for c in ordered[:count])


class BlockFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    course: Annotated[str, Field(strict=True, min_length=1, max_length=100)]
    exam_numbers: Annotated[
        tuple[Annotated[int, Field(strict=True, ge=1, le=1000)], ...],
        Field(min_length=1, max_length=100),
    ]
    topic_ids: Annotated[
        tuple[Annotated[str, Field(strict=True, max_length=200)], ...], Field(max_length=100)
    ] = ()
    count: Annotated[int, Field(strict=True, ge=1, le=500)] = 20


@dataclass(frozen=True)
class NativeCandidate:
    key: QuestionKey
    reference: NativeQuestionRef
    course: str
    course_label: str
    exam_number: int
    source_label: str
    topics: tuple[TopicLabel, ...]
    performance: BlockCandidate


@dataclass(frozen=True)
class BlockCatalog:
    questions: tuple[NativeCandidate, ...]
    unavailable_publications: int


class BlockService:
    def __init__(
        self,
        *,
        sessions: StudySessionService,
        bank: BankRepository,
        publications_for: Callable[[str], tuple[PublishedQuizRecord, ...]],
    ):
        self.sessions = sessions
        self.bank = bank
        self.publications_for = publications_for

    def catalog(self, owner_id: str) -> BlockCatalog:
        TypeAdapter(OwnerId).validate_python(owner_id)
        # Only owner-accessible ready bank metadata supplies reviewed topic filters.
        ready = {q.key: q for q in self.bank.list_ready_questions(learner_id=owner_id)}
        facts: list[AttemptFact] = []
        cursor = 0
        while page := self.bank.iter_attempts(learner_id=owner_id, after_id=cursor):
            facts.extend(page)
            cursor = page[-1].id
        outcomes = Counter((fact.key, fact.result) for fact in _unique(tuple(facts)))
        candidates: dict[str, NativeCandidate] = {}
        unavailable = 0
        for listed in self.publications_for(owner_id):
            try:
                with self.sessions._session_factory() as session:
                    publication = self.sessions._publication(session, owner_id, listed.token)
                    self.sessions._content(owner_id, publication)
            except (PermissionError, ValueError, OSError):
                unavailable += 1
                continue
            digest = _hash(publication)
            for question in publication.quiz.questions:
                key = _key(publication.token, publication.version, digest, question.id)
                correct = outcomes[key, "correct"]
                graded = correct + outcomes[key, "incorrect"]
                known = ready.get(key)
                topics = (
                    tuple(
                        t
                        for t in known.topics
                        if t.review_state == "accepted" and t.method != "unmapped"
                    )
                    if known
                    else ()
                )
                candidate = NativeCandidate(
                    key,
                    NativeQuestionRef(
                        quiz_token=publication.token,
                        quiz_version=publication.version,
                        quiz_content_sha256=digest,
                        question_id=question.id,
                    ),
                    publication.destination_subject_key,
                    publication.destination_subject,
                    publication.destination_exam_number,
                    f"{publication.destination_subject} · "
                    f"Exam {publication.destination_exam_number}"
                    f" · {publication.label or publication.title} · {question.id}",
                    topics,
                    BlockCandidate(key.question_id, graded, correct / graded if graded else None),
                )
                if key.question_id in candidates and candidates[key.question_id] != candidate:
                    raise ValueError("Native candidate conflict")
                candidates[key.question_id] = candidate
        return BlockCatalog(tuple(candidates.values()), unavailable)

    @staticmethod
    def eligible(catalog: BlockCatalog, filters: BlockFilters) -> tuple[NativeCandidate, ...]:
        return tuple(
            q
            for q in catalog.questions
            if q.course == filters.course
            and q.exam_number in filters.exam_numbers
            and (
                not filters.topic_ids or any(t.canonical_id in filters.topic_ids for t in q.topics)
            )
        )

    @staticmethod
    def preview(
        catalog: BlockCatalog, filters: BlockFilters, *, exclude_keys: tuple[str, ...] = ()
    ) -> tuple[NativeCandidate, ...]:
        matching = {
            q.key.question_id: q
            for q in BlockService.eligible(catalog, filters)
            if q.key.question_id not in exclude_keys
        }
        keys = select_block(tuple(q.performance for q in matching.values()), count=filters.count)
        return tuple(matching[key] for key in keys)

    def create(
        self, owner_id: str, filters: BlockFilters, *, selected_keys: tuple[str, ...]
    ) -> SessionView:
        if not 1 <= len(selected_keys) <= filters.count or len(set(selected_keys)) != len(
            selected_keys
        ):
            raise ValueError("Select distinct available questions")
        catalog = self.catalog(owner_id)
        eligible = {q.key.question_id: q for q in self.eligible(catalog, filters)}
        if any(key not in eligible for key in selected_keys):
            raise ValueError("Selected question changed or is outside this scope")
        return self.sessions.create_block(
            owner_id, tuple(eligible[key].reference for key in selected_keys)
        )

    def resolve(self, owner_id: str, key_id: str) -> NativeCandidate:
        candidate = next(
            (q for q in self.catalog(owner_id).questions if q.key.question_id == key_id), None
        )
        if candidate is None:
            raise PermissionError("Question is unavailable")
        return candidate
