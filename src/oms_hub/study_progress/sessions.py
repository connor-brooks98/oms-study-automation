"""Server-issued deliveries, immutable answer staging, and idempotent bank receipts."""

import hashlib
import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal, Self, cast
from urllib.parse import quote
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from oms_hub.files.atomic import sha256_file
from oms_hub.models import PublishedQuizModel, StudySessionModel, StudySessionQuestionModel
from oms_hub.question_bank.contracts import QuestionKey, Result, TopicLabel
from oms_hub.question_bank.repository import BankRepository
from oms_hub.study_chat.contracts import OwnerId, UuidId
from oms_hub.study_generation.domain import PublishedQuizMediaRecord, PublishedQuizRecord
from oms_hub.study_generation.native_quiz import (
    grade_answer,
    grade_matching_answer,
    image_requirements,
    parse_native_quiz,
    public_quiz_content,
    serialize_native_quiz,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]
Duration = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
SelectionId = Annotated[str, Field(min_length=1, max_length=200)]


class AnswerSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    kind: Literal["choice", "matching"] = "choice"
    choice_id: SelectionId | None = None
    matches: Annotated[dict[SelectionId, SelectionId], Field(min_length=2, max_length=8)] | None = (
        None
    )

    @model_validator(mode="after")
    def selection_shape(self) -> Self:
        if self.kind == "choice" and (self.choice_id is None or self.matches is not None):
            raise ValueError("A choice is required")
        if self.kind == "matching" and (not self.matches or self.choice_id is not None):
            raise ValueError("Matches are required")
        return self


class NativeQuestionRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    quiz_token: Annotated[str, Field(min_length=1, max_length=64)]
    quiz_version: Annotated[int, Field(ge=1)]
    quiz_content_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    question_id: SelectionId


def native_reference(publication: PublishedQuizRecord, question_id: str) -> NativeQuestionRef:
    return NativeQuestionRef(
        quiz_token=publication.token,
        quiz_version=publication.version,
        quiz_content_sha256=_hash(publication),
        question_id=question_id,
    )


@dataclass(frozen=True)
class SessionView:
    id: str
    quiz_token: str
    quiz_version: int
    title: str
    closed: bool
    questions: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class AnswerReceipt:
    attempt_id: str
    bank_attempt_id: int
    result: Result
    feedback: dict[str, object]


def _hash(publication: PublishedQuizRecord) -> str:
    return hashlib.sha256(serialize_native_quiz(publication.quiz).encode()).hexdigest()


def _key(token: str, version: int, digest: str, question_id: str) -> QuestionKey:
    identity = json.dumps([token, version, digest, question_id], separators=(",", ":"))
    return QuestionKey(
        source="study_hub",
        product="lecture_quiz",
        question_id="sha256:" + hashlib.sha256(identity.encode()).hexdigest(),
    )


def question_key(publication: PublishedQuizRecord, question_id: str) -> QuestionKey:
    return _key(publication.token, publication.version, _hash(publication), question_id)


def _owned(session: Session, session_id: str, owner_id: str) -> StudySessionModel:
    TypeAdapter(OwnerId).validate_python(owner_id)
    TypeAdapter(UuidId).validate_python(session_id)
    row = session.get(StudySessionModel, session_id)
    if row is None or row.owner_id != owner_id:
        raise PermissionError("Session unavailable")
    return row


