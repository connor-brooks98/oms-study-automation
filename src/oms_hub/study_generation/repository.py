import re
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from oms_hub.db import Database
from oms_hub.files.atomic import sha256_file
from oms_hub.models import (
    CourseQuizDocumentModel,
    ExamQuizTabModel,
    ExistingArtifactImportModel,
    GenerationAttemptModel,
    GenerationJobModel,
    GoogleConnectionModel,
    LectureModel,
    NotebookMappingModel,
    NotebookScopeLeaseModel,
    NotebookSourceMappingModel,
    OutlineOutputModel,
    OutlineReplacementReviewModel,
    PublishedQuizFlagModel,
    PublishedQuizMediaModel,
    PublishedQuizModel,
    QuizOutputModel,
    StudioQuizImageOverrideModel,
    StudioQuizImageRequirementModel,
    StudioRunModel,
    StudyPromptSettingModel,
    StudyRevisionModel,
    UploadBatchModel,
    UploadItemModel,
)
from oms_hub.study_generation.domain import (
    GenerationJob,
    GenerationKind,
    GenerationStage,
    GenerationState,
    NativeQuiz,
    NotebookMapping,
    NotebookSourceBinding,
    OutlineRecord,
    PromptKind,
    PublishedQuizLibrarySection,
    PublishedQuizMediaRecord,
    PublishedQuizOrderDirection,
    PublishedQuizRecord,
    QuizMatchingQuestion,
    QuizRecord,
    SourceKind,
)
from oms_hub.study_generation.native_quiz import (
    image_requirements,
    parse_native_quiz,
    serialize_native_quiz,
)
from oms_hub.study_generation.practice_domain import QuizContentKind, QuizWorkflowKind
from oms_hub.study_generation.studio_domain import StudioRunStage, StudioRunState

_ACTIVE_STATES = {
    GenerationState.QUEUED.value,
    GenerationState.RUNNING.value,
    GenerationState.PAUSED.value,
}
_ANKI_PROMPT_DIRECTORY_KEY = "anki_curation_prompt_directory"


def _validate_question_kinds(quiz: NativeQuiz, content_kind: str) -> None:
    if (
        any(isinstance(question, QuizMatchingQuestion) for question in quiz.questions)
        and content_kind != QuizContentKind.PRACTICE_QUESTIONS.value
    ):
        raise ValueError("matching questions are limited to practice-question content")


class StudioPublicationRecoveryConflict(RuntimeError):
    """Active Studio publication ownership cannot be resolved safely."""


class DirectImportReviewer(Protocol):
    def to_native_quiz_in_session(
        self, session: Session, run_id: str, *, title: str
    ) -> NativeQuiz: ...


class ImportedOutlineReplacementRequired(RuntimeError):
    """An imported current outline may only be replaced by an explicit decision."""


@dataclass(frozen=True, slots=True)
class ImportedOutlineReplacementReview:
    generation_job_id: str
    lecture_id: int
    import_id: str
    operator: str
    reason: str
    confirmed_at: str


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


