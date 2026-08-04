import re
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.engine import CursorResult

from oms_hub.db import Database
from oms_hub.files.atomic import sha256_file
from oms_hub.models import (
    CourseQuizDocumentModel,
    ExamQuizTabModel,
    GenerationAttemptModel,
    GenerationJobModel,
    GoogleConnectionModel,
    LectureModel,
    NotebookMappingModel,
    NotebookSourceMappingModel,
    OutlineOutputModel,
    PublishedQuizMediaModel,
    PublishedQuizModel,
    QuizOutputModel,
    StudioQuizImageOverrideModel,
    StudioQuizImageRequirementModel,
    StudioRunModel,
    StudyPromptSettingModel,
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
    PublishedQuizMediaRecord,
    PublishedQuizRecord,
    QuizRecord,
    SourceKind,
)
from oms_hub.study_generation.native_quiz import (
    image_requirements,
    parse_native_quiz,
    serialize_native_quiz,
)
from oms_hub.study_generation.studio_domain import StudioRunStage, StudioRunState

_ACTIVE_STATES = {
    GenerationState.QUEUED.value,
    GenerationState.RUNNING.value,
    GenerationState.PAUSED.value,
}
_ANKI_PROMPT_DIRECTORY_KEY = "anki_curation_prompt_directory"


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


class GenerationRepository:
    def __init__(self, database: Database, accuracy_gate: object | None = None):
        self.database = database
        self.accuracy_gate = accuracy_gate

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
        self._validate_accuracy(quiz)
        with self.database.session() as session:
            lecture = session.get(LectureModel, lecture_id)
            if lecture is None:
                raise ValueError("lecture was removed")
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
                    version=1,
                    active=True,
                )
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
            session.flush()
            return self._published_quiz(model)

    def published_quiz(self, token: str) -> PublishedQuizRecord | None:
        with self.database.session() as session:
            model = session.get(PublishedQuizModel, token)
            return self._published_quiz(model) if model is not None and model.active else None

    def published_quizzes(self) -> tuple[PublishedQuizRecord, ...]:
        with self.database.session() as session:
            models = session.scalars(
                select(PublishedQuizModel)
                .order_by(
                    PublishedQuizModel.destination_subject_key,
                    PublishedQuizModel.destination_exam_number,
                    PublishedQuizModel.title,
                )
                .where(PublishedQuizModel.active.is_(True))
            ).all()
            return tuple(self._published_quiz(model) for model in models)

    def publish_studio_quiz(
        self,
        run_id: str,
        quiz: NativeQuiz,
    ) -> PublishedQuizRecord:
        self._validate_accuracy(quiz)
        with self.database.session() as session:
            run = session.get(StudioRunModel, run_id)
            if run is None:
                raise ValueError("Studio run was removed")
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
                    version=1,
                    active=True,
                )
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
                    version=1,
                    active=True,
                )
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
            model.active = False
            run = session.get(StudioRunModel, run_id)
            if run is not None:
                run.published_token = None
            return model.token

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
