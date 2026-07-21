from datetime import UTC, datetime

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Base(DeclarativeBase):
    pass


class LectureModel(Base):
    __tablename__ = "lectures"
    __table_args__ = (
        UniqueConstraint("subject", "exam_number", "lecture_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    subject: Mapped[str] = mapped_column(String(100))
    exam_number: Mapped[int]
    lecture_number: Mapped[int]
    topic: Mapped[str] = mapped_column(String(300))
    lecturer: Mapped[str] = mapped_column(String(300), default="")
    exam_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    scheduled_start_utc: Mapped[str | None] = mapped_column(String(40), nullable=True)
    campus: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)
    steps: Mapped[list["LectureStepModel"]] = relationship(
        back_populates="lecture",
        cascade="all, delete-orphan",
        order_by="LectureStepModel.id",
    )


class LectureStepModel(Base):
    __tablename__ = "lecture_steps"
    __table_args__ = (UniqueConstraint("lecture_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lecture_id: Mapped[int] = mapped_column(ForeignKey("lectures.id"))
    name: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(30))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)
    lecture: Mapped[LectureModel] = relationship(back_populates="steps")


class ImportIssueModel(Base):
    __tablename__ = "import_issues"

    id: Mapped[int] = mapped_column(primary_key=True)
    sheet: Mapped[str] = mapped_column(String(150))
    row_number: Mapped[int]
    message: Mapped[str] = mapped_column(Text)
    raw_values: Mapped[str] = mapped_column(Text)


class ImportRunModel(Base):
    __tablename__ = "import_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    source_name: Mapped[str] = mapped_column(String(300))
    imported_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class ExternalEventModel(Base):
    __tablename__ = "external_events"
    __table_args__ = (UniqueConstraint("provider", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(String(500))
    revision: Mapped[str | None] = mapped_column(String(300), nullable=True)
    lecture_id: Mapped[int | None] = mapped_column(
        ForeignKey("lectures.id"),
        nullable=True,
    )
    needs_review: Mapped[bool] = mapped_column(default=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text)
    seen_at: Mapped[str] = mapped_column(String(40), default=utc_now)