class GenerationRepository:
    def __init__(
        self,
        database: Database,
        accuracy_gate: object | None = None,
        practice_review: DirectImportReviewer | None = None,
    ):
        self.database = database
        self.accuracy_gate = accuracy_gate
        self.practice_review = practice_review

    def queue(self, lecture_id: int, kind: GenerationKind) -> GenerationJob:
        with self.database.session() as session:
            existing = session.scalar(
                select(GenerationJobModel)
                .where(
                    GenerationJobModel.lecture_id == lecture_id,
                    GenerationJobModel.kind == kind.value,
                    GenerationJobModel.state.in_(_ACTIVE_STATES),
                )
                .order_by(GenerationJobModel.created_at.desc())
            )
            if existing is not None:
                return self._job(existing)
            predecessor = session.scalar(
                select(GenerationJobModel)
                .where(
                    GenerationJobModel.lecture_id == lecture_id,
                    GenerationJobModel.kind == kind.value,
                    GenerationJobModel.state == GenerationState.FAILED.value,
                )
                .order_by(GenerationJobModel.created_at.desc())
            )
            model = GenerationJobModel(
                id=str(uuid4()),
                lecture_id=lecture_id,
                kind=kind.value,
                state=GenerationState.QUEUED.value,
                stage=GenerationStage.VALIDATE.value,
                supersedes_job_id=(predecessor.id if predecessor else None),
            )
            session.add(model)
            session.flush()
            return self._job(model)

    def set_prompt_path(self, kind: PromptKind, path: str) -> None:
        normalized = path.strip()
        if not normalized:
            raise ValueError("prompt path cannot be empty")
        with self.database.session() as session:
            model = session.get(StudyPromptSettingModel, kind.value)
            if model is None:
                session.add(StudyPromptSettingModel(kind=kind.value, path=normalized))
            else:
                model.path = normalized
                model.last_sha256 = None
                model.last_modified_at = None

    def prompt_path(self, kind: PromptKind) -> str | None:
        with self.database.session() as session:
            model = session.get(StudyPromptSettingModel, kind.value)
            return model.path if model is not None and model.path else None

    def set_anki_prompt_directory(self, path: str) -> None:
        normalized = path.strip()
        if not normalized:
            raise ValueError("Anki prompt directory cannot be empty")
        selected = Path(normalized)
        if not selected.is_dir():
            raise ValueError("Anki prompt directory is unavailable")
        with self.database.session() as session:
            model = session.get(StudyPromptSettingModel, _ANKI_PROMPT_DIRECTORY_KEY)
            if model is None:
                session.add(
                    StudyPromptSettingModel(
                        kind=_ANKI_PROMPT_DIRECTORY_KEY,
                        path=str(selected),
                    )
                )
            else:
                model.path = str(selected)
                model.last_sha256 = None
                model.last_modified_at = None

    def anki_prompt_directory(self) -> str | None:
        with self.database.session() as session:
            model = session.get(StudyPromptSettingModel, _ANKI_PROMPT_DIRECTORY_KEY)
            return model.path if model is not None and model.path else None

    def record_prompt_validation(
        self,
        kind: PromptKind,
        sha256: str,
        modified_at: str,
    ) -> None:
        with self.database.session() as session:
            model = session.get(StudyPromptSettingModel, kind.value)
            if model is None:
                raise KeyError(kind.value)
            model.last_sha256 = sha256
            model.last_modified_at = modified_at

    def save_google_status(
        self,
        *,
        state: str,
        account_email: str | None,
        notebook_state: str,
        gemini_state: str,
        docs_state: str,
        diagnostic: str | None,
        tested_at: str,
    ) -> None:
        with self.database.session() as session:
            model = session.get(GoogleConnectionModel, 1)
            if model is None:
                model = GoogleConnectionModel(id=1)
                session.add(model)
            model.state = state
            model.account_email = account_email
            model.notebook_state = notebook_state
            model.gemini_state = gemini_state
            model.docs_state = docs_state
            model.diagnostic = diagnostic
            model.last_tested_at = tested_at

    def google_status(self) -> GoogleConnectionModel | None:
        with self.database.session() as session:
            model = session.get(GoogleConnectionModel, 1)
            if model is None:
                return None
            session.expunge(model)
            return model

    def notebook_mapping(
        self,
        subject_key: str,
        exam_number: int,
    ) -> NotebookMapping | None:
        with self.database.session() as session:
            model = session.scalar(
                select(NotebookMappingModel).where(
                    NotebookMappingModel.subject_key == subject_key,
                    NotebookMappingModel.exam_number == exam_number,
                )
            )
            return self._notebook_mapping(model) if model is not None else None

    def notebook_mapping_by_remote_id(
        self,
        remote_notebook_id: str,
    ) -> NotebookMapping | None:
        with self.database.session() as session:
            model = session.scalar(
                select(NotebookMappingModel).where(
                    NotebookMappingModel.remote_notebook_id == remote_notebook_id
                )
            )
            return self._notebook_mapping(model) if model is not None else None

    def acquire_notebook_scope(
        self,
        subject_key: str,
        exam_number: int,
        owner_kind: str,
        owner_id: str,
        *,
        now: datetime | None = None,
        lease_duration: timedelta = timedelta(minutes=30),
    ) -> bool:
        """Atomically acquire or renew one logical NotebookLM mutation scope."""
        normalized_key = _normalize(subject_key)
        normalized_kind = owner_kind.strip()
        normalized_owner = owner_id.strip()
        if not normalized_key or not normalized_kind or not normalized_owner:
            raise ValueError("notebook scope owner fields cannot be empty")
        if exam_number < 1:
            raise ValueError("exam number must be positive")
        now = now or datetime.now(UTC)
        expires_at = (now + lease_duration).isoformat()
        with self.database.session() as session:
            result = session.execute(
                text(
                    "INSERT INTO notebook_scope_leases "
                    "(subject_key, exam_number, owner_kind, owner_id, "
                    "lease_expires_at, updated_at) "
                    "VALUES (:subject_key, :exam_number, :owner_kind, :owner_id, "
                    ":lease_expires_at, :updated_at) "
                    "ON CONFLICT(subject_key, exam_number) DO UPDATE SET "
                    "owner_kind=excluded.owner_kind, owner_id=excluded.owner_id, "
                    "lease_expires_at=excluded.lease_expires_at, "
                    "updated_at=excluded.updated_at "
                    "WHERE notebook_scope_leases.lease_expires_at <= :updated_at "
                    "OR (notebook_scope_leases.owner_kind=:owner_kind "
                    "AND notebook_scope_leases.owner_id=:owner_id)"
                ),
                {
                    "subject_key": normalized_key,
                    "exam_number": exam_number,
                    "owner_kind": normalized_kind[:30],
                    "owner_id": normalized_owner[:100],
                    "lease_expires_at": expires_at,
                    "updated_at": now.isoformat(),
                },
            )
            return cast(CursorResult[Any], result).rowcount == 1

    def release_notebook_scope(
        self,
        subject_key: str,
        exam_number: int,
        owner_kind: str,
        owner_id: str,
    ) -> bool:
        """Release only the lease still owned by this exact durable operation."""
        with self.database.session() as session:
            result = session.execute(
                delete(NotebookScopeLeaseModel).where(
                    NotebookScopeLeaseModel.subject_key == _normalize(subject_key),
                    NotebookScopeLeaseModel.exam_number == exam_number,
                    NotebookScopeLeaseModel.owner_kind == owner_kind.strip()[:30],
                    NotebookScopeLeaseModel.owner_id == owner_id.strip()[:100],
                )
            )
            return cast(CursorResult[Any], result).rowcount == 1

    def renew_notebook_scope(
        self,
        subject_key: str,
        exam_number: int,
        owner_kind: str,
        owner_id: str,
        *,
        now: datetime | None = None,
        lease_duration: timedelta = timedelta(minutes=30),
    ) -> bool:
        """Extend a live lease without reclaiming an expired or replaced owner."""
        now = now or datetime.now(UTC)
        with self.database.session() as session:
            result = session.execute(
                update(NotebookScopeLeaseModel)
                .where(
                    NotebookScopeLeaseModel.subject_key == _normalize(subject_key),
                    NotebookScopeLeaseModel.exam_number == exam_number,
                    NotebookScopeLeaseModel.owner_kind == owner_kind.strip()[:30],
                    NotebookScopeLeaseModel.owner_id == owner_id.strip()[:100],
                    NotebookScopeLeaseModel.lease_expires_at > now.isoformat(),
                )
                .values(
                    lease_expires_at=(now + lease_duration).isoformat(),
                    updated_at=now.isoformat(),
                )
            )
            return cast(CursorResult[Any], result).rowcount == 1

    def save_notebook_mapping(
        self,
        subject: str,
        subject_key: str,
        exam_number: int,
        remote_notebook_id: str,
        title: str,
    ) -> NotebookMapping:
        normalized_subject = subject.strip()
        normalized_key = subject_key.strip()
        normalized_remote_id = remote_notebook_id.strip()
        normalized_title = title.strip()
        if (
            not normalized_subject
            or not normalized_key
            or not normalized_remote_id
            or not normalized_title
        ):
            raise ValueError("notebook mapping fields cannot be empty")
        with self.database.session() as session:
            model = session.scalar(
                select(NotebookMappingModel).where(
                    NotebookMappingModel.subject_key == normalized_key,
                    NotebookMappingModel.exam_number == exam_number,
                )
            )
            if model is None:
                model = NotebookMappingModel(
                    subject=normalized_subject,
                    subject_key=normalized_key,
                    exam_number=exam_number,
                    remote_notebook_id=normalized_remote_id,
                    title=normalized_title,
                )
                session.add(model)
            else:
                model.subject = normalized_subject
                model.remote_notebook_id = normalized_remote_id
                model.title = normalized_title
            session.flush()
            return self._notebook_mapping(model)

    def source_binding(
        self,
        notebook_mapping_id: int,
        lecture_id: int,
        source_kind: SourceKind,
    ) -> NotebookSourceBinding | None:
        with self.database.session() as session:
            model = session.scalar(
                select(NotebookSourceMappingModel)
                .where(
                    NotebookSourceMappingModel.notebook_mapping_id == notebook_mapping_id,
                    NotebookSourceMappingModel.lecture_id == lecture_id,
                    NotebookSourceMappingModel.source_kind == source_kind.value,
                    NotebookSourceMappingModel.state == "ready",
                )
                .order_by(
                    NotebookSourceMappingModel.verified_at.desc(),
                    NotebookSourceMappingModel.id.desc(),
                )
            )
            return self._source_binding(model) if model is not None else None

    def bind_source(
        self,
        notebook_mapping_id: int,
        lecture_id: int,
        revision_id: int,
        source_kind: SourceKind,
        source_sha256: str,
        remote_source_id: str,
        display_title: str,
    ) -> NotebookSourceBinding:
        normalized_remote_id = remote_source_id.strip()
        normalized_title = display_title.strip()
        if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
            raise ValueError("source fingerprint is invalid")
        if not normalized_remote_id or not normalized_title:
            raise ValueError("source binding fields cannot be empty")
        with self.database.session() as session:
            session.execute(
                update(NotebookSourceMappingModel)
                .where(
                    NotebookSourceMappingModel.notebook_mapping_id == notebook_mapping_id,
                    NotebookSourceMappingModel.lecture_id == lecture_id,
                    NotebookSourceMappingModel.source_kind == source_kind.value,
                    NotebookSourceMappingModel.state == "ready",
                )
                .values(state="superseded")
            )
            model = session.scalar(
                select(NotebookSourceMappingModel).where(
                    NotebookSourceMappingModel.notebook_mapping_id == notebook_mapping_id,
                    NotebookSourceMappingModel.study_revision_id == revision_id,
                    NotebookSourceMappingModel.source_kind == source_kind.value,
                )
            )
            verified_at = datetime.now(UTC).isoformat()
            if model is None:
                model = NotebookSourceMappingModel(
                    notebook_mapping_id=notebook_mapping_id,
                    lecture_id=lecture_id,
                    study_revision_id=revision_id,
                    source_kind=source_kind.value,
                    source_sha256=source_sha256,
                    remote_source_id=normalized_remote_id,
                    display_title=normalized_title,
                    state="ready",
                    verified_at=verified_at,
                )
                session.add(model)
            else:
                model.lecture_id = lecture_id
                model.source_sha256 = source_sha256
                model.remote_source_id = normalized_remote_id
                model.display_title = normalized_title
                model.state = "ready"
                model.verified_at = verified_at
            session.flush()
            return self._source_binding(model)

    def claim_next(self, now: datetime) -> GenerationJob | None:
        with self.database.session() as session:
            model = session.scalar(
                select(GenerationJobModel)
                .where(
                    GenerationJobModel.state == GenerationState.QUEUED.value,
                    or_(
                        GenerationJobModel.next_attempt_at.is_(None),
                        GenerationJobModel.next_attempt_at <= now.isoformat(),
                    ),
                )
                .order_by(GenerationJobModel.created_at, GenerationJobModel.id)
                .limit(1)
            )
            if model is None:
                return None
            result = session.execute(
                update(GenerationJobModel)
                .where(
                    GenerationJobModel.id == model.id,
                    GenerationJobModel.state == GenerationState.QUEUED.value,
                )
                .values(
                    state=GenerationState.RUNNING.value,
                    attempts=GenerationJobModel.attempts + 1,
                    error=None,
                    next_attempt_at=None,
                )
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                return None
            session.flush()
            session.refresh(model)
            return self._job(model)

    def advance(
        self,
        job_id: str,
        stage: GenerationStage,
        **fields: object,
    ) -> GenerationJob:
        allowed = {
            "notebook_id",
            "pdf_source_id",
            "transcript_source_id",
            "notebook_answer",
            "gemini_quiz_id",
            "quiz_url",
            "prompt_path",
            "prompt_sha256",
            "pdf_revision_id",
            "transcript_revision_id",
        }
        if unknown := set(fields) - allowed:
            raise ValueError(f"unsupported job field(s): {sorted(unknown)}")
        with self.database.session() as session:
            model = session.get(GenerationJobModel, job_id)
            if model is None:
                raise KeyError(job_id)
            model.stage = stage.value
            for name, value in fields.items():
                setattr(model, name, value)
            session.flush()
            return self._job(model)

    def get(self, job_id: str) -> GenerationJob:
        with self.database.session() as session:
            model = session.get(GenerationJobModel, job_id)
            if model is None:
                raise KeyError(job_id)
            return self._job(model)

    def recover_interrupted(self) -> int:
        with self.database.session() as session:
            models = session.scalars(
                select(GenerationJobModel).where(
                    or_(
                        GenerationJobModel.state == GenerationState.RUNNING.value,
                        (
                            (GenerationJobModel.state == GenerationState.PAUSED.value)
                            & (GenerationJobModel.kind == GenerationKind.QUIZ.value)
                            & (GenerationJobModel.stage == GenerationStage.DOCS.value)
                            & GenerationJobModel.quiz_url.is_not(None)
                        ),
                    )
                )
            ).all()
            for model in models:
                model.state = GenerationState.QUEUED.value
                model.error = "requeued after an interrupted Hub process"
                model.next_attempt_at = None
            return len(models)

    def complete(self, job_id: str) -> GenerationJob:
        return self._set_state(
            job_id,
            GenerationState.COMPLETE,
            GenerationStage.COMPLETE,
            None,
        )

    def requeue(self, job_id: str) -> GenerationJob:
        with self.database.session() as session:
            model = session.get(GenerationJobModel, job_id)
            if model is None:
                raise KeyError(job_id)
            if model.state != GenerationState.PAUSED.value:
                return self._job(model)
            model.state = GenerationState.QUEUED.value
            model.error = None
            model.next_attempt_at = None
            session.flush()
            return self._job(model)

    def retry(self, job_id: str, error: str, delay: timedelta) -> GenerationJob:
        with self.database.session() as session:
            model = session.get(GenerationJobModel, job_id)
            if model is None:
                raise KeyError(job_id)
            model.state = GenerationState.QUEUED.value
            model.error = error[:500]
            model.next_attempt_at = (datetime.now(UTC) + delay).isoformat()
            session.flush()
            return self._job(model)

    def record_attempt(
        self,
        job_id: str,
        attempt_number: int,
        diagnostic_source: str,
        raw_response: str | None,
        error: str,
    ) -> None:
        with self.database.session() as session:
            session.add(
                GenerationAttemptModel(
                    job_id=job_id,
                    attempt_number=attempt_number,
                    diagnostic_source=diagnostic_source,
                    raw_response=raw_response,
                    error=error[:1000],
                )
            )

    def contract_failure_count(self, job_id: str) -> int:
        with self.database.session() as session:
            return int(
                session.scalar(
                    select(func.count(GenerationAttemptModel.id)).where(
                        GenerationAttemptModel.job_id == job_id,
                        GenerationAttemptModel.diagnostic_source == "contract",
                    )
                )
                or 0
            )

    def fail(self, job_id: str, error: str, *, paused: bool = False) -> GenerationJob:
        return self._set_state(
            job_id,
            GenerationState.PAUSED if paused else GenerationState.FAILED,
            None,
            error[:500],
        )

    def current_job(
        self,
        lecture_id: int,
        kind: GenerationKind,
    ) -> GenerationJob | None:
        with self.database.session() as session:
            model = session.scalar(
                select(GenerationJobModel)
                .where(
                    GenerationJobModel.lecture_id == lecture_id,
                    GenerationJobModel.kind == kind.value,
                )
                .order_by(GenerationJobModel.created_at.desc())
            )
            return self._job(model) if model is not None else None

    def record_outline(
        self,
        lecture_id: int,
        job_id: str,
        path: Path,
        sha256: str,
        *,
        replacement_review: ImportedOutlineReplacementReview | None = None,
    ) -> OutlineRecord:
        with self.database.session() as session:
            self._assert_outline_replacement_allowed(
                session,
                lecture_id,
                job_id,
                replacement_review=replacement_review,
            )
            session.execute(
                update(OutlineOutputModel)
                .where(OutlineOutputModel.lecture_id == lecture_id)
                .values(current=False)
            )
            model = session.scalar(
                select(OutlineOutputModel).where(OutlineOutputModel.job_id == job_id)
            )
            if model is None:
                model = OutlineOutputModel(
                    lecture_id=lecture_id,
                    job_id=job_id,
                    path=str(path),
                    sha256=sha256,
                    current=True,
                )
                session.add(model)
            else:
                model.path = str(path)
                model.sha256 = sha256
                model.current = True
            session.flush()
            return self._outline(model)

    def assert_outline_replacement_allowed(
        self,
        lecture_id: int,
        job_id: str,
        *,
        replacement_review: ImportedOutlineReplacementReview | None = None,
    ) -> None:
        """Fail closed before a writer can replace an imported canonical PDF."""
        with self.database.session() as session:
            self._assert_outline_replacement_allowed(
                session,
                lecture_id,
                job_id,
                replacement_review=replacement_review,
            )

    def _assert_outline_replacement_allowed(
        self,
        session: Session,
        lecture_id: int,
        job_id: str,
        *,
        replacement_review: ImportedOutlineReplacementReview | None,
    ) -> None:
        current = session.scalar(
            select(OutlineOutputModel).where(
                OutlineOutputModel.lecture_id == lecture_id,
                OutlineOutputModel.current.is_(True),
            )
        )
        if (
            current is not None
            and current.provenance_kind == "imported_notebooklm"
        ):
            review = session.get(OutlineReplacementReviewModel, job_id)
            if (
                replacement_review is None
                or review is None
                or replacement_review.generation_job_id != review.generation_job_id
                or review.lecture_id != lecture_id
                or review.import_id != current.import_id
                or not review.operator.strip()
                or not review.reason.strip()
            ):
                raise ImportedOutlineReplacementRequired(
                    "current imported outline requires a durable replacement review decision"
                )
            self._assert_replacement_review_eligible(
                session,
                lecture_id,
                job_id,
                current,
                review,
                expected_state=GenerationState.RUNNING,
            )

    def approve_imported_outline_replacement(
        self, lecture_id: int, job_id: str, operator: str, reason: str
    ) -> ImportedOutlineReplacementReview:
        operator = operator.strip()
        reason = reason.strip()
        if not operator or not reason:
            raise ValueError("replacement operator and reason are required")
        with self.database.session() as session:
            job = session.get(GenerationJobModel, job_id)
            current = session.scalar(
                select(OutlineOutputModel).where(
                    OutlineOutputModel.lecture_id == lecture_id,
                    OutlineOutputModel.current.is_(True),
                )
            )
            if (
                job is None
                or job.lecture_id != lecture_id
                or job.kind != GenerationKind.OUTLINE.value
                or current is None
                or current.provenance_kind != "imported_notebooklm"
                or current.import_id is None
            ):
                raise ImportedOutlineReplacementRequired(
                    "replacement review requires this lecture's current imported outline "
                    "and outline job"
                )
            if job.state != GenerationState.FAILED.value or job.stage != GenerationStage.PDF.value:
                raise ImportedOutlineReplacementRequired(
                    "replacement review requires the exact terminal failed PDF-stage outline job"
                )
            review = session.get(OutlineReplacementReviewModel, job_id)
            if review is None:
                review = OutlineReplacementReviewModel(
                    generation_job_id=job_id,
                    lecture_id=lecture_id,
                    import_id=current.import_id,
                    operator=operator,
                    reason=reason,
                )
            elif (
                review.lecture_id != lecture_id
                or review.import_id != current.import_id
                or review.operator != operator
                or review.reason != reason
            ):
                raise ImportedOutlineReplacementRequired(
                    "replacement job already has a different durable review decision"
                )
            self._assert_replacement_review_eligible(
                session,
                lecture_id,
                job_id,
                current,
                review,
                expected_state=GenerationState.FAILED,
            )
            if session.get(OutlineReplacementReviewModel, job_id) is None:
                session.add(review)
                # The SQLite trigger intentionally observes the terminal failed
                # state.  Persist that reviewed identity before requeueing.
                session.flush()
            job.state = GenerationState.QUEUED.value
            job.next_attempt_at = None
            job.error = None
            session.flush()
            return self._replacement_review(review)

    @staticmethod
    def _assert_replacement_review_eligible(
        session: Session,
        lecture_id: int,
        job_id: str,
        current: OutlineOutputModel,
        review: OutlineReplacementReviewModel,
        *,
        expected_state: GenerationState,
    ) -> None:
        job = session.get(GenerationJobModel, job_id)
        audit = session.get(ExistingArtifactImportModel, current.import_id)
        slide = session.get(StudyRevisionModel, current.slide_revision_id)
        transcript = session.get(StudyRevisionModel, current.transcript_revision_id)
        lecture = session.get(LectureModel, lecture_id)
        item = session.get(UploadItemModel, transcript.upload_item_id) if transcript else None
        batch = session.get(UploadBatchModel, item.batch_id) if item else None
        current_slide = session.scalar(
            select(StudyRevisionModel).where(
                StudyRevisionModel.lecture_id == lecture_id,
                StudyRevisionModel.kind == "slides",
                StudyRevisionModel.current.is_(True),
            )
        )
        current_transcript = session.scalar(
            select(StudyRevisionModel).where(
                StudyRevisionModel.lecture_id == lecture_id,
                StudyRevisionModel.kind == "transcripts",
                StudyRevisionModel.current.is_(True),
            )
        )
        if (
            job is None
            or job.lecture_id != lecture_id
            or job.kind != GenerationKind.OUTLINE.value
            or job.state != expected_state.value
            or job.stage != GenerationStage.PDF.value
            or not job.notebook_answer
            or not job.pdf_source_id
            or not job.transcript_source_id
            or job.pdf_revision_id != current.slide_revision_id
            or job.transcript_revision_id != current.transcript_revision_id
            or review.lecture_id != lecture_id
            or review.import_id != current.import_id
            or audit is None
            or audit.status != "complete"
            or audit.lecture_id != lecture_id
            or audit.outline_id != current.id
            or lecture is None
            or (audit.subject, audit.exam_number, audit.lecture_number, audit.topic)
            != (lecture.subject, lecture.exam_number, lecture.lecture_number, lecture.topic)
            or slide is None
            or transcript is None
            or current_slide is None
            or current_transcript is None
            or current_slide.id != slide.id
            or current_transcript.id != transcript.id
            or slide.id != audit.slide_revision_id
            or transcript.id != audit.transcript_revision_id
            or slide.source_sha256 != audit.slide_source_sha256
            or slide.derived_sha256 != audit.slide_pdf_sha256
            or transcript.source_sha256 != audit.transcript_sha256
            or transcript.derived_sha256 != audit.transcript_sha256
            or transcript.provenance_kind != "imported_cleaned"
            or transcript.import_id != audit.id
            or transcript.state != "current"
            or transcript.immutable_source_path != audit.immutable_transcript_path
            or transcript.immutable_derived_path != audit.immutable_transcript_path
            or transcript.canonical_source_path != audit.canonical_transcript_path
            or transcript.canonical_derived_path != audit.canonical_transcript_path
            or item is None
            or item.lecture_id != lecture_id
            or item.kind != "transcripts"
            or item.original_filename != audit.transcript_filename
            or item.staged_path != audit.immutable_transcript_path
            or item.sha256 != audit.transcript_sha256
            or item.state != "complete"
            or not item.manual_assignment
            or batch is None
            or batch.kind != "transcripts"
            or batch.state != "complete"
            or current.path != audit.canonical_outline_path
            or current.job_id is not None
            or current.provenance_kind != "imported_notebooklm"
            or current.import_id != audit.id
            or current.original_filename != audit.outline_filename
            or current.immutable_path != audit.immutable_outline_path
            or current.slide_revision_id != audit.slide_revision_id
            or current.slide_source_sha256 != audit.slide_source_sha256
            or current.slide_sha256 != audit.slide_pdf_sha256
            or current.transcript_revision_id != audit.transcript_revision_id
            or current.transcript_sha256 != audit.transcript_sha256
            or current.sha256 != audit.outline_sha256
            or not GenerationRepository._replacement_source_identity_matches(
                session, job, lecture_id, slide, transcript
            )
        ):
            raise ImportedOutlineReplacementRequired(
                "replacement review job or pinned imported sources are no longer eligible"
            )

    @staticmethod
    def _replacement_source_identity_matches(
        session: Session,
        job: GenerationJobModel,
        lecture_id: int,
        slide: StudyRevisionModel,
        transcript: StudyRevisionModel,
    ) -> bool:
        if not job.notebook_id or not job.pdf_source_id or not job.transcript_source_id:
            return False
        lecture = session.get(LectureModel, lecture_id)
        if lecture is None:
            return False
        mapping = session.scalar(
            select(NotebookMappingModel).where(
                NotebookMappingModel.remote_notebook_id == job.notebook_id,
                NotebookMappingModel.subject_key == _normalize(lecture.subject),
                NotebookMappingModel.exam_number == lecture.exam_number,
            )
        )
        if mapping is None:
            return False
        pdf = session.scalar(
            select(NotebookSourceMappingModel).where(
                NotebookSourceMappingModel.notebook_mapping_id == mapping.id,
                NotebookSourceMappingModel.lecture_id == lecture_id,
                NotebookSourceMappingModel.study_revision_id == slide.id,
                NotebookSourceMappingModel.source_kind == SourceKind.LECTURE_PDF.value,
                NotebookSourceMappingModel.source_sha256 == slide.derived_sha256,
                NotebookSourceMappingModel.remote_source_id == job.pdf_source_id,
                NotebookSourceMappingModel.state == "ready",
            )
        )
        transcript_binding = session.scalar(
            select(NotebookSourceMappingModel).where(
                NotebookSourceMappingModel.notebook_mapping_id == mapping.id,
                NotebookSourceMappingModel.lecture_id == lecture_id,
                NotebookSourceMappingModel.study_revision_id == transcript.id,
                NotebookSourceMappingModel.source_kind == SourceKind.CLEANED_TRANSCRIPT.value,
                NotebookSourceMappingModel.source_sha256 == transcript.derived_sha256,
                NotebookSourceMappingModel.remote_source_id == job.transcript_source_id,
                NotebookSourceMappingModel.state == "ready",
            )
        )
        return pdf is not None and transcript_binding is not None

    def imported_outline_replacement_review(
        self, lecture_id: int, job_id: str
    ) -> ImportedOutlineReplacementReview | None:
        with self.database.session() as session:
            review = session.get(OutlineReplacementReviewModel, job_id)
            if review is None or review.lecture_id != lecture_id:
                return None
            return self._replacement_review(review)

    def current_outline(self, lecture_id: int) -> OutlineRecord | None:
        with self.database.session() as session:
            model = session.scalar(
                select(OutlineOutputModel).where(
                    OutlineOutputModel.lecture_id == lecture_id,
                    OutlineOutputModel.current.is_(True),
                )
            )
            return self._outline(model) if model is not None else None

    def outline(self, outline_id: int) -> OutlineRecord | None:
        with self.database.session() as session:
            model = session.get(OutlineOutputModel, outline_id)
            return self._outline(model) if model is not None else None

    def record_quiz(
        self,
        lecture_id: int,
        job_id: str,
        url: str,
    ) -> QuizRecord:
        with self.database.session() as session:
            session.execute(
                update(QuizOutputModel)
                .where(QuizOutputModel.lecture_id == lecture_id)
                .values(current=False)
            )
            model = session.scalar(select(QuizOutputModel).where(QuizOutputModel.job_id == job_id))
            if model is None:
                model = QuizOutputModel(
                    lecture_id=lecture_id,
                    job_id=job_id,
                    url=url,
                    docs_synced=False,
                    current=True,
                )
                session.add(model)
            else:
                model.url = url
                model.docs_synced = False
                model.current = True
            session.flush()
            return self._quiz(model)

    def current_quiz(self, lecture_id: int) -> QuizRecord | None:
        with self.database.session() as session:
            model = session.scalar(
                select(QuizOutputModel).where(
                    QuizOutputModel.lecture_id == lecture_id,
                    QuizOutputModel.current.is_(True),
                )
            )
            return self._quiz(model) if model is not None else None

    def publish_quiz(
        self,
        lecture_id: int,
        job_id: str,
        quiz: NativeQuiz,
    ) -> PublishedQuizRecord:
        with self.database.session() as session:
            lecture = session.get(LectureModel, lecture_id)
            if lecture is None:
                raise ValueError("lecture was removed")
            # Lecture generation owns this title; Studio/import labels remain untouched.
            quiz = replace(quiz, title=lecture.topic.strip())
            _validate_question_kinds(quiz, QuizContentKind.LECTURE_QUIZ.value)
            self._validate_accuracy(quiz)
            model = session.scalar(
                select(PublishedQuizModel).where(
                    PublishedQuizModel.lecture_id == lecture_id,
                    PublishedQuizModel.active.is_(True),
                )
            )
            if model is None:
                model = PublishedQuizModel(
                    token=secrets.token_hex(32),
                    lecture_id=lecture_id,
                    job_id=job_id,
                    studio_run_id=None,
                    destination_subject=lecture.subject,
                    destination_subject_key=_normalize(lecture.subject),
                    destination_exam_number=lecture.exam_number,
                    label=quiz.title,
                    label_key=_normalize(quiz.title),
                    title=quiz.title,
                    payload_json=serialize_native_quiz(quiz),
                    content_kind=QuizContentKind.LECTURE_QUIZ.value,
                    version=1,
                    active=True,
                )
                model.display_order = self._next_display_order(session, model)
                session.add(model)
            elif model.job_id != job_id:
                model.job_id = job_id
                model.destination_subject = lecture.subject
                model.destination_subject_key = _normalize(lecture.subject)
                model.destination_exam_number = lecture.exam_number
                model.label = quiz.title
                model.label_key = _normalize(quiz.title)
                model.title = quiz.title
                model.payload_json = serialize_native_quiz(quiz)
                model.version += 1
            model.content_kind = QuizContentKind.LECTURE_QUIZ.value
            session.flush()
            return self._published_quiz(model)

    def published_quiz(self, token: str) -> PublishedQuizRecord | None:
        with self.database.session() as session:
            model = session.get(PublishedQuizModel, token)
            return self._published_quiz(model) if model is not None and model.active else None

    def published_quizzes(
        self,
        content_kinds: frozenset[QuizContentKind],
    ) -> tuple[PublishedQuizRecord, ...]:
        with self.database.session() as session:
            statement = (
                select(PublishedQuizModel)
                .where(PublishedQuizModel.active.is_(True))
                .where(
                    PublishedQuizModel.content_kind.in_(
                        [content_kind.value for content_kind in content_kinds]
                    )
                )
            )
            models = sorted(
                session.scalars(statement).all(),
                key=lambda model: self._published_quiz_order_key(session, model),
            )
            return tuple(self._published_quiz(model) for model in models)

    def rename_published_quiz(self, token: str, title: str) -> PublishedQuizRecord:
        """Update only a released quiz's display title and native payload title."""
        cleaned_title = title.strip()
        if not cleaned_title or len(cleaned_title) > 300:
            raise ValueError("title must contain between 1 and 300 characters")
        with self.database.session() as session:
            model = self._active_published_quiz_in_session(session, token)
            model.title = cleaned_title
            model.payload_json = serialize_native_quiz(
                replace(parse_native_quiz(model.payload_json), title=cleaned_title)
            )
            session.flush()
            return self._published_quiz(model)

    def replace_published_quiz_payload(self, token: str, payload_json: str) -> PublishedQuizRecord:
        """Atomically validate, replace, and version a private editor update."""
        quiz = parse_native_quiz(payload_json)
        with self.database.session() as session:
            model = self._active_published_quiz_in_session(session, token)
            _validate_question_kinds(quiz, model.content_kind)
            available_image_keys = set(
                session.scalars(
                    select(PublishedQuizMediaModel.image_key).where(
                        PublishedQuizMediaModel.quiz_token == token
                    )
                ).all()
            )
            unknown_images = {
                question.image_ref.key
                for question in quiz.questions
                if question.image_ref is not None
            } - available_image_keys
            if unknown_images:
                raise ValueError(
                    "quiz references unavailable image media: "
                    + ", ".join(sorted(unknown_images))
                )
            old_version = model.version
            # Question editing is intentionally title-neutral: a stale library form
            # must not undo a title PATCH that already succeeded in this session.
            quiz = replace(quiz, title=model.title)
            model.payload_json = serialize_native_quiz(quiz)
            model.version += 1
            session.execute(
                update(PublishedQuizFlagModel)
                .where(
                    PublishedQuizFlagModel.quiz_token == token,
                    PublishedQuizFlagModel.quiz_version == old_version,
                    PublishedQuizFlagModel.status == "open",
                )
                .values(status="resolved")
            )
            session.flush()
            return self._published_quiz(model)

    def record_published_quiz_flag(
        self, token: str, version: int, question_id: str, reason: str
    ) -> None:
        with self.database.session() as session:
            model = self._active_published_quiz_in_session(session, token)
            question_ids = {
                question.id for question in parse_native_quiz(model.payload_json).questions
            }
            if model.version != version or question_id not in question_ids:
                raise ValueError("quiz question is no longer current")
            session.execute(
                text(
                    "INSERT INTO published_quiz_flags "
                    "(quiz_token, quiz_version, question_id, reason, occurrence_count, status, "
                    "created_at, updated_at) "
                    "VALUES (:token, :version, :question_id, :reason, 1, 'open', :now, :now) "
                    "ON CONFLICT (quiz_token, quiz_version, question_id, reason) DO UPDATE SET "
                    "occurrence_count = published_quiz_flags.occurrence_count + 1, "
                    "status = 'open', updated_at = :now"
                ),
                {"token": token, "version": version, "question_id": question_id, "reason": reason,
                 "now": datetime.now(UTC).isoformat()},
            )

    def open_published_quiz_flags(self, token: str) -> tuple[dict[str, object], ...]:
        with self.database.session() as session:
            self._active_published_quiz_in_session(session, token)
            rows = session.scalars(
                select(PublishedQuizFlagModel)
                .where(
                    PublishedQuizFlagModel.quiz_token == token,
                    PublishedQuizFlagModel.status == "open",
                )
                .order_by(
                    PublishedQuizFlagModel.updated_at.desc(),
                    PublishedQuizFlagModel.id.desc(),
                )
            ).all()
            return tuple({"question_id": row.question_id, "reason": row.reason,
                          "count": row.occurrence_count, "version": row.quiz_version}
                         for row in rows)

    def open_published_quiz_flag_count(self, token: str) -> int:
        with self.database.session() as session:
            return int(
                session.scalar(
                    select(func.count(PublishedQuizFlagModel.id)).where(
                        PublishedQuizFlagModel.quiz_token == token,
                        PublishedQuizFlagModel.status == "open",
                    )
                )
                or 0
            )

    def move_published_quiz(
        self,
        token: str,
        section: PublishedQuizLibrarySection,
    ) -> PublishedQuizRecord:
        """Move a released quiz between the two public library sections."""
        with self.database.session() as session:
            model = self._active_published_quiz_in_session(session, token)
            source_scope = self._published_quiz_scope(session, model)
            source_section = self._library_section(model)
            self._normalize_scope_order(session, source_scope, source_section)

            if section is source_section:
                session.flush()
                return self._published_quiz(model)

            target_kind = self._content_kind_for_section(model, section)
            _validate_question_kinds(parse_native_quiz(model.payload_json), target_kind)
            model.content_kind = target_kind
            self._normalize_scope_order(
                session,
                source_scope,
                source_section,
                exclude_token=model.token,
            )
            target_scope = self._published_quiz_scope(session, model)
            destination = self._normalize_scope_order(
                session,
                target_scope,
                section,
                exclude_token=model.token,
            )
            model.display_order = len(destination) + 1
            session.flush()
            return self._published_quiz(model)

    def reorder_published_quiz(
        self,
        token: str,
        direction: PublishedQuizOrderDirection,
    ) -> PublishedQuizRecord:
        """Move a released quiz one position inside its canonical library scope."""
        with self.database.session() as session:
            model = self._active_published_quiz_in_session(session, token)
            ordered = self._normalize_scope_order(
                session,
                self._published_quiz_scope(session, model),
                self._library_section(model),
            )
            index = next(index for index, row in enumerate(ordered) if row.token == token)
            adjacent = index - 1 if direction is PublishedQuizOrderDirection.UP else index + 1
            if 0 <= adjacent < len(ordered):
                other = ordered[adjacent]
                model.display_order, other.display_order = (
                    other.display_order,
                    model.display_order,
                )
            session.flush()
            return self._published_quiz(model)

    def publish_studio_quiz(
        self,
        run_id: str,
        quiz: NativeQuiz,
    ) -> PublishedQuizRecord:
        with self.database.session() as session:
            run = session.get(StudioRunModel, run_id)
            if run is None:
                raise ValueError("Studio run was removed")
            _validate_question_kinds(quiz, run.content_kind)
            self._validate_accuracy(quiz)
            return self._publish_studio_quiz_in_session(session, run_id, quiz)

    def publish_and_complete_studio_run(
        self,
        run_id: str,
        quiz: NativeQuiz,
        notebook_id: str,
        raw_response: str,
    ) -> PublishedQuizRecord:
        """Atomically publish a generated Studio quiz and complete its run.

        The same-run branch also repairs the historical split-commit state: an
        active publication already owned by the run is adopted, not duplicated.
        """
        with self.database.session() as session:
            run = session.get(StudioRunModel, run_id)
            if run is None:
                raise ValueError("Studio run was removed")
            _validate_question_kinds(quiz, run.content_kind)
            self._validate_accuracy(quiz)
            owned = session.scalar(
                select(PublishedQuizModel).where(
                    PublishedQuizModel.studio_run_id == run_id,
                    PublishedQuizModel.active.is_(True),
                )
            )
            adopting_historical_publication = owned is not None
            if owned is None:
                created = self._publish_studio_quiz_in_session(session, run_id, quiz)
                # The helper returns a domain value, so reacquire the persisted row.
                published = session.get(PublishedQuizModel, created.token)
                assert published is not None
            else:
                published = owned
            run.state = StudioRunState.COMPLETE.value
            run.stage = StudioRunStage.COMPLETE.value
            if not adopting_historical_publication or run.notebook_id is None:
                run.notebook_id = notebook_id
            if not adopting_historical_publication or run.raw_response is None:
                run.raw_response = raw_response
            run.published_token = published.token
            run.error = None
            run.next_attempt_at = None
            session.flush()
            return self._published_quiz(published)

    def adopt_owned_studio_publication(
        self,
        run_id: str,
    ) -> PublishedQuizRecord | None:
        """Complete a historical split run without repeating its remote work."""
        with self.database.session() as session:
            run = session.get(StudioRunModel, run_id)
            if run is None:
                raise ValueError("Studio run was removed")
            published = session.scalar(
                select(PublishedQuizModel).where(
                    PublishedQuizModel.studio_run_id == run_id,
                    PublishedQuizModel.active.is_(True),
                )
            )
            if published is None:
                return None
            run.state = StudioRunState.COMPLETE.value
            run.stage = StudioRunStage.COMPLETE.value
            run.published_token = published.token
            run.error = None
            run.next_attempt_at = None
            session.flush()
            return self._published_quiz(published)

    def recover_owned_studio_publications(self) -> int:
        """Atomically converge publication owners and conflicting active runs.

        Older code could commit the publication and subsequently mark its run
        failed.  Such a run is outside the normal queued/retrying claim set, so
        startup recovery must discover it from publication ownership instead
        of waiting for the run worker to claim it. The publication owner and
        every conflicting active run are updated in this one SQL transaction.
        """
        with self.database.session() as session:
            publications = session.scalars(
                select(PublishedQuizModel).where(
                    PublishedQuizModel.studio_run_id.is_not(None),
                    PublishedQuizModel.active.is_(True),
                ).order_by(
                    PublishedQuizModel.destination_subject_key,
                    PublishedQuizModel.destination_exam_number,
                    PublishedQuizModel.label_key,
                    PublishedQuizModel.token,
                )
            ).all()
            recovered = 0
            seen_scopes: set[tuple[str, int, str]] = set()
            for published in publications:
                assert published.studio_run_id is not None
                scope = (
                    published.destination_subject_key,
                    published.destination_exam_number,
                    published.label_key,
                )
                if scope in seen_scopes:
                    raise StudioPublicationRecoveryConflict(
                        "startup recovery conflict: multiple active Studio publications "
                        f"exist for {scope[0]} exam {scope[1]} label {scope[2]}"
                    )
                seen_scopes.add(scope)
                run = session.get(StudioRunModel, published.studio_run_id)
                if run is None:
                    raise StudioPublicationRecoveryConflict(
                        "startup recovery conflict: active Studio publication "
                        f"{published.token} references missing run {published.studio_run_id}"
                    )
                owner_already_complete = (
                    run.state == StudioRunState.COMPLETE.value
                    and run.published_token == published.token
                )
                conflicts = session.scalars(
                    select(StudioRunModel).where(
                        StudioRunModel.destination_subject_key == scope[0],
                        StudioRunModel.destination_exam_number == scope[1],
                        StudioRunModel.label_key == scope[2],
                        StudioRunModel.id != run.id,
                        StudioRunModel.state.in_(
                            {
                                StudioRunState.QUEUED.value,
                                StudioRunState.RUNNING.value,
                                StudioRunState.RETRYING.value,
                            }
                        ),
                    )
                ).all()
                for conflict in conflicts:
                    if conflict.supersedes_run_id == run.id:
                        # A declared rerun intentionally coexists with its
                        # predecessor's durable publication until replacement.
                        # Leave it active for normal interrupted-run recovery.
                        continue
                    conflict.state = StudioRunState.FAILED.value
                    conflict.diagnostic_source = "recovery"
                    conflict.error = (
                        f"active publication {published.token} is owned by Studio run "
                        f"{run.id}; conflicting run retired during startup recovery"
                    )
                    conflict.next_attempt_at = None
                run.state = StudioRunState.COMPLETE.value
                run.stage = StudioRunStage.COMPLETE.value
                run.published_token = published.token
                run.error = None
                run.next_attempt_at = None
                if not owner_already_complete:
                    recovered += 1
            session.flush()
            return recovered

    def prepare_studio_run_chat(self, run_id: str) -> bool:
        """Resolve publication ownership before a claimed run may do remote work.

        Returns true when the scope has no active publication or when a rerun
        is replacing its declared predecessor. Owning runs adopt their durable
        publication; unrelated non-owning runs enter a targeted terminal
        recovery state. The check and state transition share one SQL transaction
        so the worker never merely returns with a running claim.
        """
        with self.database.session() as session:
            run = session.get(StudioRunModel, run_id)
            if run is None:
                raise ValueError("Studio run was removed")
            publications = session.scalars(
                select(PublishedQuizModel).where(
                    PublishedQuizModel.studio_run_id.is_not(None),
                    PublishedQuizModel.destination_subject_key
                    == run.destination_subject_key,
                    PublishedQuizModel.destination_exam_number
                    == run.destination_exam_number,
                    PublishedQuizModel.label_key == run.label_key,
                    PublishedQuizModel.active.is_(True),
                )
            ).all()
            if not publications:
                run.stage = StudioRunStage.CHAT.value
                session.flush()
                return True
            if len(publications) > 1:
                raise StudioPublicationRecoveryConflict(
                    "worker recovery conflict: multiple active Studio publications "
                    f"exist for {run.destination_subject_key} exam "
                    f"{run.destination_exam_number} label {run.label_key}"
                )
            published = publications[0]
            assert published.studio_run_id is not None
            if published.studio_run_id == run.id:
                run.state = StudioRunState.COMPLETE.value
                run.stage = StudioRunStage.COMPLETE.value
                run.published_token = published.token
                run.error = None
                run.next_attempt_at = None
            elif (
                run.supersedes_run_id is not None
                and published.studio_run_id == run.supersedes_run_id
            ):
                # A rerun deliberately keeps its predecessor's publication live
                # until the replacement is ready. That durable predecessor does
                # not make the successor's remote work unsafe.
                run.stage = StudioRunStage.CHAT.value
                session.flush()
                return True
            else:
                run.state = StudioRunState.FAILED.value
                run.diagnostic_source = "recovery"
                run.error = (
                    f"active publication {published.token} is owned by Studio run "
                    f"{published.studio_run_id}; remote chat was not created"
                )
                run.next_attempt_at = None
            session.flush()
            return False

    @staticmethod
    def _require_unreserved_studio_publication_scope(
        session: Session,
        run: StudioRunModel,
    ) -> None:
        competing_active_run = session.scalar(
            select(StudioRunModel).where(
                StudioRunModel.destination_subject_key
                == run.destination_subject_key,
                StudioRunModel.destination_exam_number
                == run.destination_exam_number,
                StudioRunModel.label_key == run.label_key,
                StudioRunModel.id != run.id,
                StudioRunModel.state.in_(
                    {
                        StudioRunState.QUEUED.value,
                        StudioRunState.RUNNING.value,
                        StudioRunState.RETRYING.value,
                    }
                ),
            )
        )
        if competing_active_run is not None:
            raise ValueError(
                "another active Studio run owns this publication scope; "
                "publication was not changed"
            )

    def _publish_studio_quiz_in_session(
        self, session: Session, run_id: str, quiz: NativeQuiz
    ) -> PublishedQuizRecord:
        run = session.get(StudioRunModel, run_id)
        if run is None:
            raise ValueError("Studio run was removed")
        _validate_question_kinds(quiz, run.content_kind)
        self._require_unreserved_studio_publication_scope(session, run)
        model = None
        if run.supersedes_run_id:
            model = session.scalar(
                select(PublishedQuizModel).where(
                    PublishedQuizModel.studio_run_id == run.supersedes_run_id
                )
            )
        if model is None:
            duplicate = session.scalar(
                select(PublishedQuizModel).where(
                    PublishedQuizModel.studio_run_id.is_not(None),
                    PublishedQuizModel.destination_subject_key == run.destination_subject_key,
                    PublishedQuizModel.destination_exam_number == run.destination_exam_number,
                    PublishedQuizModel.label_key == run.label_key,
                    PublishedQuizModel.active.is_(True),
                )
            )
            if duplicate is not None:
                raise ValueError(
                    "a published Studio quiz already uses this label for that exam"
                )
            model = PublishedQuizModel(
                token=secrets.token_hex(32),
                lecture_id=None,
                job_id=None,
                studio_run_id=run.id,
                destination_subject=run.destination_subject,
                destination_subject_key=run.destination_subject_key,
                destination_exam_number=run.destination_exam_number,
                label=run.label,
                label_key=run.label_key,
                title=quiz.title,
                payload_json=serialize_native_quiz(quiz),
                content_kind=run.content_kind,
                version=1,
                active=True,
            )
            model.display_order = self._next_display_order(session, model)
            session.add(model)
        else:
            previous = session.get(StudioRunModel, run.supersedes_run_id)
            if previous is not None:
                previous.published_token = None
            model.studio_run_id = run.id
            model.destination_subject = run.destination_subject
            model.destination_subject_key = run.destination_subject_key
            model.destination_exam_number = run.destination_exam_number
            model.label = run.label
            model.label_key = run.label_key
            model.title = quiz.title
            model.payload_json = serialize_native_quiz(quiz)
            model.content_kind = run.content_kind
            model.version += 1
            model.active = True
        session.flush()
        return self._published_quiz(model)

    def publish_reviewed_studio_quiz(
        self,
        run_id: str,
    ) -> PublishedQuizRecord:
        with self.database.session() as session:
            run = session.get(StudioRunModel, run_id)
            if run is None:
                raise KeyError(run_id)
            if run.published_token:
                existing = session.get(PublishedQuizModel, run.published_token)
                if (
                    existing is not None
                    and existing.active
                    and existing.studio_run_id == run_id
                ):
                    return self._published_quiz(existing)
            self._require_unreserved_studio_publication_scope(session, run)
            if run.workflow_kind == QuizWorkflowKind.DIRECT_IMPORT.value:
                if run.state != StudioRunState.AWAITING_REVIEW.value:
                    raise ValueError("imported quiz is not awaiting question review")
                if self.practice_review is None:
                    raise ValueError("imported question review is not configured")
                quiz = self.practice_review.to_native_quiz_in_session(
                    session,
                    run.id,
                    title=run.label,
                )
                _validate_question_kinds(quiz, run.content_kind)
                self._validate_accuracy(quiz)
                return self._publish_direct_import_in_session(session, run, quiz)
            if run.state != StudioRunState.AWAITING_IMAGES.value:
                raise ValueError("Studio run is not awaiting image publication")
            if not run.draft_payload_json:
                raise ValueError("Studio quiz draft is missing")

            draft = parse_native_quiz(run.draft_payload_json)
            requirement_models = session.scalars(
                select(StudioQuizImageRequirementModel)
                .where(StudioQuizImageRequirementModel.run_id == run_id)
                .order_by(StudioQuizImageRequirementModel.id)
            ).all()
            requirements_by_key = {
                requirement.image_key: requirement
                for requirement in requirement_models
            }
            overridden = frozenset(
                session.scalars(
                    select(StudioQuizImageOverrideModel.question_id).where(
                        StudioQuizImageOverrideModel.run_id == run_id
                    )
                ).all()
            )
            active_question_ids_by_key: dict[str, list[str]] = {}
            for question in draft.questions:
                if question.image_ref is not None and question.id not in overridden:
                    active_question_ids_by_key.setdefault(
                        question.image_ref.key,
                        [],
                    ).append(question.id)
            unresolved = [
                requirement.key
                for requirement in image_requirements(draft)
                if active_question_ids_by_key.get(requirement.key)
                and not self._stored_image_is_complete(
                    requirements_by_key.get(requirement.key)
                )
            ]
            if unresolved:
                raise ValueError(
                    "quiz images are still required: " + ", ".join(unresolved)
                )
            quiz = replace(
                draft,
                questions=tuple(
                    replace(question, image_ref=None)
                    if question.id in overridden
                    else question
                    for question in draft.questions
                ),
            )
            _validate_question_kinds(quiz, run.content_kind)
            self._validate_accuracy(quiz)

            model = None
            if run.supersedes_run_id:
                model = session.scalar(
                    select(PublishedQuizModel).where(
                        PublishedQuizModel.studio_run_id == run.supersedes_run_id
                    )
                )
            if model is None:
                duplicate = session.scalar(
                    select(PublishedQuizModel).where(
                        PublishedQuizModel.studio_run_id.is_not(None),
                        PublishedQuizModel.destination_subject_key
                        == run.destination_subject_key,
                        PublishedQuizModel.destination_exam_number
                        == run.destination_exam_number,
                        PublishedQuizModel.label_key == run.label_key,
                        PublishedQuizModel.active.is_(True),
                    )
                )
                if duplicate is not None:
                    raise ValueError(
                        "a published Studio quiz already uses this label for that exam"
                    )
                model = PublishedQuizModel(
                    token=secrets.token_hex(32),
                    lecture_id=None,
                    job_id=None,
                    studio_run_id=run.id,
                    destination_subject=run.destination_subject,
                    destination_subject_key=run.destination_subject_key,
                    destination_exam_number=run.destination_exam_number,
                    label=run.label,
                    label_key=run.label_key,
                    title=quiz.title,
                    payload_json=serialize_native_quiz(quiz),
                    content_kind=run.content_kind,
                    version=1,
                    active=True,
                )
                model.display_order = self._next_display_order(session, model)
                session.add(model)
                session.flush()
            else:
                previous = session.get(StudioRunModel, run.supersedes_run_id)
                if previous is not None:
                    previous.published_token = None
                model.studio_run_id = run.id
                model.destination_subject = run.destination_subject
                model.destination_subject_key = run.destination_subject_key
                model.destination_exam_number = run.destination_exam_number
                model.label = run.label
                model.label_key = run.label_key
                model.title = quiz.title
                model.payload_json = serialize_native_quiz(quiz)
                model.content_kind = run.content_kind
                model.version += 1
                model.active = True

            session.execute(
                delete(PublishedQuizMediaModel).where(
                    PublishedQuizMediaModel.quiz_token == model.token
                )
            )
            for image_key in active_question_ids_by_key:
                requirement = requirements_by_key[image_key]
                assert requirement.asset_path is not None
                assert requirement.asset_sha256 is not None
                assert requirement.media_type is not None
                assert requirement.width is not None
                assert requirement.height is not None
                session.add(
                    PublishedQuizMediaModel(
                        quiz_token=model.token,
                        image_key=image_key,
                        path=requirement.asset_path,
                        sha256=requirement.asset_sha256,
                        media_type=requirement.media_type,
                        width=requirement.width,
                        height=requirement.height,
                        alt_text=requirement.description,
                    )
                )
            run.state = StudioRunState.COMPLETE.value
            run.stage = StudioRunStage.COMPLETE.value
            run.published_token = model.token
            run.error = None
            run.next_attempt_at = None
            session.flush()
            return self._published_quiz(model)

    def _publish_direct_import_in_session(
        self,
        session: Session,
        run: StudioRunModel,
        quiz: NativeQuiz,
    ) -> PublishedQuizRecord:
        _validate_question_kinds(quiz, run.content_kind)
        requirements_by_key = {
            item.image_key: item
            for item in session.scalars(
                select(StudioQuizImageRequirementModel).where(
                    StudioQuizImageRequirementModel.run_id == run.id
                )
            ).all()
        }
        active_image_keys = tuple(
            dict.fromkeys(
                question.image_ref.key
                for question in quiz.questions
                if question.image_ref is not None
            )
        )
        unresolved = [
            key
            for key in active_image_keys
            if not self._stored_image_is_complete(requirements_by_key.get(key))
        ]
        if unresolved:
            raise ValueError("quiz images are still required: " + ", ".join(unresolved))
        model = None
        if run.supersedes_run_id:
            model = session.scalar(
                select(PublishedQuizModel).where(
                    PublishedQuizModel.studio_run_id == run.supersedes_run_id
                )
            )
        if model is None:
            duplicate = session.scalar(
                select(PublishedQuizModel).where(
                    PublishedQuizModel.studio_run_id.is_not(None),
                    PublishedQuizModel.destination_subject_key == run.destination_subject_key,
                    PublishedQuizModel.destination_exam_number == run.destination_exam_number,
                    PublishedQuizModel.label_key == run.label_key,
                    PublishedQuizModel.active.is_(True),
                )
            )
            if duplicate is not None:
                raise ValueError("a published Studio quiz already uses this label for that exam")
            model = PublishedQuizModel(
                token=secrets.token_hex(32),
                lecture_id=None,
                job_id=None,
                studio_run_id=run.id,
                destination_subject=run.destination_subject,
                destination_subject_key=run.destination_subject_key,
                destination_exam_number=run.destination_exam_number,
                label=run.label,
                label_key=run.label_key,
                title=quiz.title,
                payload_json=serialize_native_quiz(quiz),
                content_kind=run.content_kind,
                version=1,
                active=True,
            )
            model.display_order = self._next_display_order(session, model)
            session.add(model)
        else:
            previous = session.get(StudioRunModel, run.supersedes_run_id)
            if previous is not None:
                previous.published_token = None
            model.studio_run_id = run.id
            model.destination_subject = run.destination_subject
            model.destination_subject_key = run.destination_subject_key
            model.destination_exam_number = run.destination_exam_number
            model.label = run.label
            model.label_key = run.label_key
            model.title = quiz.title
            model.payload_json = serialize_native_quiz(quiz)
            model.content_kind = run.content_kind
            model.version += 1
            model.active = True
        session.flush()
        session.execute(
            delete(PublishedQuizMediaModel).where(
                PublishedQuizMediaModel.quiz_token == model.token
            )
        )
        for image_key in active_image_keys:
            requirement = requirements_by_key[image_key]
            assert requirement.asset_path is not None
            assert requirement.asset_sha256 is not None
            assert requirement.media_type is not None
            assert requirement.width is not None
            assert requirement.height is not None
            session.add(
                PublishedQuizMediaModel(
                    quiz_token=model.token,
                    image_key=image_key,
                    path=requirement.asset_path,
                    sha256=requirement.asset_sha256,
                    media_type=requirement.media_type,
                    width=requirement.width,
                    height=requirement.height,
                    alt_text=requirement.description,
                )
            )
        run.state = StudioRunState.COMPLETE.value
        run.stage = StudioRunStage.COMPLETE.value
        run.published_token = model.token
        run.error = None
        run.next_attempt_at = None
        session.flush()
        return self._published_quiz(model)

    def published_quiz_media(
        self,
        token: str,
    ) -> tuple[PublishedQuizMediaRecord, ...]:
        with self.database.session() as session:
            published = session.get(PublishedQuizModel, token)
            if published is None or not published.active:
                return ()
            models = session.scalars(
                select(PublishedQuizMediaModel)
                .where(PublishedQuizMediaModel.quiz_token == token)
                .order_by(PublishedQuizMediaModel.id)
            ).all()
            return tuple(self._published_quiz_media(model) for model in models)

    def published_quiz_media_item(
        self,
        token: str,
        image_key: str,
    ) -> PublishedQuizMediaRecord | None:
        with self.database.session() as session:
            published = session.get(PublishedQuizModel, token)
            if published is None or not published.active:
                return None
            model = session.scalar(
                select(PublishedQuizMediaModel).where(
                    PublishedQuizMediaModel.quiz_token == token,
                    PublishedQuizMediaModel.image_key == image_key,
                )
            )
            return None if model is None else self._published_quiz_media(model)

    def unpublish_quiz(self, token: str) -> str:
        with self.database.session() as session:
            return self._unpublish_quiz_in_session(session, token)

    def unpublish_studio_quiz(self, run_id: str) -> str:
        with self.database.session() as session:
            model = session.scalar(
                select(PublishedQuizModel).where(
                    PublishedQuizModel.studio_run_id == run_id,
                    PublishedQuizModel.active.is_(True),
                )
            )
            if model is None:
                raise KeyError(run_id)
            return self._unpublish_quiz_in_session(session, model.token)

    @staticmethod
    def _unpublish_quiz_in_session(session: Session, token: str) -> str:
        model = session.get(PublishedQuizModel, token)
        if model is None or not model.active:
            raise KeyError(token)
        model.active = False
        if model.studio_run_id is not None:
            run = session.get(StudioRunModel, model.studio_run_id)
            if run is not None and run.published_token == token:
                run.published_token = None
        return model.token

    @staticmethod
    def _active_published_quiz_in_session(
        session: Session,
        token: str,
    ) -> PublishedQuizModel:
        model = session.get(PublishedQuizModel, token)
        if model is None or not model.active:
            raise KeyError(token)
        return model

    @staticmethod
    def _library_section(
        model: PublishedQuizModel,
    ) -> PublishedQuizLibrarySection:
        if model.content_kind == QuizContentKind.PRACTICE_QUESTIONS.value:
            return PublishedQuizLibrarySection.PRACTICE_QUESTIONS
        return PublishedQuizLibrarySection.QUIZZES

    @staticmethod
    def _content_kind_for_section(
        model: PublishedQuizModel,
        section: PublishedQuizLibrarySection,
    ) -> str:
        if section is PublishedQuizLibrarySection.PRACTICE_QUESTIONS:
            return QuizContentKind.PRACTICE_QUESTIONS.value
        if model.lecture_id is not None:
            return QuizContentKind.LECTURE_QUIZ.value
        return QuizContentKind.EXAM_REVIEW.value

    @staticmethod
    def _published_quiz_scope(
        session: Session,
        model: PublishedQuizModel,
    ) -> tuple[str, int]:
        if model.lecture_id is not None:
            lecture = session.get(LectureModel, model.lecture_id)
            if lecture is not None:
                return (_normalize(lecture.subject), lecture.exam_number)
        return (
            _normalize(model.destination_subject_key or model.destination_subject),
            model.destination_exam_number,
        )

    @classmethod
    def _published_quiz_order_key(
        cls,
        session: Session,
        model: PublishedQuizModel,
    ) -> tuple[str, int, int, int, int, str, str]:
        scope = cls._published_quiz_scope(session, model)
        lecture = (
            session.get(LectureModel, model.lecture_id)
            if model.lecture_id is not None
            else None
        )
        return (
            scope[0],
            scope[1],
            model.display_order,
            0 if lecture is not None else 1,
            lecture.lecture_number if lecture is not None else 0,
            model.title.casefold(),
            model.token,
        )

    @classmethod
    def _normalize_scope_order(
        cls,
        session: Session,
        scope: tuple[str, int],
        section: PublishedQuizLibrarySection,
        *,
        exclude_token: str | None = None,
    ) -> list[PublishedQuizModel]:
        models = session.scalars(
            select(PublishedQuizModel).where(PublishedQuizModel.active.is_(True))
        ).all()
        scoped = [
            model
            for model in models
            if model.token != exclude_token
            and cls._published_quiz_scope(session, model) == scope
            and cls._library_section(model) is section
        ]
        scoped.sort(key=lambda model: cls._published_quiz_order_key(session, model))
        for index, model in enumerate(scoped, start=1):
            model.display_order = index
        return scoped

    @classmethod
    def _next_display_order(
        cls,
        session: Session,
        model: PublishedQuizModel,
    ) -> int:
        scope = cls._published_quiz_scope(session, model)
        section = cls._library_section(model)
        existing = [
            row.display_order
            for row in session.scalars(
                select(PublishedQuizModel).where(PublishedQuizModel.active.is_(True))
            ).all()
            if cls._published_quiz_scope(session, row) == scope
            and cls._library_section(row) is section
        ]
        return max(existing, default=0) + 1

    def course_document(self, subject_key: str) -> CourseQuizDocumentModel | None:
        with self.database.session() as session:
            model = session.get(CourseQuizDocumentModel, subject_key)
            if model is not None:
                session.expunge(model)
            return model

    def save_course_document(
        self,
        subject: str,
        subject_key: str,
        document_id: str,
        title: str,
    ) -> None:
        with self.database.session() as session:
            model = session.get(CourseQuizDocumentModel, subject_key)
            if model is None:
                session.add(
                    CourseQuizDocumentModel(
                        subject_key=subject_key,
                        subject=subject,
                        document_id=document_id,
                        title=title,
                    )
                )
            else:
                if model.document_id != document_id:
                    session.execute(
                        delete(ExamQuizTabModel).where(ExamQuizTabModel.subject_key == subject_key)
                    )
                model.subject = subject
                model.document_id = document_id
                model.title = title

    def exam_tab(
        self,
        subject_key: str,
        exam_number: int,
    ) -> ExamQuizTabModel | None:
        with self.database.session() as session:
            model = session.scalar(
                select(ExamQuizTabModel).where(
                    ExamQuizTabModel.subject_key == subject_key,
                    ExamQuizTabModel.exam_number == exam_number,
                )
            )
            if model is not None:
                session.expunge(model)
            return model

    def save_exam_tab(
        self,
        subject_key: str,
        exam_number: int,
        tab_id: str,
    ) -> None:
        with self.database.session() as session:
            model = session.scalar(
                select(ExamQuizTabModel).where(
                    ExamQuizTabModel.subject_key == subject_key,
                    ExamQuizTabModel.exam_number == exam_number,
                )
            )
            if model is None:
                session.add(
                    ExamQuizTabModel(
                        subject_key=subject_key,
                        exam_number=exam_number,
                        tab_id=tab_id,
                    )
                )
            else:
                model.tab_id = tab_id

    def _set_state(
        self,
        job_id: str,
        state: GenerationState,
        stage: GenerationStage | None,
        error: str | None,
    ) -> GenerationJob:
        with self.database.session() as session:
            model = session.get(GenerationJobModel, job_id)
            if model is None:
                raise KeyError(job_id)
            model.state = state.value
            if stage is not None:
                model.stage = stage.value
            model.error = error
            session.flush()
            return self._job(model)

    def _validate_accuracy(self, quiz: NativeQuiz) -> None:
        gate = self.accuracy_gate
        if gate is not None:
            validate = getattr(gate, "validate", None)
            if callable(validate):
                validate(quiz)

    @staticmethod
    def _job(model: GenerationJobModel) -> GenerationJob:
        return GenerationJob(
            id=model.id,
            lecture_id=model.lecture_id,
            kind=GenerationKind(model.kind),
            state=GenerationState(model.state),
            stage=GenerationStage(model.stage),
            attempts=model.attempts,
            error=model.error,
            prompt_path=model.prompt_path,
            prompt_sha256=model.prompt_sha256,
            pdf_revision_id=model.pdf_revision_id,
            transcript_revision_id=model.transcript_revision_id,
            notebook_id=model.notebook_id,
            pdf_source_id=model.pdf_source_id,
            transcript_source_id=model.transcript_source_id,
            notebook_answer=model.notebook_answer,
            gemini_quiz_id=model.gemini_quiz_id,
            supersedes_job_id=model.supersedes_job_id,
            quiz_url=model.quiz_url,
        )

    @staticmethod
    def _outline(model: OutlineOutputModel) -> OutlineRecord:
        return OutlineRecord(
            model.id,
            model.lecture_id,
            model.job_id,
            Path(model.path),
            model.sha256,
            model.current,
            model.provenance_kind,
            model.original_filename,
            Path(model.immutable_path) if model.immutable_path else None,
            model.slide_revision_id,
            model.slide_sha256,
            model.slide_source_sha256,
            model.transcript_revision_id,
            model.transcript_sha256,
            model.import_id,
        )

    @staticmethod
    def _replacement_review(
        model: OutlineReplacementReviewModel,
    ) -> ImportedOutlineReplacementReview:
        return ImportedOutlineReplacementReview(
            model.generation_job_id,
            model.lecture_id,
            model.import_id,
            model.operator,
            model.reason,
            model.confirmed_at,
        )

    @staticmethod
    def _quiz(model: QuizOutputModel) -> QuizRecord:
        return QuizRecord(
            model.id,
            model.lecture_id,
            model.job_id,
            model.url,
            model.current,
        )

    @staticmethod
    def _published_quiz(model: PublishedQuizModel) -> PublishedQuizRecord:
        quiz = parse_native_quiz(model.payload_json)
        return PublishedQuizRecord(
            model.token,
            model.lecture_id,
            model.job_id,
            model.studio_run_id,
            model.destination_subject,
            model.destination_subject_key,
            model.destination_exam_number,
            model.label,
            model.title,
            quiz,
            model.version,
            model.active,
            model.content_kind,
            model.display_order,
        )

    @staticmethod
    def _stored_image_is_complete(
        model: StudioQuizImageRequirementModel | None,
    ) -> bool:
        if not (
            model is not None
            and model.asset_path
            and model.asset_sha256
            and model.media_type
            and model.width is not None
            and model.height is not None
        ):
            return False
        path = Path(model.asset_path)
        try:
            return path.is_file() and sha256_file(path) == model.asset_sha256
        except OSError:
            return False

    @staticmethod
    def _published_quiz_media(
        model: PublishedQuizMediaModel,
    ) -> PublishedQuizMediaRecord:
        return PublishedQuizMediaRecord(
            model.quiz_token,
            model.image_key,
            Path(model.path),
            model.sha256,
            model.media_type,
            model.width,
            model.height,
            model.alt_text,
        )

    @staticmethod
    def _notebook_mapping(model: NotebookMappingModel) -> NotebookMapping:
        return NotebookMapping(
            model.id,
            model.subject,
            model.subject_key,
            model.exam_number,
            model.remote_notebook_id,
            model.title,
        )

    @staticmethod
    def _source_binding(
        model: NotebookSourceMappingModel,
    ) -> NotebookSourceBinding:
        return NotebookSourceBinding(
            model.id,
            model.notebook_mapping_id,
            model.lecture_id,
            model.study_revision_id,
            SourceKind(model.source_kind),
            model.source_sha256,
            model.remote_source_id,
            model.display_title,
            model.state,
        )
