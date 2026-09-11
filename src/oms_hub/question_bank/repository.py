from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import cast
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from oms_hub.models import (
    BankAttemptModel,
    BankImportModel,
    BankImportRowModel,
    BankQuestionModel,
    BankTopicReviewModel,
    utc_now,
)
from oms_hub.question_bank.contracts import (
    AttemptFact,
    ImportEnvelope,
    ImportPreview,
    ImportReceipt,
    ImportRow,
    QuestionKey,
    Result,
    RowIssue,
    TopicLabel,
)
from oms_hub.question_bank.imports import preview_import

_TOPICS = TypeAdapter(tuple[TopicLabel, ...])


@dataclass(frozen=True)
class BankQuestion:
    key: QuestionKey
    content_hash: str
    native_question: dict[str, object]
    topics: tuple[TopicLabel, ...]


class _Conflict(ValueError):
    def __init__(self, issue: RowIssue):
        self.issue = issue
        super().__init__(issue.detail)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _owner(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 320:
        raise ValueError("A valid learner/reviewer identity is required")
    return value


def _key(question: BankQuestionModel) -> QuestionKey:
    return QuestionKey.model_validate(
        {
            "source": question.source,
            "product": question.product,
            "question_id": question.external_question_id,
        }
    )


def _question(session: Session, key: QuestionKey) -> BankQuestionModel | None:
    key = QuestionKey.model_validate_json(key.model_dump_json())
    return session.scalar(
        select(BankQuestionModel).where(
            BankQuestionModel.source == key.source,
            BankQuestionModel.product == key.product,
            BankQuestionModel.external_question_id == key.question_id,
        )
    )


def _ready(question: BankQuestionModel) -> BankQuestion | None:
    if (
        not question.body_ready
        or question.content_hash is None
        or question.native_question_json is None
    ):
        return None
    return BankQuestion(
        _key(question),
        question.content_hash,
        json.loads(question.native_question_json),
        _TOPICS.validate_json(question.reviewed_topics_json),
    )


def _fact(
    attempt: BankAttemptModel,
    question: BankQuestionModel,
    row: BankImportRowModel,
    provenance_json: str,
) -> AttemptFact:
    reviewed = _TOPICS.validate_json(question.reviewed_topics_json)
    reviewed_labels = {(topic.axis, topic.label) for topic in reviewed}
    trusted_native = json.loads(provenance_json).get("kind") == "study_hub_attempt"
    # Only the internal recording path can create this provenance, never external input.
    proposals = tuple(
        topic if trusted_native else topic.model_copy(update={"review_state": "pending"})
        for topic in _TOPICS.validate_json(row.topics_json)
        if (topic.axis, topic.label) not in reviewed_labels
    )
    return AttemptFact(
        id=attempt.id,
        learner_id=attempt.learner_id,
        key=_key(question),
        attempt_id=attempt.external_attempt_id,
        result=cast(Result, attempt.result),
        occurred_at=datetime.fromisoformat(attempt.occurred_at) if attempt.occurred_at else None,
        elapsed_ms=attempt.elapsed_ms,
        imported_at=datetime.fromisoformat(attempt.imported_at),
        import_id=row.import_id,
        topics=proposals + reviewed,
    )


class BankRepository:
    def __init__(self, session_factory: Callable[[], AbstractContextManager[Session]]):
        self._session_factory = session_factory

    def commit_import(
        self,
        preview: ImportPreview,
        *,
        learner_id: str,
        expected_digest: str,
    ) -> ImportReceipt:
        _owner(learner_id)
        # Re-parse even frozen models: nested raw bodies and model_copy are not trusted.
        envelope = ImportEnvelope.model_validate(preview.envelope.model_dump(mode="python"))
        checked = preview_import(_json(envelope.model_dump(mode="json")).encode())
        if checked.digest != expected_digest or checked.digest != preview.digest:
            raise ValueError("Import digest changed; preview again")
        return self._commit(checked, learner_id, _json(checked.envelope.provenance.model_dump()))

    def _commit(
        self, preview: ImportPreview, learner_id: str, provenance_json: str
    ) -> ImportReceipt:
        import_id = str(uuid4())
        blocking = tuple(issue for issue in preview.issues if issue.code == "duplicate_conflict")
        if blocking:
            return ImportReceipt(import_id, 0, 0, 0, blocking)
        envelope = preview.envelope
        try:
            with self._session_factory() as session:
                # ponytail: serialize SQLite writers; move to row locks if the DB backend changes.
                session.execute(text("BEGIN IMMEDIATE"))
                existing = session.scalar(
                    select(BankImportModel).where(
                        BankImportModel.learner_id == learner_id,
                        BankImportModel.source == envelope.source,
                        BankImportModel.product == envelope.product,
                        BankImportModel.export_id == envelope.export_id,
                    )
                )
                if existing is not None:
                    if (
                        existing.digest != preview.digest
                        or existing.provenance_json != provenance_json
                    ):
                        raise _Conflict(
                            RowIssue(1, "export_conflict", "Export identity has changed content")
                        )
                    stored = json.loads(existing.receipt_json)
                    return ImportReceipt(
                        existing.id,
                        stored["inserted_questions"],
                        stored["inserted_attempts"],
                        stored["duplicate_rows"],
                        (),
                    )
                imported_at = utc_now()
                record = BankImportModel(
                    id=import_id,
                    learner_id=learner_id,
                    source=envelope.source,
                    product=envelope.product,
                    export_id=envelope.export_id,
                    digest=preview.digest,
                    provenance_json=provenance_json,
                    imported_at=imported_at,
                )
                session.add(record)
                session.flush()
                inserted_questions = inserted_attempts = duplicate_rows = 0
                ready_rows = set(preview.ready_question_rows)
                row_issues: dict[int, list[RowIssue]] = {}
                for issue in preview.issues:
                    row_issues.setdefault(issue.row, []).append(issue)
                for number, row in enumerate(envelope.rows, start=1):
                    key = QuestionKey(
                        source=envelope.source,
                        product=envelope.product,
                        question_id=row.question_id,
                    )
                    question = _question(session, key)
                    if question is None:
                        question = BankQuestionModel(
                            source=key.source,
                            product=key.product,
                            external_question_id=key.question_id,
                        )
                        session.add(question)
                        session.flush()
                        inserted_questions += 1
                    body = _json(row.question) if row.question is not None else None
                    body_hash = _hash(body) if body is not None else None
                    if body_hash is not None:
                        if question.content_hash is not None and question.content_hash != body_hash:
                            raise _Conflict(
                                RowIssue(number, "question_conflict", "Question body changed")
                            )
                        if question.content_hash is None:
                            question.content_hash = body_hash
                            # Only first-body enrichment is permitted, never repair/overwrite.
                            question.body_ready = number in ready_rows
                            question.native_question_json = body if question.body_ready else None
                    canonical_row = _json(row.model_dump(mode="json"))
                    row_hash = _hash(canonical_row)
                    issues = row_issues.get(number, [])
                    identical_row = session.scalar(
                        select(BankImportRowModel.id)
                        .join(BankImportModel, BankImportModel.id == BankImportRowModel.import_id)
                        .where(
                            BankImportModel.learner_id == learner_id,
                            BankImportRowModel.question_id == question.id,
                            BankImportRowModel.canonical_row_hash == row_hash,
                        )
                    )
                    imported_row = BankImportRowModel(
                        import_id=import_id,
                        row_number=number,
                        question_id=question.id,
                        canonical_row_hash=row_hash,
                        row_json=canonical_row,
                        user_note=row.user_note,
                        tags_json=_json(row.tags),
                        topics_json=_json([topic.model_dump() for topic in row.topics]),
                        issues_json=_json([asdict(issue) for issue in issues]),
                        body_ready=number in ready_rows,
                    )
                    session.add(imported_row)
                    session.flush()
                    if row.attempt_id is None:
                        duplicate_rows += int(identical_row is not None)
                        continue
                    attempt = session.scalar(
                        select(BankAttemptModel).where(
                            BankAttemptModel.learner_id == learner_id,
                            BankAttemptModel.question_id == question.id,
                            BankAttemptModel.external_attempt_id == row.attempt_id,
                        )
                    )
                    if attempt is not None:
                        original = session.get(BankImportRowModel, attempt.import_row_id)
                        if original is None or original.canonical_row_hash != row_hash:
                            raise _Conflict(
                                RowIssue(number, "attempt_conflict", "Attempt content changed")
                            )
                        duplicate_rows += 1
                        continue
                    session.add(
                        BankAttemptModel(
                            learner_id=learner_id,
                            question_id=question.id,
                            external_attempt_id=row.attempt_id,
                            result=row.result,
                            occurred_at=row.occurred_at.isoformat() if row.occurred_at else None,
                            elapsed_ms=row.elapsed_ms,
                            imported_at=imported_at,
                            import_row_id=imported_row.id,
                        )
                    )
                    session.flush()
                    inserted_attempts += 1
                receipt = ImportReceipt(
                    import_id, inserted_questions, inserted_attempts, duplicate_rows, ()
                )
                record.receipt_json = _json(asdict(receipt))
                session.flush()
                return receipt
        except _Conflict as conflict:
            # session_factory has rolled back the entire transaction before this receipt escapes.
            return ImportReceipt(import_id, 0, 0, 0, (conflict.issue,))

    def iter_attempts(
        self,
        *,
        learner_id: str,
        after_id: int = 0,
        limit: int = 500,
    ) -> tuple[AttemptFact, ...]:
        _owner(learner_id)
        if (
            type(limit) is not int
            or not 1 <= limit <= 500
            or type(after_id) is not int
            or after_id < 0
        ):
            raise ValueError("Invalid attempt cursor or limit")
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    BankAttemptModel,
                    BankQuestionModel,
                    BankImportRowModel,
                    BankImportModel.provenance_json,
                )
                .join(BankQuestionModel, BankQuestionModel.id == BankAttemptModel.question_id)
                .join(BankImportRowModel, BankImportRowModel.id == BankAttemptModel.import_row_id)
                .join(BankImportModel, BankImportModel.id == BankImportRowModel.import_id)
                .where(BankAttemptModel.learner_id == learner_id, BankAttemptModel.id > after_id)
                .order_by(BankAttemptModel.id)
                .limit(limit)
            )
            return tuple(_fact(*row) for row in rows)

    def record_attempt(
        self,
        *,
        learner_id: str,
        key: QuestionKey,
        attempt_id: str,
        result: Result,
        occurred_at: datetime | None,
        elapsed_ms: int | None,
        topics: tuple[TopicLabel, ...] = (),
    ) -> AttemptFact:
        _owner(learner_id)
        key = QuestionKey.model_validate_json(key.model_dump_json())
        row = ImportRow(
            question_id=key.question_id,
            attempt_id=attempt_id,
            result=result,
            occurred_at=occurred_at,
            elapsed_ms=elapsed_ms,
            topics=topics,
        )
        if row.attempt_id is None:
            raise ValueError("Native attempt requires attempt_id")
        identity = _json([key.source, key.product, key.question_id, attempt_id])
        preview = preview_import(
            _json(
                {
                    "schema_version": 1,
                    "source": key.source,
                    "product": key.product,
                    "export_id": "native:" + _hash(identity),
                    "provenance": {"kind": "user_results", "description": "Server-graded attempt"},
                    "rows": [row.model_dump(mode="json")],
                }
            ).encode()
        )
        receipt = self._commit(preview, learner_id, _json({"kind": "study_hub_attempt"}))
        if receipt.conflicts:
            raise ValueError(receipt.conflicts[0].detail)
        with self._session_factory() as session:
            result_row = session.execute(
                select(
                    BankAttemptModel,
                    BankQuestionModel,
                    BankImportRowModel,
                    BankImportModel.provenance_json,
                )
                .join(BankQuestionModel, BankQuestionModel.id == BankAttemptModel.question_id)
                .join(BankImportRowModel, BankImportRowModel.id == BankAttemptModel.import_row_id)
                .join(BankImportModel, BankImportModel.id == BankImportRowModel.import_id)
                .where(
                    BankAttemptModel.learner_id == learner_id,
                    BankQuestionModel.source == key.source,
                    BankQuestionModel.product == key.product,
                    BankQuestionModel.external_question_id == key.question_id,
                    BankAttemptModel.external_attempt_id == attempt_id,
                )
            ).one()
            return _fact(*result_row)

    def get_question(self, key: QuestionKey) -> BankQuestion | None:
        """Trusted internal lookup; owner-facing consumers use list_ready_questions."""
        with self._session_factory() as session:
            question = _question(session, key)
            return _ready(question) if question else None

    def list_ready_questions(
        self,
        *,
        learner_id: str,
        course: str | None = None,
        exam: str | None = None,
        topic_ids: tuple[str, ...] = (),
    ) -> tuple[BankQuestion, ...]:
        _owner(learner_id)
        with self._session_factory() as session:
            records = session.scalars(
                select(BankQuestionModel)
                .join(BankImportRowModel, BankImportRowModel.question_id == BankQuestionModel.id)
                .join(BankImportModel, BankImportModel.id == BankImportRowModel.import_id)
                .where(
                    BankImportModel.learner_id == learner_id,
                    BankImportRowModel.body_ready.is_(True),
                    BankQuestionModel.body_ready.is_(True),
                )
                .distinct()
                .order_by(BankQuestionModel.id)
            )
            result = []
            for record in records:
                question = _ready(record)
                if question is None:
                    continue
                accepted = {
                    (t.axis, t.canonical_id)
                    for t in question.topics
                    if t.review_state == "accepted"
                }
                if course is not None and ("course", course) not in accepted:
                    continue
                if exam is not None and ("exam", exam) not in accepted:
                    continue
                if topic_ids and not any(("topic", topic_id) in accepted for topic_id in topic_ids):
                    continue
                result.append(question)
            return tuple(result)

    def set_topics(
        self,
        key: QuestionKey,
        topics: tuple[TopicLabel, ...],
        expected_content_hash: str,
        *,
        reviewer_context: str,
    ) -> None:
        _owner(reviewer_context)
        topic_json = _json([topic.model_dump(mode="json") for topic in topics])
        _TOPICS.validate_json(topic_json)
        with self._session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            question = _question(session, key)
            if (
                question is None
                or _ready(question) is None
                or question.content_hash != expected_content_hash
            ):
                raise ValueError("Question is absent, held or changed")
            session.add(
                BankTopicReviewModel(
                    question_id=question.id,
                    content_hash=expected_content_hash,
                    previous_topics_json=question.reviewed_topics_json,
                    new_topics_json=topic_json,
                    reviewer_context_json=_json({"actor": reviewer_context}),
                    reviewed_at=utc_now(),
                )
            )
            question.reviewed_topics_json = topic_json

    def review_rows(self, *, import_id: str, learner_id: str) -> tuple[ImportRow, ...]:
        _owner(learner_id)
        with self._session_factory() as session:
            record = session.get(BankImportModel, import_id)
            if record is None or record.learner_id != learner_id:
                raise ValueError("Import is not available to this learner")
            rows = session.scalars(
                select(BankImportRowModel)
                .where(BankImportRowModel.import_id == import_id)
                .order_by(BankImportRowModel.row_number)
            )
            return tuple(ImportRow.model_validate_json(row.row_json) for row in rows)

    def get_import(self, *, import_id: str, learner_id: str) -> tuple[ImportPreview, ImportReceipt]:
        """Load an owned external import and its original receipt for review."""
        _owner(learner_id)
        with self._session_factory() as session:
            record = session.get(BankImportModel, import_id)
            if record is None or record.learner_id != learner_id:
                raise ValueError("Import is not available to this learner")
            rows = session.scalars(
                select(BankImportRowModel)
                .where(BankImportRowModel.import_id == import_id)
                .order_by(BankImportRowModel.row_number)
            )
            preview = preview_import(
                _json(
                    {
                        "schema_version": 1,
                        "source": record.source,
                        "product": record.product,
                        "export_id": record.export_id,
                        "provenance": json.loads(record.provenance_json),
                        "rows": [json.loads(row.row_json) for row in rows],
                    }
                ).encode()
            )
            stored = json.loads(record.receipt_json)
            receipt = ImportReceipt(
                record.id,
                stored["inserted_questions"],
                stored["inserted_attempts"],
                stored["duplicate_rows"],
                (),
            )
            return preview, receipt
