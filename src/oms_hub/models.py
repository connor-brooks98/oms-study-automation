from datetime import UTC, datetime

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint, text
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


class RuntimeSettingModel(Base):
    """Allowlisted, staged runtime overrides; never a generic .env mirror."""

    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(String(500))
    revision: Mapped[int] = mapped_column(default=1)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class RuntimeSettingAuditModel(Base):
    """Append-only operator record for the small remotely writable allowlist."""

    __tablename__ = "runtime_setting_audit"
    __table_args__ = (Index("ix_runtime_setting_audit_key_revision", "key", "revision"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(30))
    previous_value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    revision: Mapped[int] = mapped_column()
    actor: Mapped[str] = mapped_column(String(320))
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


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
        Index(
            "uq_study_revisions_transcript_cleaning_lecture",
            "lecture_id",
            unique=True,
            sqlite_where=text("kind='transcripts' AND state='cleaning'"),
        ),
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


class LLMTaskAssignmentModel(Base):
    __tablename__ = "llm_task_assignments"

    task: Mapped[str] = mapped_column(String(30), primary_key=True)
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[str] = mapped_column(
        String(40),
        default=utc_now,
        onupdate=utc_now,
    )


class StudyAISettingModel(Base):
    __tablename__ = "study_ai_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    openrouter_model: Mapped[str] = mapped_column(
        String(200),
        default="openai/gpt-4o-mini",
    )
    accuracy_gate_enabled: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


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
    display_title: Mapped[str] = mapped_column(String(500), default="")
    state: Mapped[str] = mapped_column(String(30), default="ready")
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    verified_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class StudioSourceModel(Base):
    __tablename__ = "studio_sources"
    __table_args__ = (
        Index("ix_studio_sources_scope_state", "subject_key", "exam_number", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    subject: Mapped[str] = mapped_column(String(100))
    subject_key: Mapped[str] = mapped_column(String(100))
    exam_number: Mapped[int]
    source_type: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(500))
    original_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    payload_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    purpose: Mapped[str] = mapped_column(String(30), default="notebook")
    import_role: Mapped[str | None] = mapped_column(String(40), nullable=True)
    import_attach_to_notebook: Mapped[bool] = mapped_column(default=False)
    snapshot_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(30), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    diagnostic_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_notebook_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    remote_source_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    converted_from_pptx: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class StudioSourceOperationModel(Base):
    """Durable command journal for externally-visible Studio source mutations."""

    __tablename__ = "studio_source_operations"
    __table_args__ = (
        Index("ix_studio_source_operations_poll", "state", "created_at"),
        Index("ix_studio_source_operations_source", "source_id", "created_at"),
        Index(
            "ix_studio_source_operations_notebook_active",
            "notebook_id",
            unique=True,
            sqlite_where=text(
                "notebook_id IS NOT NULL AND state IN "
                "('queued', 'executing', 'reconciling', 'deleting')"
            ),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("studio_sources.id"))
    operation_kind: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(30), default="queued")
    notebook_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    remote_source_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    baseline_remote_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    attempts: Mapped[int] = mapped_column(default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    diagnostic_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class StudioRunModel(Base):
    __tablename__ = "studio_runs"
    __table_args__ = (
        Index("ix_studio_runs_poll", "state", "next_attempt_at", "created_at"),
        Index("ix_studio_runs_scope", "subject_key", "exam_number", "created_at"),
        Index("ix_studio_runs_supersedes", "supersedes_run_id"),
        Index(
            "ix_studio_runs_active_label",
            "destination_subject_key",
            "destination_exam_number",
            "label_key",
            unique=True,
            sqlite_where=text("state IN ('queued', 'running', 'retrying')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    subject: Mapped[str] = mapped_column(String(100))
    subject_key: Mapped[str] = mapped_column(String(100))
    exam_number: Mapped[int]
    destination_subject: Mapped[str] = mapped_column(String(100))
    destination_subject_key: Mapped[str] = mapped_column(String(100))
    destination_exam_number: Mapped[int]
    label: Mapped[str] = mapped_column(String(300))
    label_key: Mapped[str] = mapped_column(String(300), default="")
    prompt: Mapped[str] = mapped_column(Text)
    workflow_kind: Mapped[str] = mapped_column(
        String(30), default="notebook_generation"
    )
    content_kind: Mapped[str] = mapped_column(String(30), default="exam_review")
    state: Mapped[str] = mapped_column(String(30), default="queued")
    stage: Mapped[str] = mapped_column(String(30), default="validate")
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    diagnostic_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    notebook_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    history_hidden_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    supersedes_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("studio_runs.id"),
        nullable=True,
    )
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class StudioRunSourceModel(Base):
    __tablename__ = "studio_run_sources"
    __table_args__ = (
        UniqueConstraint("run_id", "source_id"),
        Index("ix_studio_run_sources_run", "run_id", "position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("studio_runs.id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("studio_sources.id"))
    remote_source_id: Mapped[str] = mapped_column(String(200))
    source_title: Mapped[str] = mapped_column(String(500))
    position: Mapped[int]


class StudioImportRunSourceModel(Base):
    __tablename__ = "studio_import_run_sources"
    __table_args__ = (
        UniqueConstraint("run_id", "source_id"),
        UniqueConstraint("run_id", "position"),
        Index("ix_studio_import_run_sources_run", "run_id", "position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("studio_runs.id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("studio_sources.id"))
    source_role: Mapped[str] = mapped_column(String(40))
    attach_to_notebook: Mapped[bool] = mapped_column(default=False)
    remote_notebook_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    remote_source_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    position: Mapped[int]


class StudioRunArtifactModel(Base):
    __tablename__ = "studio_run_artifacts"
    __table_args__ = (
        UniqueConstraint("run_id", "artifact_key"),
        Index("ix_studio_run_artifacts_run", "run_id", "artifact_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("studio_runs.id"))
    artifact_key: Mapped[str] = mapped_column(String(500))
    signature_sha256: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(30), nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class StudioQuestionReviewModel(Base):
    __tablename__ = "studio_question_reviews"
    __table_args__ = (
        UniqueConstraint("run_id", "question_id"),
        Index("ix_studio_question_reviews_run", "run_id", "question_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("studio_runs.id"))
    question_id: Mapped[str] = mapped_column(String(200))
    answer_provenance: Mapped[str | None] = mapped_column(String(30), nullable=True)
    verification_required: Mapped[bool] = mapped_column(default=False)
    verified_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    extraction_confidence: Mapped[float] = mapped_column()
    diagnostics_json: Mapped[str] = mapped_column(Text, default="[]")
    original_identifier: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class StudioRunAttemptModel(Base):
    __tablename__ = "studio_run_attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "attempt_number"),
        Index("ix_studio_run_attempts_run", "run_id", "attempt_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("studio_runs.id"))
    attempt_number: Mapped[int]
    diagnostic_source: Mapped[str] = mapped_column(String(40))
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class StudioQuizImageRequirementModel(Base):
    __tablename__ = "studio_quiz_image_requirements"
    __table_args__ = (
        UniqueConstraint("run_id", "image_key"),
        Index("ix_studio_quiz_image_requirements_run", "run_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("studio_runs.id"))
    image_key: Mapped[str] = mapped_column(String(64))
    source_title: Mapped[str] = mapped_column(String(500))
    locator: Mapped[str] = mapped_column(String(1000))
    description: Mapped[str] = mapped_column(String(1000))
    asset_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    asset_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    width: Mapped[int | None] = mapped_column(nullable=True)
    height: Mapped[int | None] = mapped_column(nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class StudioQuizImageOverrideModel(Base):
    __tablename__ = "studio_quiz_image_overrides"
    __table_args__ = (
        UniqueConstraint("run_id", "question_id"),
        Index("ix_studio_quiz_image_overrides_run", "run_id", "question_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("studio_runs.id"))
    question_id: Mapped[str] = mapped_column(String(4))
    image_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


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
    __table_args__ = (
        Index("ix_generation_jobs_poll", "state", "next_attempt_at", "created_at"),
        Index("ix_generation_jobs_supersedes", "supersedes_job_id"),
    )

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
    pdf_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("study_revisions.id"),
        nullable=True,
    )
    transcript_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("study_revisions.id"),
        nullable=True,
    )
    notebook_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    pdf_source_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    transcript_source_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    notebook_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    gemini_quiz_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    supersedes_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_jobs.id"),
        nullable=True,
    )
    quiz_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class GenerationAttemptModel(Base):
    __tablename__ = "generation_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number"),
        Index("ix_generation_attempts_job", "job_id", "attempt_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("generation_jobs.id"))
    attempt_number: Mapped[int]
    diagnostic_source: Mapped[str] = mapped_column(String(40))
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


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


class PublishedQuizModel(Base):
    __tablename__ = "published_quizzes"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    lecture_id: Mapped[int | None] = mapped_column(
        ForeignKey("lectures.id"),
        nullable=True,
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_jobs.id"),
        nullable=True,
    )
    studio_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("studio_runs.id"),
        nullable=True,
    )
    destination_subject: Mapped[str] = mapped_column(String(100), default="")
    destination_subject_key: Mapped[str] = mapped_column(String(100), default="")
    destination_exam_number: Mapped[int] = mapped_column(default=0)
    label: Mapped[str] = mapped_column(String(300), default="")
    label_key: Mapped[str] = mapped_column(String(300), default="")
    title: Mapped[str] = mapped_column(String(300))
    payload_json: Mapped[str] = mapped_column(Text)
    content_kind: Mapped[str] = mapped_column(String(30), default="lecture_quiz")
    display_order: Mapped[int] = mapped_column(default=0)
    version: Mapped[int] = mapped_column(default=1)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(
        String(40),
        default=utc_now,
        onupdate=utc_now,
    )


class PublishedQuizMediaModel(Base):
    __tablename__ = "published_quiz_media"
    __table_args__ = (
        UniqueConstraint("quiz_token", "image_key"),
        Index("ix_published_quiz_media_token", "quiz_token", "image_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    quiz_token: Mapped[str] = mapped_column(ForeignKey("published_quizzes.token"))
    image_key: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(50))
    width: Mapped[int]
    height: Mapped[int]
    alt_text: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
