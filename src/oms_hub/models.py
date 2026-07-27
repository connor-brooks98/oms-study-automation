from datetime import UTC, datetime

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Base(DeclarativeBase):
    pass


class SchemaVersionModel(Base):
    __tablename__ = "schema_version"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int]
    updated_at: Mapped[str] = mapped_column(
        String(40),
        default=utc_now,
        onupdate=utc_now,
    )


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
    scheduled_start_utc: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )
    campus: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(
        String(40),
        default=utc_now,
        onupdate=utc_now,
    )
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
    updated_at: Mapped[str] = mapped_column(
        String(40),
        default=utc_now,
        onupdate=utc_now,
    )
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


class UploadBatchModel(Base):
    __tablename__ = "upload_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(30), default="uploading")
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(
        String(40),
        default=utc_now,
        onupdate=utc_now,
    )


class UploadItemModel(Base):
    __tablename__ = "upload_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("upload_batches.id"))
    kind: Mapped[str] = mapped_column(String(20))
    original_filename: Mapped[str] = mapped_column(String(500))
    staged_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int]
    state: Mapped[str] = mapped_column(String(30), default="matching")
    lecture_id: Mapped[int | None] = mapped_column(
        ForeignKey("lectures.id"),
        nullable=True,
    )
    confidence: Mapped[float] = mapped_column(default=0.0)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    manual_assignment: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(
        String(40),
        default=utc_now,
        onupdate=utc_now,
    )


class StudyRevisionModel(Base):
    __tablename__ = "study_revisions"
    __table_args__ = (
        UniqueConstraint("upload_item_id"),
        UniqueConstraint("lecture_id", "kind", "source_sha256"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    upload_item_id: Mapped[str] = mapped_column(ForeignKey("upload_items.id"))
    lecture_id: Mapped[int] = mapped_column(ForeignKey("lectures.id"))
    kind: Mapped[str] = mapped_column(String(20))
    source_sha256: Mapped[str] = mapped_column(String(64))
    immutable_source_path: Mapped[str] = mapped_column(Text)
    derived_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    immutable_derived_path: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    canonical_source_path: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    canonical_derived_path: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    icloud_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    state: Mapped[str] = mapped_column(String(30), default="proposed")
    current: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    promoted_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class StudyUsageModel(Base):
    __tablename__ = "study_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("study_revisions.id"),
        unique=True,
    )
    provider: Mapped[str] = mapped_column(
        String(30),
        default="openai",
        server_default="openai",
    )
    model: Mapped[str] = mapped_column(String(100))
    request_id: Mapped[str] = mapped_column(String(200))
    input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    cost_microusd: Mapped[int]
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class LLMProviderSettingModel(Base):
    __tablename__ = "llm_provider_settings"

    provider: Mapped[str] = mapped_column(String(30), primary_key=True)
    model: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(default=False)
    last_test_state: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
    )
    last_tested_at: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )
    diagnostic_source: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )
    diagnostic_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    http_status: Mapped[int | None] = mapped_column(nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )
    updated_at: Mapped[str] = mapped_column(
        String(40),
        default=utc_now,
        onupdate=utc_now,
    )


class IngestionJobModel(Base):
    __tablename__ = "ingestion_jobs"
    __table_args__ = (UniqueConstraint("upload_item_id", "action"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    upload_item_id: Mapped[str] = mapped_column(ForeignKey("upload_items.id"))
    action: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(30), default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(
        String(40),
        default=utc_now,
        onupdate=utc_now,
    )


class GoogleConnectionModel(Base):
    __tablename__ = "google_connection"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    state: Mapped[str] = mapped_column(String(30), default="disconnected")
    account_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    notebook_state: Mapped[str | None] = mapped_column(String(30), nullable=True)
    gemini_state: Mapped[str | None] = mapped_column(String(30), nullable=True)
    docs_state: Mapped[str | None] = mapped_column(String(30), nullable=True)
    diagnostic: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_tested_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class StudyPromptSettingModel(Base):
    __tablename__ = "study_prompt_settings"

    kind: Mapped[str] = mapped_column(String(30), primary_key=True)
    path: Mapped[str] = mapped_column(Text, default="")
    last_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_modified_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class NotebookMappingModel(Base):
    __tablename__ = "notebook_mappings"
    __table_args__ = (UniqueConstraint("subject_key", "exam_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    subject: Mapped[str] = mapped_column(String(100))
    subject_key: Mapped[str] = mapped_column(String(100))
    exam_number: Mapped[int]
    remote_notebook_id: Mapped[str] = mapped_column(String(200), unique=True)
    title: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class NotebookSourceMappingModel(Base):
    __tablename__ = "notebook_source_mappings"
    __table_args__ = (
        UniqueConstraint("notebook_mapping_id", "study_revision_id", "source_kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    notebook_mapping_id: Mapped[int] = mapped_column(ForeignKey("notebook_mappings.id"))
    lecture_id: Mapped[int] = mapped_column(ForeignKey("lectures.id"))
    study_revision_id: Mapped[int] = mapped_column(ForeignKey("study_revisions.id"))
    source_kind: Mapped[str] = mapped_column(String(30))
    source_sha256: Mapped[str] = mapped_column(String(64))
    remote_source_id: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(30), default="ready")
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    verified_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class CourseQuizDocumentModel(Base):
    __tablename__ = "course_quiz_documents"

    subject_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    subject: Mapped[str] = mapped_column(String(100))
    document_id: Mapped[str] = mapped_column(String(200), unique=True)
    title: Mapped[str] = mapped_column(String(300))
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class ExamQuizTabModel(Base):
    __tablename__ = "exam_quiz_tabs"
    __table_args__ = (UniqueConstraint("subject_key", "exam_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    subject_key: Mapped[str] = mapped_column(
        ForeignKey("course_quiz_documents.subject_key")
    )
    exam_number: Mapped[int]
    tab_id: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class GenerationJobModel(Base):
    __tablename__ = "generation_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    lecture_id: Mapped[int] = mapped_column(ForeignKey("lectures.id"))
    kind: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(30), default="queued")
    stage: Mapped[str] = mapped_column(String(30), default="validate")
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notebook_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    pdf_source_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    transcript_source_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    notebook_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    gemini_quiz_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    quiz_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class OutlineOutputModel(Base):
    __tablename__ = "outline_outputs"

    id: Mapped[int] = mapped_column(primary_key=True)
    lecture_id: Mapped[int] = mapped_column(ForeignKey("lectures.id"))
    job_id: Mapped[str] = mapped_column(ForeignKey("generation_jobs.id"), unique=True)
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    current: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class QuizOutputModel(Base):
    __tablename__ = "quiz_outputs"

    id: Mapped[int] = mapped_column(primary_key=True)
    lecture_id: Mapped[int] = mapped_column(ForeignKey("lectures.id"))
    job_id: Mapped[str] = mapped_column(ForeignKey("generation_jobs.id"), unique=True)
    url: Mapped[str] = mapped_column(Text)
    docs_synced: Mapped[bool] = mapped_column(default=False)
    current: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
