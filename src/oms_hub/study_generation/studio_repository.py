from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import delete, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from oms_hub.db import Database
from oms_hub.models import (
    PublishedQuizMediaModel,
    PublishedQuizModel,
    StudioQuizImageOverrideModel,
    StudioQuizImageRequirementModel,
    StudioRunAttemptModel,
    StudioRunModel,
    StudioRunSourceModel,
    StudioSourceModel,
)
from oms_hub.study_generation.domain import NativeQuiz
from oms_hub.study_generation.native_quiz import (
    image_requirements,
    parse_native_quiz,
    serialize_native_quiz,
)
from oms_hub.study_generation.studio_domain import (
    StudioQuizImageRequirement,
    StudioQuizReview,
    StudioRun,
    StudioRunAttempt,
    StudioRunSource,
    StudioRunStage,
    StudioRunState,
    StudioSource,
    StudioSourceState,
    StudioSourceType,
    StudioStoredImage,
)


class StudioRepository:
    def __init__(self, database: Database):
        self.database = database

    def create_source(
        self,
        subject: str,
        exam_number: int,
        source_type: StudioSourceType,
        title: str,
        *,
        payload_path: Path | None = None,
        source_url: str | None = None,
        original_filename: str | None = None,
    ) -> StudioSource:
        with self.database.session() as session:
            model = StudioSourceModel(
                id=str(uuid4()),
                subject=subject,
                subject_key=normalize_subject(subject),
                exam_number=exam_number,
                source_type=source_type.value,
                title=title,
                payload_path=str(payload_path) if payload_path else None,
                source_url=source_url,
                original_filename=original_filename,
            )
            session.add(model)
            session.flush()
            return self._domain(model)

    def get(self, source_id: str) -> StudioSource | None:
        with self.database.session() as session:
            model = session.get(StudioSourceModel, source_id)
            return None if model is None else self._domain(model)

    def list_sources(
        self,
        subject_key: str | None = None,
        exam_number: int | None = None,
    ) -> list[StudioSource]:
        with self.database.session() as session:
            statement = (
                select(StudioSourceModel)
                .where(StudioSourceModel.state != StudioSourceState.DELETED.value)
                .order_by(StudioSourceModel.created_at)
            )
            if subject_key is not None:
                statement = statement.where(
                    StudioSourceModel.subject_key == normalize_subject(subject_key)
                )
            if exam_number is not None:
                statement = statement.where(StudioSourceModel.exam_number == exam_number)
            return [self._domain(item) for item in session.scalars(statement).all()]

    def set_payload_path(self, source_id: str, payload_path: Path) -> StudioSource:
        with self.database.session() as session:
            model = session.get(StudioSourceModel, source_id)
            if model is None:
                raise KeyError(source_id)
            model.payload_path = str(payload_path)
            session.flush()
            return self._domain(model)

    def claim_next(self, now: datetime | None = None) -> StudioSource | None:
        now = now or datetime.now(UTC)
        with self.database.session() as session:
            model = session.scalar(
                select(StudioSourceModel)
                .where(
                    StudioSourceModel.state == StudioSourceState.PENDING.value,
                    or_(
                        StudioSourceModel.next_attempt_at.is_(None),
                        StudioSourceModel.next_attempt_at <= now.isoformat(),
                    ),
                )
                .order_by(StudioSourceModel.created_at, StudioSourceModel.id)
                .limit(1)
            )
            if model is None:
                return None
            result = session.execute(
                update(StudioSourceModel)
                .where(
                    StudioSourceModel.id == model.id,
                    StudioSourceModel.state == StudioSourceState.PENDING.value,
                )
                .values(
                    state=StudioSourceState.ATTACHING.value,
                    attempts=StudioSourceModel.attempts + 1,
                    error=None,
                    next_attempt_at=None,
                )
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                return None
            session.flush()
            session.refresh(model)
            return self._domain(model)

    def complete(
        self,
        source_id: str,
        notebook_id: str,
        remote_source_id: str,
        *,
        converted: bool = False,
        payload_path: Path | None = None,
    ) -> None:
        with self.database.session() as session:
            model = session.get(StudioSourceModel, source_id)
            if model is None:
                raise KeyError(source_id)
            model.state = StudioSourceState.ATTACHED.value
            model.next_attempt_at = None
            model.remote_notebook_id = notebook_id
            model.remote_source_id = remote_source_id
            model.converted_from_pptx = converted
            if payload_path is not None:
                model.payload_path = str(payload_path)

    def fail(
        self,
        source_id: str,
        source: str,
        error: str,
        *,
        retry: bool,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(UTC)
        with self.database.session() as session:
            model = session.get(StudioSourceModel, source_id)
            if model is None:
                raise KeyError(source_id)
            model.state = (
                StudioSourceState.PENDING.value
                if retry and model.attempts < 3
                else StudioSourceState.FAILED.value
            )
            model.diagnostic_source = source
            model.error = error[:1000]
            model.next_attempt_at = (
                (now + timedelta(seconds=min(30, 5 * (2 ** (model.attempts - 1))))).isoformat()
                if model.state == StudioSourceState.PENDING.value
                else None
            )

    def recover_interrupted_jobs(self) -> int:
        with self.database.session() as session:
            source_models = session.scalars(
                select(StudioSourceModel).where(
                    StudioSourceModel.state == StudioSourceState.ATTACHING.value
                )
            ).all()
            for source_model in source_models:
                source_model.state = StudioSourceState.PENDING.value
            run_models = session.scalars(
                select(StudioRunModel).where(StudioRunModel.state == StudioRunState.RUNNING.value)
            ).all()
            for run_model in run_models:
                run_model.state = StudioRunState.QUEUED.value
                run_model.error = "requeued after an interrupted Hub process"
                run_model.next_attempt_at = None
            return len(source_models) + len(run_models)

    def queue_run(
        self,
        subject: str,
        exam_number: int,
        prompt: str,
        source_ids: list[str],
        label: str,
        destination_subject: str,
        destination_exam_number: int,
        *,
        supersedes_run_id: str | None = None,
    ) -> StudioRun:
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("selected Studio sources contain duplicates")
        subject_key = normalize_subject(subject)
        destination_key = normalize_subject(destination_subject)
        label_key = normalize_subject(label)
        with self.database.session() as session:
            sources = list(
                session.scalars(
                    select(StudioSourceModel).where(StudioSourceModel.id.in_(source_ids))
                ).all()
            )
            by_id = {source.id: source for source in sources}
            if len(by_id) != len(source_ids):
                raise ValueError("a selected Studio source no longer exists")
            ordered = [by_id[source_id] for source_id in source_ids]
            if any(
                source.subject_key != subject_key or source.exam_number != exam_number
                for source in ordered
            ):
                raise ValueError("selected Studio sources belong to another course or exam")
            if any(
                source.state != StudioSourceState.ATTACHED.value or not source.remote_source_id
                for source in ordered
            ):
                raise ValueError("all selected Studio sources must be attached")
            if (
                supersedes_run_id is not None
                and session.get(StudioRunModel, supersedes_run_id) is None
            ):
                raise ValueError("the Studio run being replaced no longer exists")
            if supersedes_run_id is None:
                active_run = session.scalar(
                    select(StudioRunModel).where(
                        StudioRunModel.destination_subject_key == destination_key,
                        StudioRunModel.destination_exam_number == destination_exam_number,
                        StudioRunModel.label_key == label_key,
                        StudioRunModel.state.in_(
                            {
                                StudioRunState.QUEUED.value,
                                StudioRunState.RUNNING.value,
                                StudioRunState.RETRYING.value,
                            }
                        ),
                    )
                )
                published = session.scalar(
                    select(PublishedQuizModel).where(
                        PublishedQuizModel.studio_run_id.is_not(None),
                        PublishedQuizModel.destination_subject_key == destination_key,
                        PublishedQuizModel.destination_exam_number == destination_exam_number,
                        PublishedQuizModel.label_key == label_key,
                        PublishedQuizModel.active.is_(True),
                    )
                )
                if active_run is not None or published is not None:
                    raise ValueError("this quiz label is already in use for the destination exam")
            model = StudioRunModel(
                id=str(uuid4()),
                subject=subject,
                subject_key=subject_key,
                exam_number=exam_number,
                destination_subject=destination_subject,
                destination_subject_key=destination_key,
                destination_exam_number=destination_exam_number,
                label=label,
                label_key=label_key,
                prompt=prompt,
                state=StudioRunState.QUEUED.value,
                stage=StudioRunStage.VALIDATE.value,
                supersedes_run_id=supersedes_run_id,
            )
            session.add(model)
            try:
                session.flush()
            except IntegrityError as error:
                raise ValueError(
                    "this quiz label is already in use for the destination exam"
                ) from error
            for position, source in enumerate(ordered):
                assert source.remote_source_id is not None
                session.add(
                    StudioRunSourceModel(
                        run_id=model.id,
                        source_id=source.id,
                        remote_source_id=source.remote_source_id,
                        source_title=source.title,
                        position=position,
                    )
                )
            session.flush()
            return self._run_domain(session, model)

    def get_run(self, run_id: str) -> StudioRun:
        with self.database.session() as session:
            model = session.get(StudioRunModel, run_id)
            if model is None:
                raise KeyError(run_id)
            return self._run_domain(session, model)

    def list_runs(
        self,
        subject_key: str | None = None,
        exam_number: int | None = None,
        limit: int = 50,
    ) -> list[StudioRun]:
        with self.database.session() as session:
            statement = select(StudioRunModel).order_by(
                StudioRunModel.created_at.desc(), StudioRunModel.id.desc()
            )
            if subject_key is not None:
                statement = statement.where(
                    StudioRunModel.subject_key == normalize_subject(subject_key)
                )
            if exam_number is not None:
                statement = statement.where(StudioRunModel.exam_number == exam_number)
            statement = statement.limit(limit)
            models = session.scalars(statement).all()
            run_ids = [model.id for model in models]
            sources_by_run: dict[str, list[StudioRunSourceModel]] = {}
            if run_ids:
                snapshots = session.scalars(
                    select(StudioRunSourceModel)
                    .where(StudioRunSourceModel.run_id.in_(run_ids))
                    .order_by(StudioRunSourceModel.run_id, StudioRunSourceModel.position)
                ).all()
                for snapshot in snapshots:
                    sources_by_run.setdefault(snapshot.run_id, []).append(snapshot)
            return [
                self._run_domain(session, model, sources_by_run.get(model.id, ()))
                for model in models
            ]

    def claim_next_run(self, now: datetime | None = None) -> StudioRun | None:
        now = now or datetime.now(UTC)
        with self.database.session() as session:
            model = session.scalar(
                select(StudioRunModel)
                .where(
                    StudioRunModel.state.in_(
                        {
                            StudioRunState.QUEUED.value,
                            StudioRunState.RETRYING.value,
                        }
                    ),
                    or_(
                        StudioRunModel.next_attempt_at.is_(None),
                        StudioRunModel.next_attempt_at <= now.isoformat(),
                    ),
                )
                .order_by(StudioRunModel.created_at, StudioRunModel.id)
                .limit(1)
            )
            if model is None:
                return None
            claimed = session.execute(
                update(StudioRunModel)
                .where(
                    StudioRunModel.id == model.id,
                    StudioRunModel.state.in_(
                        {
                            StudioRunState.QUEUED.value,
                            StudioRunState.RETRYING.value,
                        }
                    ),
                )
                .values(
                    state=StudioRunState.RUNNING.value,
                    stage=StudioRunStage.NOTEBOOK.value,
                    attempts=StudioRunModel.attempts + 1,
                    error=None,
                    next_attempt_at=None,
                )
            )
            if cast(CursorResult[Any], claimed).rowcount != 1:
                return None
            session.flush()
            session.refresh(model)
            return self._run_domain(session, model)

    def set_run_stage(self, run_id: str, stage: StudioRunStage) -> None:
        with self.database.session() as session:
            model = session.get(StudioRunModel, run_id)
            if model is None:
                raise KeyError(run_id)
            model.stage = stage.value

    def list_run_attempts(self, run_id: str) -> tuple[StudioRunAttempt, ...]:
        with self.database.session() as session:
            models = session.scalars(
                select(StudioRunAttemptModel)
                .where(StudioRunAttemptModel.run_id == run_id)
                .order_by(StudioRunAttemptModel.attempt_number)
            ).all()
            return tuple(
                StudioRunAttempt(
                    model.attempt_number,
                    model.diagnostic_source,
                    model.raw_response,
                    model.error,
                    model.created_at,
                )
                for model in models
            )

    def complete_run(self, run_id: str, notebook_id: str, raw_response: str) -> StudioRun:
        with self.database.session() as session:
            model = session.get(StudioRunModel, run_id)
            if model is None:
                raise KeyError(run_id)
            model.state = StudioRunState.COMPLETE.value
            model.stage = StudioRunStage.COMPLETE.value
            model.notebook_id = notebook_id
            model.raw_response = raw_response
            model.error = None
            model.next_attempt_at = None
            session.flush()
            return self._run_domain(session, model)

    def complete_published_run(
        self,
        run_id: str,
        notebook_id: str,
        raw_response: str,
        published_token: str,
    ) -> StudioRun:
        with self.database.session() as session:
            model = session.get(StudioRunModel, run_id)
            if model is None:
                raise KeyError(run_id)
            model.state = StudioRunState.COMPLETE.value
            model.stage = StudioRunStage.COMPLETE.value
            model.notebook_id = notebook_id
            model.raw_response = raw_response
            model.published_token = published_token
            model.error = None
            model.next_attempt_at = None
            session.flush()
            return self._run_domain(session, model)

    def save_run_response(self, run_id: str, raw_response: str) -> None:
        with self.database.session() as session:
            model = session.get(StudioRunModel, run_id)
            if model is None:
                raise KeyError(run_id)
            model.raw_response = raw_response

    def await_image_review(
        self,
        run_id: str,
        notebook_id: str,
        raw_response: str,
        quiz: NativeQuiz,
    ) -> StudioRun:
        requirements = image_requirements(quiz)
        if not requirements:
            raise ValueError("quiz does not require image review")
        with self.database.session() as session:
            model = session.get(StudioRunModel, run_id)
            if model is None:
                raise KeyError(run_id)
            session.execute(
                delete(StudioQuizImageOverrideModel).where(
                    StudioQuizImageOverrideModel.run_id == run_id
                )
            )
            stale_asset_paths = {
                path
                for path in session.scalars(
                    select(StudioQuizImageRequirementModel.asset_path).where(
                        StudioQuizImageRequirementModel.run_id == run_id,
                        StudioQuizImageRequirementModel.asset_path.is_not(None),
                    )
                ).all()
                if path is not None
            }
            session.execute(
                delete(StudioQuizImageRequirementModel).where(
                    StudioQuizImageRequirementModel.run_id == run_id
                )
            )
            for requirement in requirements:
                session.add(
                    StudioQuizImageRequirementModel(
                        run_id=run_id,
                        image_key=requirement.key,
                        source_title=requirement.source_title,
                        locator=requirement.locator,
                        description=requirement.description,
                    )
                )
            model.state = StudioRunState.AWAITING_IMAGES.value
            model.stage = StudioRunStage.IMAGE_REVIEW.value
            model.notebook_id = notebook_id
            model.raw_response = raw_response
            model.draft_payload_json = serialize_native_quiz(quiz)
            model.error = None
            model.next_attempt_at = None
            session.flush()
            result = self._run_domain(session, model)
        self._unlink_orphaned_assets(stale_asset_paths)
        return result

    def _unlink_orphaned_assets(self, asset_paths: set[str]) -> None:
        if not asset_paths:
            return
        with self.database.session() as session:
            referenced = set(
                session.scalars(
                    select(PublishedQuizMediaModel.path).where(
                        PublishedQuizMediaModel.path.in_(asset_paths)
                    )
                ).all()
            )
        for asset_path in asset_paths - referenced:
            Path(asset_path).unlink(missing_ok=True)

    def quiz_review(self, run_id: str) -> StudioQuizReview:
        with self.database.session() as session:
            model = session.get(StudioRunModel, run_id)
            if model is None:
                raise KeyError(run_id)
            return self._review_domain(session, model)

    def bind_image(
        self,
        run_id: str,
        image_key: str,
        image: StudioStoredImage,
    ) -> StudioQuizReview:
        with self.database.session() as session:
            run = session.get(StudioRunModel, run_id)
            if run is None:
                raise KeyError(run_id)
            if run.state != StudioRunState.AWAITING_IMAGES.value:
                raise ValueError("Studio run is not awaiting images")
            model = session.scalar(
                select(StudioQuizImageRequirementModel).where(
                    StudioQuizImageRequirementModel.run_id == run_id,
                    StudioQuizImageRequirementModel.image_key == image_key,
                )
            )
            if model is None:
                raise KeyError(image_key)
            model.asset_path = str(image.path)
            model.asset_sha256 = image.sha256
            model.media_type = image.media_type
            model.width = image.width
            model.height = image.height
            model.original_filename = image.original_filename
            session.flush()
            return self._review_domain(session, run)

    def set_image_override(
        self,
        run_id: str,
        question_id: str,
        enabled: bool,
    ) -> StudioQuizReview:
        with self.database.session() as session:
            run = session.get(StudioRunModel, run_id)
            if run is None:
                raise KeyError(run_id)
            if run.state != StudioRunState.AWAITING_IMAGES.value:
                raise ValueError("Studio run is not awaiting images")
            if not run.draft_payload_json:
                raise ValueError("Studio quiz draft is missing")
            quiz = parse_native_quiz(run.draft_payload_json)
            question = next(
                (item for item in quiz.questions if item.id == question_id),
                None,
            )
            if question is None or question.image_ref is None:
                raise ValueError("question does not have an image requirement")
            existing = session.scalar(
                select(StudioQuizImageOverrideModel).where(
                    StudioQuizImageOverrideModel.run_id == run_id,
                    StudioQuizImageOverrideModel.question_id == question_id,
                )
            )
            if enabled and existing is None:
                session.add(
                    StudioQuizImageOverrideModel(
                        run_id=run_id,
                        question_id=question_id,
                        image_key=question.image_ref.key,
                    )
                )
            elif not enabled and existing is not None:
                session.delete(existing)
            session.flush()
            return self._review_domain(session, run)

    def resolved_quiz(self, run_id: str) -> NativeQuiz:
        review = self.quiz_review(run_id)
        if review.unresolved_keys:
            raise ValueError(
                "quiz images are still required: " + ", ".join(review.unresolved_keys)
            )
        return replace(
            review.quiz,
            questions=tuple(
                replace(question, image_ref=None)
                if question.id in review.overridden_question_ids
                else question
                for question in review.quiz.questions
            ),
        )

    def mark_run_attempt_error(
        self,
        run_id: str,
        attempt_number: int,
        diagnostic_source: str,
        error: str,
    ) -> None:
        with self.database.session() as session:
            model = session.scalar(
                select(StudioRunAttemptModel).where(
                    StudioRunAttemptModel.run_id == run_id,
                    StudioRunAttemptModel.attempt_number == attempt_number,
                )
            )
            if model is None:
                raise KeyError((run_id, attempt_number))
            model.diagnostic_source = diagnostic_source
            model.error = error[:1000]

    def contract_failure_count(self, run_id: str) -> int:
        with self.database.session() as session:
            return len(
                session.scalars(
                    select(StudioRunAttemptModel.id).where(
                        StudioRunAttemptModel.run_id == run_id,
                        StudioRunAttemptModel.diagnostic_source == "contract",
                    )
                ).all()
            )

    def rerun(self, run_id: str) -> StudioRun:
        previous = self.get_run(run_id)
        return self.queue_run(
            previous.subject,
            previous.exam_number,
            previous.prompt,
            [source.source_id for source in previous.sources],
            previous.label,
            previous.destination_subject,
            previous.destination_exam_number,
            supersedes_run_id=previous.id,
        )

    def mark_source_deleted(self, source_id: str) -> StudioSource:
        with self.database.session() as session:
            model = session.get(StudioSourceModel, source_id)
            if model is None:
                raise KeyError(source_id)
            model.state = StudioSourceState.DELETED.value
            session.flush()
            return self._domain(model)

    def retry_run(
        self,
        run_id: str,
        diagnostic_source: str,
        error: str,
        delay: timedelta,
    ) -> StudioRun:
        with self.database.session() as session:
            model = session.get(StudioRunModel, run_id)
            if model is None:
                raise KeyError(run_id)
            model.state = StudioRunState.RETRYING.value
            model.stage = StudioRunStage.CHAT.value
            model.diagnostic_source = diagnostic_source
            model.error = error[:1000]
            model.next_attempt_at = (datetime.now(UTC) + delay).isoformat()
            session.flush()
            return self._run_domain(session, model)

    def fail_run(
        self,
        run_id: str,
        diagnostic_source: str,
        error: str,
    ) -> StudioRun:
        with self.database.session() as session:
            model = session.get(StudioRunModel, run_id)
            if model is None:
                raise KeyError(run_id)
            model.state = StudioRunState.FAILED.value
            model.diagnostic_source = diagnostic_source
            model.error = error[:1000]
            model.next_attempt_at = None
            session.flush()
            return self._run_domain(session, model)

    def record_run_attempt(
        self,
        run_id: str,
        attempt_number: int,
        diagnostic_source: str,
        raw_response: str | None,
        error: str | None,
    ) -> None:
        with self.database.session() as session:
            session.add(
                StudioRunAttemptModel(
                    run_id=run_id,
                    attempt_number=attempt_number,
                    diagnostic_source=diagnostic_source,
                    raw_response=raw_response,
                    error=error[:1000] if error else None,
                )
            )

    @staticmethod
    def _domain(model: StudioSourceModel) -> StudioSource:
        return StudioSource(
            model.id,
            model.subject,
            model.subject_key,
            model.exam_number,
            StudioSourceType(model.source_type),
            model.title,
            model.original_filename,
            Path(model.payload_path) if model.payload_path else None,
            model.source_url,
            StudioSourceState(model.state),
            model.attempts,
            model.next_attempt_at,
            model.diagnostic_source,
            model.error,
            model.remote_notebook_id,
            model.remote_source_id,
            model.converted_from_pptx,
        )

    @staticmethod
    def _run_domain(
        session: Session,
        model: StudioRunModel,
        sources: Sequence[StudioRunSourceModel] | None = None,
    ) -> StudioRun:
        snapshots = (
            sources
            if sources is not None
            else session.scalars(
                select(StudioRunSourceModel)
                .where(StudioRunSourceModel.run_id == model.id)
                .order_by(StudioRunSourceModel.position)
            ).all()
        )
        return StudioRun(
            model.id,
            model.subject,
            model.subject_key,
            model.exam_number,
            model.destination_subject,
            model.destination_subject_key,
            model.destination_exam_number,
            model.label,
            model.prompt,
            StudioRunState(model.state),
            StudioRunStage(model.stage),
            model.attempts,
            model.next_attempt_at,
            model.diagnostic_source,
            model.error,
            model.notebook_id,
            model.raw_response,
            model.draft_payload_json,
            model.published_token,
            model.supersedes_run_id,
            tuple(
                StudioRunSource(
                    snapshot.source_id,
                    snapshot.remote_source_id,
                    snapshot.source_title,
                )
                for snapshot in snapshots
            ),
        )

    @classmethod
    def _review_domain(
        cls,
        session: Session,
        model: StudioRunModel,
    ) -> StudioQuizReview:
        if not model.draft_payload_json:
            raise ValueError("Studio quiz draft is missing")
        quiz = parse_native_quiz(model.draft_payload_json)
        requirement_models = session.scalars(
            select(StudioQuizImageRequirementModel)
            .where(StudioQuizImageRequirementModel.run_id == model.id)
            .order_by(StudioQuizImageRequirementModel.id)
        ).all()
        overridden = frozenset(
            session.scalars(
                select(StudioQuizImageOverrideModel.question_id).where(
                    StudioQuizImageOverrideModel.run_id == model.id
                )
            ).all()
        )
        question_ids_by_key: dict[str, list[str]] = {}
        for question in quiz.questions:
            if question.image_ref is not None:
                question_ids_by_key.setdefault(question.image_ref.key, []).append(question.id)
        requirements = tuple(
            StudioQuizImageRequirement(
                requirement.image_key,
                requirement.source_title,
                requirement.locator,
                requirement.description,
                tuple(question_ids_by_key.get(requirement.image_key, ())),
                (
                    StudioStoredImage(
                        Path(requirement.asset_path),
                        requirement.asset_sha256,
                        requirement.media_type,
                        requirement.width,
                        requirement.height,
                        requirement.original_filename,
                    )
                    if requirement.asset_path is not None
                    and requirement.asset_sha256 is not None
                    and requirement.media_type is not None
                    and requirement.width is not None
                    and requirement.height is not None
                    and requirement.original_filename is not None
                    else None
                ),
            )
            for requirement in requirement_models
        )
        return StudioQuizReview(
            cls._run_domain(session, model),
            quiz,
            requirements,
            overridden,
        )


def normalize_subject(subject: str) -> str:
    return " ".join(subject.casefold().split())