class StudySessionService:
    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        bank: BankRepository,
        load_quiz: Callable[[str, str], PublishedQuizRecord],
        topics_for: Callable[[str, QuestionKey], tuple[TopicLabel, ...]],
        media_for: Callable[[str, PublishedQuizRecord], tuple[PublishedQuizMediaRecord, ...]],
    ):
        self._session_factory = session_factory
        self.bank = bank
        self.load_quiz = load_quiz
        self.topics_for = topics_for
        self.media_for = media_for

    def _publication(self, session: Session, owner_id: str, token: str) -> PublishedQuizRecord:
        # O's callback also checks owner access and accepted review/publication state.
        publication = self.load_quiz(owner_id, token)
        current = session.get(PublishedQuizModel, token)
        if (
            not publication.active
            or publication.token != token
            or current is None
            or not current.active
            or current.version != publication.version
            or serialize_native_quiz(parse_native_quiz(current.payload_json))
            != serialize_native_quiz(publication.quiz)
        ):
            raise ValueError("Publication changed")
        return publication

    def _content(self, owner_id: str, publication: PublishedQuizRecord) -> list[dict[str, object]]:
        # The callback enforces trusted paths; bind every required image to this publication.
        media = self.media_for(owner_id, publication)
        by_key = {m.image_key: m for m in media}
        if len(by_key) != len(media):
            raise ValueError("Duplicate media")
        urls: dict[str, tuple[str, str, int | None, int | None]] = {}
        for required in image_requirements(publication.quiz):
            item = by_key.get(required.key)
            if (
                item is None
                or item.quiz_token != publication.token
                or sha256_file(item.path) != item.sha256
            ):
                raise ValueError("Required media unavailable")
            urls[item.image_key] = (
                f"/public/quizzes/{quote(publication.token, safe='')}/media/"
                f"{quote(item.image_key, safe='')}",
                "Question image",
                item.width,
                item.height,
            )
        questions = cast(
            list[dict[str, object]], public_quiz_content(publication.quiz, urls)["questions"]
        )
        for question in questions:
            for name in ("area", "topic", "learning_objective"):
                question.pop(name, None)
        return questions

    def create(self, owner_id: str, quiz_token: str) -> SessionView:
        publication = self.load_quiz(owner_id, quiz_token)
        return self.create_block(
            owner_id, tuple(native_reference(publication, q.id) for q in publication.quiz.questions)
        )

    def create_block(
        self,
        owner_id: str,
        references: tuple[NativeQuestionRef, ...],
        *,
        validate_selection: Callable[[], None] | None = None,
    ) -> SessionView:
        TypeAdapter(OwnerId).validate_python(owner_id)
        references = tuple(NativeQuestionRef.model_validate(r.model_dump()) for r in references)
        if not 1 <= len(references) <= 500 or len(set(references)) != len(references):
            raise ValueError("Select 1-500 distinct versioned questions")
        with self._session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            if validate_selection is not None:
                validate_selection()
            publications = {}
            for ref in references:
                if ref.quiz_token not in publications:
                    publication = self._publication(session, owner_id, ref.quiz_token)
                    self._content(owner_id, publication)
                    publications[ref.quiz_token] = publication
                publication = publications[ref.quiz_token]
                if (
                    ref.quiz_version != publication.version
                    or ref.quiz_content_sha256 != _hash(publication)
                    or ref.question_id not in {q.id for q in publication.quiz.questions}
                ):
                    raise ValueError("Selected question changed")
            session_id = str(uuid4())
            session.add(StudySessionModel(id=session_id, owner_id=owner_id))
            session.flush()
            for position, ref in enumerate(references):
                session.add(
                    StudySessionQuestionModel(
                        attempt_id=str(uuid4()),
                        session_id=session_id,
                        position=position,
                        **ref.model_dump(),
                    )
                )
        return self.load(session_id, owner_id=owner_id)

    def load(self, session_id: str, *, owner_id: str) -> SessionView:
        with self._session_factory() as session:
            owned = _owned(session, session_id, owner_id)
            rows = session.scalars(
                select(StudySessionQuestionModel)
                .where(StudySessionQuestionModel.session_id == session_id)
                .order_by(StudySessionQuestionModel.position)
            ).all()
            if not rows:
                raise ValueError("Empty session")
            publications = {}
            for token in dict.fromkeys(row.quiz_token for row in rows):
                publication = self._publication(session, owner_id, token)
                questions = self._content(owner_id, publication)
                publications[token] = (publication, {q["id"]: q for q in questions})
            delivered = []
            for row in rows:
                publication, by_id = publications[row.quiz_token]
                self._unchanged(row, publication)
                question = dict(by_id[row.question_id])
                # Delivery IDs are unique across publications; grading retains the original row ID.
                question["id"] = f"q{row.position + 1}"
                question["source_label"] = (
                    f"{publication.destination_subject} · "
                    f"Exam {publication.destination_exam_number}"
                    f" · {publication.label or publication.title} · {row.question_id}"
                )
                question["attempt_id"] = row.attempt_id
                # A staged selection survives a crash; feedback requires a bank receipt.
                if row.selected_answer_json:
                    staged = json.loads(row.selected_answer_json)
                    question["answer"] = staged["submission"]
                    question["elapsed_ms"] = row.elapsed_ms
                    if row.bank_attempt_id is not None:
                        question["feedback"] = staged["feedback"]
                delivered.append(question)
            title = (
                publication.title
                if len(publications) == 1 and len(rows) == len(publication.quiz.questions)
                else f"Study block · {len(rows)} questions"
            )
            return SessionView(
                owned.id,
                rows[0].quiz_token,
                rows[0].quiz_version if len(publications) == 1 else 1,
                title,
                owned.closed_at is not None,
                tuple(delivered),
            )

    @staticmethod
    def _unchanged(row: StudySessionQuestionModel, publication: PublishedQuizRecord) -> None:
        if row.quiz_version != publication.version or row.quiz_content_sha256 != _hash(publication):
            raise ValueError("Delivered question version changed")

    def answer(
        self,
        session_id: str,
        attempt_id: str,
        submission: AnswerSelection,
        *,
        owner_id: str,
        elapsed_ms: int | None = None,
    ) -> AnswerReceipt:
        TypeAdapter(UuidId).validate_python(attempt_id)
        TypeAdapter(Duration | None).validate_python(elapsed_ms)
        submission = AnswerSelection.model_validate_json(submission.model_dump_json())
        with self._session_factory() as session:
            # Serialize staging, then release the DB before Q's independent atomic transaction.
            session.execute(text("BEGIN IMMEDIATE"))
            owned = _owned(session, session_id, owner_id)
            row = session.get(StudySessionQuestionModel, attempt_id)
            if row is None or row.session_id != session_id:
                raise PermissionError("Attempt was not delivered by this session")
            key = _key(row.quiz_token, row.quiz_version, row.quiz_content_sha256, row.question_id)
            if row.selected_answer_json is not None:
                staged = json.loads(row.selected_answer_json)
                if staged["submission"] != submission.model_dump() or row.elapsed_ms != elapsed_ms:
                    raise ValueError("Answer conflict")
            else:
                if owned.closed_at is not None:
                    raise ValueError("Session is closed")
                publication = self._publication(session, owner_id, row.quiz_token)
                self._unchanged(row, publication)
                question = next(q for q in publication.quiz.questions if q.id == row.question_id)
                feedback: dict[str, object]
                result: Result
                graded = (
                    grade_matching_answer(
                        publication.quiz,
                        row.question_id,
                        cast(dict[str, str], submission.matches),
                    )
                    if submission.kind == "matching"
                    else grade_answer(
                        publication.quiz, row.question_id, cast(str, submission.choice_id)
                    )
                )
                feedback = asdict(graded)
                result = "correct" if graded.correct else "incorrect"
                for name in ("area", "topic", "learning_objective"):
                    if (value := getattr(question, name)) is not None:
                        feedback[name] = value
                staged = {
                    "submission": submission.model_dump(),
                    "feedback": feedback,
                    "result": result,
                    "topics": [
                        t.model_dump()
                        for t in self.topics_for(owner_id, key)
                        if t.review_state == "accepted" and t.method != "unmapped"
                    ],
                }
                row.selected_answer_json = json.dumps(staged, sort_keys=True)
                row.submitted_at = datetime.now(UTC).isoformat()
                row.elapsed_ms = elapsed_ms
            occurred_at = datetime.fromisoformat(cast(str, row.submitted_at))

        fact = self.bank.record_attempt(
            learner_id=owner_id,
            key=key,
            attempt_id=attempt_id,
            result=staged["result"],
            occurred_at=occurred_at,
            elapsed_ms=elapsed_ms,
            topics=tuple(TopicLabel.model_validate(t) for t in staged["topics"]),
        )
        if (
            fact.learner_id != owner_id
            or fact.key != key
            or fact.attempt_id != attempt_id
            or fact.result != staged["result"]
            or fact.occurred_at != occurred_at
            or fact.elapsed_ms != elapsed_ms
        ):
            raise ValueError("Bank receipt conflict")
        with self._session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            _owned(session, session_id, owner_id)
            row = session.get(StudySessionQuestionModel, attempt_id)
            if row is None or row.session_id != session_id:
                raise PermissionError("Attempt unavailable")
            if row.bank_attempt_id not in (None, fact.id):
                raise ValueError("Bank receipt conflict")
            row.bank_attempt_id = fact.id
        return AnswerReceipt(attempt_id, fact.id, fact.result, staged["feedback"])

    def close(self, session_id: str, *, owner_id: str) -> None:
        with self._session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = _owned(session, session_id, owner_id)
            row.closed_at = row.closed_at or datetime.now(UTC).isoformat()
