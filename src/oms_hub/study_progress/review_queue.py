"""Current missed/guessed native questions, derived from receipted personal answers."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy import select

from oms_hub.models import BankAttemptModel, StudySessionModel, StudySessionQuestionModel
from oms_hub.study_chat.contracts import OwnerId
from oms_hub.study_progress.blocks import BlockService
from oms_hub.study_progress.sessions import NativeQuestionRef, SessionView, _key


class ReviewFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    course: Annotated[str, Field(strict=True, min_length=1, max_length=100)] | None = None
    exam: Annotated[int, Field(strict=True, ge=1, le=1000)] | None = None
    count: Annotated[int, Field(strict=True, ge=1, le=500)] = 20


@dataclass(frozen=True)
class ReviewItem:
    key: str
    reference: NativeQuestionRef
    course: str
    course_label: str
    exam: int
    source_label: str
    reason: Literal["missed", "guessed", "missed_and_guessed"]
    answered_at: str


@dataclass(frozen=True)
class ReviewQueue:
    items: tuple[ReviewItem, ...]
    total: int
    unavailable: int
    courses: tuple[tuple[str, str], ...]
    exams: tuple[int, ...]


class ReviewQueueService:
    def __init__(self, blocks: BlockService):
        self.blocks = blocks

    def catalog(self, owner_id: str, filters: ReviewFilters) -> ReviewQueue:
        TypeAdapter(OwnerId).validate_python(owner_id)
        # Bank receipts establish committed answers. Staged-but-unreceipted submissions
        # remain recoverable by replay and cannot prematurely clear the review queue.
        with self.blocks.sessions._session_factory() as session:
            records = session.execute(
                select(StudySessionQuestionModel, BankAttemptModel.result, BankAttemptModel.id)
                .join(
                    StudySessionModel, StudySessionModel.id == StudySessionQuestionModel.session_id
                )
                .join(
                    BankAttemptModel,
                    BankAttemptModel.id == StudySessionQuestionModel.bank_attempt_id,
                )
                .where(
                    StudySessionModel.owner_id == owner_id,
                    BankAttemptModel.learner_id == owner_id,
                    BankAttemptModel.external_attempt_id == StudySessionQuestionModel.attempt_id,
                    BankAttemptModel.result.in_(("correct", "incorrect")),
                    StudySessionQuestionModel.submitted_at.is_not(None),
                    StudySessionQuestionModel.selected_answer_json.is_not(None),
                )
            ).all()
            latest: dict[str, tuple[tuple[datetime, int], str, bool, str]] = {}
            for row, result, receipt_id in records:
                assert row.submitted_at is not None and row.selected_answer_json is not None
                occurred = datetime.fromisoformat(row.submitted_at)
                if occurred.tzinfo is None:
                    continue  # Unknown ordering is not inferred from an import/reload date.
                key = _key(
                    row.quiz_token, row.quiz_version, row.quiz_content_sha256, row.question_id
                ).question_id
                staged = json.loads(row.selected_answer_json)
                guessed = staged.get("guessed", False)
                if type(guessed) is not bool or staged.get("result") != result:
                    raise ValueError("Saved review answer conflicts with its receipt")
                order = (occurred, receipt_id)
                if key not in latest or latest[key][0] < order:
                    latest[key] = (order, result, guessed, row.submitted_at)
        candidates = {q.key.question_id: q for q in self.blocks.catalog(owner_id).questions}
        items = []
        unavailable = 0
        for key, (_order, result, guessed, answered_at) in latest.items():
            if result == "correct" and not guessed:
                continue
            candidate = candidates.get(key)
            if candidate is None:
                unavailable += 1
                continue
            reason: Literal["missed", "guessed", "missed_and_guessed"] = (
                "missed_and_guessed"
                if result == "incorrect" and guessed
                else "guessed"
                if guessed
                else "missed"
            )
            items.append(
                ReviewItem(
                    key,
                    candidate.reference,
                    candidate.course,
                    candidate.course_label,
                    candidate.exam_number,
                    candidate.source_label,
                    reason,
                    answered_at,
                )
            )
        items.sort(key=lambda item: (latest[item.key][0], item.key))
        courses = tuple(sorted({(item.course, item.course_label) for item in items}))
        exams = tuple(
            sorted(
                {item.exam for item in items if not filters.course or item.course == filters.course}
            )
        )
        chosen = tuple(
            item
            for item in items
            if (not filters.course or item.course == filters.course)
            and (filters.exam is None or item.exam == filters.exam)
        )
        return ReviewQueue(chosen, len(items), unavailable, courses, exams)

    def create(
        self, owner_id: str, filters: ReviewFilters, *, selected_keys: tuple[str, ...]
    ) -> SessionView:
        if not 1 <= len(selected_keys) <= filters.count or len(set(selected_keys)) != len(
            selected_keys
        ):
            raise ValueError("Select 1–500 distinct questions from the current review queue")
        eligible = {item.key: item for item in self.catalog(owner_id, filters).items}
        if any(key not in eligible for key in selected_keys):
            raise ValueError("Review selection changed or is outside the selected scope")

        def validate_selection() -> None:
            current = {item.key for item in self.catalog(owner_id, filters).items}
            if any(key not in current for key in selected_keys):
                raise ValueError("Review selection changed before the session started")

        return self.blocks.sessions.create_block(
            owner_id,
            tuple(eligible[key].reference for key in selected_keys),
            validate_selection=validate_selection,
        )
