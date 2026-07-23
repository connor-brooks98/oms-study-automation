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


class CanvasConnectionModel(Base):
    __tablename__ = "canvas_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    base_url: Mapped[str] = mapped_column(String(300), unique=True)
    state: Mapped[str] = mapped_column(String(40), default="unpaired")
    extension_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    credential_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paired_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_heartbeat: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_successful_scan: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    scan_requested_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    study_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    icloud_staging_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovery_confirmed: Mapped[bool] = mapped_column(default=False)
    auto_process: Mapped[bool] = mapped_column(default=False)
    last_scan_item_count: Mapped[int] = mapped_column(default=0)
    last_scan_new_count: Mapped[int] = mapped_column(default=0)
    course_candidates_json: Mapped[str] = mapped_column(Text, default="[]")


class CanvasCourseMappingModel(Base):
    __tablename__ = "canvas_course_mappings"
    __table_args__ = (UniqueConstraint("course_id"), UniqueConstraint("subject"))

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[str] = mapped_column(String(100))
    course_name: Mapped[str] = mapped_column(String(300))
    course_code: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(default=True)


class CanvasSourceItemModel(Base):
    __tablename__ = "canvas_source_items"
    __table_args__ = (UniqueConstraint("course_id", "file_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[str] = mapped_column(String(100))
    file_id: Mapped[str] = mapped_column(String(100))
    filename: Mapped[str] = mapped_column(String(500))
    source_url: Mapped[str] = mapped_column(Text)
    context_json: Mapped[str] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(String(40))
    lecture_id: Mapped[int | None] = mapped_column(ForeignKey("lectures.id"), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(100), nullable=True)
    exam_number: Mapped[int | None] = mapped_column(nullable=True)
    confidence: Mapped[float] = mapped_column(default=0.0)
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    review_state: Mapped[str] = mapped_column(String(30), default="none")
    discovered_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class SourceRevisionModel(Base):
    __tablename__ = "source_revisions"
    __table_args__ = (UniqueConstraint("source_item_id", "remote_signature"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_item_id: Mapped[int] = mapped_column(ForeignKey("canvas_source_items.id"))
    remote_signature: Mapped[str] = mapped_column(String(64))
    modified_at: Mapped[str] = mapped_column(String(60))
    remote_size: Mapped[int]
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_filename: Mapped[str] = mapped_column(String(500))
    stored_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(30), default="discovered")
    discovered_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class ArtifactModel(Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("revision_id", "role", "path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("source_revisions.id"))
    role: Mapped[str] = mapped_column(String(40))
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    validation_state: Mapped[str] = mapped_column(String(30), default="pending")
    promoted_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    current: Mapped[bool] = mapped_column(default=False)


class ProcessingJobModel(Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (UniqueConstraint("revision_id", "action"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("source_revisions.id"))
    action: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(30), default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class PanoptoConnectionModel(Base):
    __tablename__ = "panopto_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_url: Mapped[str] = mapped_column(String(300), unique=True)
    state: Mapped[str] = mapped_column(String(40), default="disabled")
    enabled: Mapped[bool] = mapped_column(default=False)
    acceptance_validated_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_successful_poll: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_prompt_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scan_requested_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class PanoptoRecordingModel(Base):
    __tablename__ = "panopto_recordings"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), unique=True)
    name: Mapped[str] = mapped_column(String(500))
    created_utc: Mapped[str] = mapped_column(String(40))
    duration_seconds: Mapped[float]
    folder_name: Mapped[str] = mapped_column(String(300), default="")
    content_language: Mapped[str | None] = mapped_column(String(60), nullable=True)
    lecture_id: Mapped[int | None] = mapped_column(ForeignKey("lectures.id"), nullable=True)
    confidence: Mapped[float] = mapped_column(default=0.0)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    review_state: Mapped[str] = mapped_column(String(30), default="none")
    discovered_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class TranscriptRevisionModel(Base):
    __tablename__ = "transcript_revisions"
    __table_args__ = (UniqueConstraint("recording_id", "raw_sha256"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    recording_id: Mapped[int] = mapped_column(ForeignKey("panopto_recordings.id"))
    raw_sha256: Mapped[str] = mapped_column(String(64))
    raw_path: Mapped[str] = mapped_column(Text)
    prompt_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cleaned_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cleaned_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(30), default="downloaded")
    current: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class TranscriptJobModel(Base):
    __tablename__ = "transcript_jobs"
    __table_args__ = (UniqueConstraint("revision_id", "action"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("transcript_revisions.id"))
    action: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(30), default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class OpenAIUsageModel(Base):
    __tablename__ = "openai_usage"
    __table_args__ = (UniqueConstraint("revision_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("transcript_revisions.id"))
    model: Mapped[str] = mapped_column(String(100))
    request_id: Mapped[str] = mapped_column(String(200))
    input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    cost_microusd: Mapped[int]
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
