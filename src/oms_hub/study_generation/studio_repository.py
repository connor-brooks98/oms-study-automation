from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult

from oms_hub.db import Database
from oms_hub.models import StudioSourceModel
from oms_hub.study_generation.studio_domain import (
    StudioSource,
    StudioSourceState,
    StudioSourceType,
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
            statement = select(StudioSourceModel).order_by(StudioSourceModel.created_at)
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
            models = session.scalars(
                select(StudioSourceModel).where(
                    StudioSourceModel.state == StudioSourceState.ATTACHING.value
                )
            ).all()
            for model in models:
                model.state = StudioSourceState.PENDING.value
            return len(models)

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


def normalize_subject(subject: str) -> str:
    return " ".join(subject.casefold().split())
