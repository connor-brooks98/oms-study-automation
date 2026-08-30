from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from oms_hub.db import Database
from oms_hub.domain import StepStatus, V2StepName
from oms_hub.models import (
    ImportIssueModel,
    ImportRunModel,
    LectureModel,
    LecturePassModel,
    LectureStepModel,
)


@dataclass(frozen=True, slots=True)
class LectureInput:
    subject: str
    exam_number: int
    lecture_number: int
    topic: str
    lecturer: str
    exam_date: str | None


def _seed_initial_passes(lecture: LectureModel) -> None:
    existing_positions = {item.position for item in lecture.passes}
    lecture.passes.extend(
        LecturePassModel(position=position)
        for position in range(1, 6)
        if position not in existing_positions
    )


class CatalogRepository:
    def __init__(self, database: Database):
        self.database = database

    def upsert_lecture(self, value: LectureInput) -> int:
        with self.database.session() as session:
            lecture = session.scalar(
                select(LectureModel).where(
                    LectureModel.subject == value.subject,
                    LectureModel.exam_number == value.exam_number,
                    LectureModel.lecture_number == value.lecture_number,
                )
            )
            if lecture is None:
                lecture = LectureModel(
                    subject=value.subject,
                    exam_number=value.exam_number,
                    lecture_number=value.lecture_number,
                    topic=value.topic,
                    lecturer=value.lecturer,
                    exam_date=value.exam_date,
                )
                lecture.steps = [
                    LectureStepModel(
                        name=name.value,
                        status=StepStatus.WAITING.value,
                    )
                    for name in V2StepName
                ]
                session.add(lecture)
            else:
                lecture.topic = value.topic
                lecture.lecturer = value.lecturer
                lecture.exam_date = value.exam_date
            _seed_initial_passes(lecture)
            session.flush()
            return lecture.id

    def list_lectures(self) -> list[LectureModel]:
        with self.database.session() as session:
            statement = (
                select(LectureModel)
                .options(
                    selectinload(LectureModel.steps),
                    selectinload(LectureModel.passes),
                )
                .order_by(
                    LectureModel.exam_date,
                    LectureModel.subject,
                    LectureModel.lecture_number,
                )
            )
            return list(session.scalars(statement).all())

    def get_lecture(self, lecture_id: int) -> LectureModel | None:
        with self.database.session() as session:
            return session.scalar(
                select(LectureModel)
                .where(LectureModel.id == lecture_id)
                .options(
                    selectinload(LectureModel.steps),
                    selectinload(LectureModel.passes),
                )
            )

    def list_exam_lectures(self, subject: str, exam_number: int) -> list[LectureModel]:
        with self.database.session() as session:
            statement = (
                select(LectureModel)
                .where(
                    LectureModel.subject == subject,
                    LectureModel.exam_number == exam_number,
                )
                .options(
                    selectinload(LectureModel.steps),
                    selectinload(LectureModel.passes),
                )
                .order_by(LectureModel.lecture_number, LectureModel.id)
            )
            return list(session.scalars(statement).all())

    def update_pass(
        self,
        lecture_id: int,
        position: int,
        *,
        completed_on: str | None,
        resource: str | None,
    ) -> LecturePassModel:
        with self.database.session() as session:
            lecture_pass = session.scalar(
                select(LecturePassModel).where(
                    LecturePassModel.lecture_id == lecture_id,
                    LecturePassModel.position == position,
                )
            )
            if lecture_pass is None:
                raise KeyError((lecture_id, position))
            lecture_pass.completed_on = completed_on
            lecture_pass.resource = resource
            session.flush()
            return lecture_pass

    def append_pass(self, lecture_id: int) -> LecturePassModel:
        with self.database.session() as session:
            lecture = session.scalar(
                select(LectureModel)
                .where(LectureModel.id == lecture_id)
                .options(selectinload(LectureModel.passes))
            )
            if lecture is None:
                raise KeyError(lecture_id)
            if not lecture.passes or any(item.completed_on is None for item in lecture.passes):
                raise ValueError("all current passes must be complete")
            lecture_pass = LecturePassModel(
                lecture_id=lecture_id,
                position=max(item.position for item in lecture.passes) + 1,
            )
            session.add(lecture_pass)
            session.flush()
            return lecture_pass

    def update_lecture(self, lecture_id: int, value: LectureInput) -> None:
        with self.database.session() as session:
            lecture = session.get(LectureModel, lecture_id)
            if lecture is None:
                raise KeyError(lecture_id)
            lecture.subject = value.subject
            lecture.exam_number = value.exam_number
            lecture.lecture_number = value.lecture_number
            lecture.topic = value.topic
            lecture.lecturer = value.lecturer
            lecture.exam_date = value.exam_date

    def set_step_status(
        self,
        lecture_id: int,
        name: V2StepName,
        status: StepStatus,
        detail: str | None = None,
    ) -> None:
        with self.database.session() as session:
            step = session.scalar(
                select(LectureStepModel).where(
                    LectureStepModel.lecture_id == lecture_id,
                    LectureStepModel.name == name.value,
                )
            )
            if step is None:
                raise KeyError((lecture_id, name.value))
            step.status = status.value
            step.detail = detail

    def replace_import_issues(
        self,
        issues: list[tuple[str, int, str, str]],
    ) -> None:
        with self.database.session() as session:
            session.execute(delete(ImportIssueModel))
            session.add_all(
                ImportIssueModel(
                    sheet=sheet,
                    row_number=row,
                    message=message,
                    raw_values=raw,
                )
                for sheet, row, message, raw in issues
            )

    def list_import_issues(self) -> list[ImportIssueModel]:
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(ImportIssueModel).order_by(
                        ImportIssueModel.sheet,
                        ImportIssueModel.row_number,
                    )
                ).all()
            )

    def resolve_import_issue(self, issue_id: int) -> None:
        with self.database.session() as session:
            issue = session.get(ImportIssueModel, issue_id)
            if issue is None:
                raise KeyError(issue_id)
            session.delete(issue)

    def has_import_hash(self, source_sha256: str) -> bool:
        with self.database.session() as session:
            return (
                session.scalar(
                    select(ImportRunModel).where(
                        ImportRunModel.source_sha256 == source_sha256
                    )
                )
                is not None
            )

    def commit_tracker_import(
        self,
        lectures: list[LectureInput],
        issues: list[tuple[str, int, str, str]],
        source_sha256: str,
        source_name: str,
    ) -> tuple[int, int]:
        with self.database.session() as session:
            if session.scalar(
                select(ImportRunModel).where(
                    ImportRunModel.source_sha256 == source_sha256
                )
            ):
                raise ValueError("tracker workbook has already been imported")

            imported = 0
            updated = 0
            for value in lectures:
                lecture = session.scalar(
                    select(LectureModel).where(
                        LectureModel.subject == value.subject,
                        LectureModel.exam_number == value.exam_number,
                        LectureModel.lecture_number == value.lecture_number,
                    )
                )
                if lecture is None:
                    lecture = LectureModel(
                        subject=value.subject,
                        exam_number=value.exam_number,
                        lecture_number=value.lecture_number,
                        topic=value.topic,
                        lecturer=value.lecturer,
                        exam_date=value.exam_date,
                    )
                    lecture.steps = [
                        LectureStepModel(
                            name=name.value,
                            status=StepStatus.WAITING.value,
                        )
                        for name in V2StepName
                    ]
                    session.add(lecture)
                    imported += 1
                else:
                    changed = (
                        lecture.topic != value.topic
                        or lecture.lecturer != value.lecturer
                        or lecture.exam_date != value.exam_date
                    )
                    lecture.topic = value.topic
                    lecture.lecturer = value.lecturer
                    lecture.exam_date = value.exam_date
                    updated += int(changed)
                _seed_initial_passes(lecture)

            session.execute(delete(ImportIssueModel))
            session.add_all(
                ImportIssueModel(
                    sheet=sheet,
                    row_number=row,
                    message=message,
                    raw_values=raw,
                )
                for sheet, row, message, raw in issues
            )
            session.add(
                ImportRunModel(
                    source_sha256=source_sha256,
                    source_name=source_name,
                )
            )
            return imported, updated
