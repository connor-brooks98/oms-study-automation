from datetime import datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult

from oms_hub.db import Database
from oms_hub.models import (
    CourseQuizDocumentModel,
    ExamQuizTabModel,
    GenerationJobModel,
    GoogleConnectionModel,
    OutlineOutputModel,
    QuizOutputModel,
    StudyPromptSettingModel,
)
from oms_hub.study_generation.domain import (
    GenerationJob,
    GenerationKind,
    GenerationStage,
    GenerationState,
    OutlineRecord,
    PromptKind,
    QuizRecord,
)

_ACTIVE_STATES = {
    GenerationState.QUEUED.value,
    GenerationState.RUNNING.value,
    GenerationState.PAUSED.value,
}


class GenerationRepository:
    def __init__(self, database: Database):
        self.database = database

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
            model = GenerationJobModel(
                id=str(uuid4()),
                lecture_id=lecture_id,
                kind=kind.value,
                state=GenerationState.QUEUED.value,
                stage=GenerationStage.VALIDATE.value,
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
                session.add(
                    StudyPromptSettingModel(kind=kind.value, path=normalized)
                )
            else:
                model.path = normalized
                model.last_sha256 = None
                model.last_modified_at = None

    def prompt_path(self, kind: PromptKind) -> str | None:
        with self.database.session() as session:
            model = session.get(StudyPromptSettingModel, kind.value)
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
                    GenerationJobModel.state == GenerationState.RUNNING.value
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
    ) -> OutlineRecord:
        with self.database.session() as session:
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
        *,
        docs_synced: bool,
    ) -> QuizRecord:
        with self.database.session() as session:
            session.execute(
                update(QuizOutputModel)
                .where(QuizOutputModel.lecture_id == lecture_id)
                .values(current=False)
            )
            model = session.scalar(
                select(QuizOutputModel).where(QuizOutputModel.job_id == job_id)
            )
            if model is None:
                model = QuizOutputModel(
                    lecture_id=lecture_id,
                    job_id=job_id,
                    url=url,
                    docs_synced=docs_synced,
                    current=True,
                )
                session.add(model)
            else:
                model.url = url
                model.docs_synced = docs_synced
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
        )

    @staticmethod
    def _quiz(model: QuizOutputModel) -> QuizRecord:
        return QuizRecord(
            model.id,
            model.lecture_id,
            model.job_id,
            model.url,
            model.docs_synced,
            model.current,
        )
