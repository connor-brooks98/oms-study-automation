from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from oms_hub.models import Base, utc_now

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class AnkiCurationInstructionModel(Base):
    __tablename__ = "anki_curation_instructions"

    lecture_id: Mapped[int] = mapped_column(
        ForeignKey("lectures.id"),
        primary_key=True,
    )
    text: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(default=1)
    sha256: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[str] = mapped_column(
        String(40),
        default=utc_now,
        onupdate=utc_now,
    )


class AnkiCurationJobModel(Base):
    __tablename__ = "anki_curation_jobs"
    __table_args__ = (
        Index("ix_anki_curation_jobs_state_created", "state", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    lecture_id: Mapped[int] = mapped_column(ForeignKey("lectures.id"))
    state: Mapped[str] = mapped_column(String(30), default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    target_deck: Mapped[str] = mapped_column(Text)
    target_tag: Mapped[str] = mapped_column(Text)
    index_snapshot_id: Mapped[str] = mapped_column(String(200))
    _legacy_amboss_input: Mapped[str] = mapped_column(
        "amboss_input",
        Text,
        default="",
        server_default="",
    )
    _legacy_amboss_sha256: Mapped[str] = mapped_column(
        "amboss_sha256",
        String(64),
        default=EMPTY_SHA256,
        server_default=EMPTY_SHA256,
    )
    block_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_revision_ids_json: Mapped[str] = mapped_column(
        Text,
        default="[]",
        server_default="[]",
    )
    deck_allowlist_json: Mapped[str] = mapped_column(
        Text,
        default="[]",
        server_default="[]",
    )
    tag_allowlist_json: Mapped[str] = mapped_column(
        Text,
        default="[]",
        server_default="[]",
    )
    provider: Mapped[str] = mapped_column(
        String(30),
        default="anthropic",
        server_default="anthropic",
    )
    model: Mapped[str] = mapped_column(
        String(200),
        default="claude-sonnet-5",
        server_default="claude-sonnet-5",
    )
    semantic_generation: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )
    companion_generation: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )
    source_index_generation: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )
    configuration_sha256: Mapped[str] = mapped_column(
        String(64),
        default=EMPTY_SHA256,
        server_default=EMPTY_SHA256,
    )
    apply_state: Mapped[str] = mapped_column(
        String(50),
        default="pending",
        server_default="pending",
    )
    instruction_text: Mapped[str] = mapped_column(Text, default="")
    instruction_sha256: Mapped[str] = mapped_column(String(64))
    lcl_prompt_version: Mapped[str] = mapped_column(String(100))
    judgment_rubric_version: Mapped[str] = mapped_column(String(100))
    gap_prompt_version: Mapped[str] = mapped_column(String(100))
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    counts_json: Mapped[str] = mapped_column(Text, default="{}")
    review_revision: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ready_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(
        String(40),
        default=utc_now,
        onupdate=utc_now,
    )


class AnkiJobStageModel(Base):
    __tablename__ = "anki_job_stages"
    __table_args__ = (
        UniqueConstraint("job_id", "stage"),
        Index("ix_anki_job_stages_job_state", "job_id", "state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("anki_curation_jobs.id"))
    stage: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(30), default="pending")
    attempt_count: Mapped[int] = mapped_column(default=0)
    provider: Mapped[str | None] = mapped_column(String(30), nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cost_microusd: Mapped[int] = mapped_column(default=0)
    cache_hits: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AnkiCandidateModel(Base):
    __tablename__ = "anki_candidates"
    __table_args__ = (
        UniqueConstraint("job_id", "note_id"),
        Index("ix_anki_candidates_job_verdict_selected", "job_id", "verdict", "selected"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("anki_curation_jobs.id"))
    note_id: Mapped[int]
    content_hash: Mapped[str] = mapped_column(String(64))
    best_concept_id: Mapped[str] = mapped_column(String(200))
    provenance_json: Mapped[str] = mapped_column(Text)
    scores_json: Mapped[str] = mapped_column(Text)
    predicted_band: Mapped[str] = mapped_column(String(30))
    verdict: Mapped[str] = mapped_column(String(30))
    confidence: Mapped[float]
    reason: Mapped[str] = mapped_column(Text)
    context_trap: Mapped[bool] = mapped_column(default=False)
    recall_direction: Mapped[str] = mapped_column(String(30))
    mnemonic_classification: Mapped[str] = mapped_column(String(50))
    dedupe_disposition: Mapped[str] = mapped_column(String(50))
    selected: Mapped[bool] = mapped_column(default=False)
    retrieval_pass: Mapped[str] = mapped_column(
        String(30),
        default="pass_1",
        server_default="pass_1",
    )


class AnkiGapCardModel(Base):
    __tablename__ = "anki_gap_cards"
    __table_args__ = (UniqueConstraint("job_id", "concept_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("anki_curation_jobs.id"))
    concept_id: Mapped[str] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text)
    extra: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(default=1)
    selected: Mapped[bool] = mapped_column(default=True)
    image_state: Mapped[str] = mapped_column(String(30), default="none")
    media_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_note_id: Mapped[int | None] = mapped_column(nullable=True)
    generated_image_json: Mapped[str] = mapped_column(Text, default="{}")
    validation_state: Mapped[str] = mapped_column(String(30), default="valid")
    source_refs_json: Mapped[str] = mapped_column(
        Text,
        default="[]",
        server_default="[]",
    )
    evidence_ids_json: Mapped[str] = mapped_column(
        Text,
        default="[]",
        server_default="[]",
    )
    provenance_json: Mapped[str] = mapped_column(
        Text,
        default="{}",
        server_default="{}",
    )
    initial_tags_json: Mapped[str] = mapped_column(
        Text,
        default="[]",
        server_default="[]",
    )
    content_hash: Mapped[str] = mapped_column(
        String(64),
        default=EMPTY_SHA256,
        server_default=EMPTY_SHA256,
    )


class AnkiSourceEvidenceModel(Base):
    __tablename__ = "anki_source_evidence"
    __table_args__ = (
        UniqueConstraint("job_id", "evidence_id"),
        Index("ix_anki_source_evidence_job_concept", "job_id", "concept_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("anki_curation_jobs.id"))
    evidence_id: Mapped[str] = mapped_column(String(200))
    concept_id: Mapped[str] = mapped_column(String(200))
    support: Mapped[str] = mapped_column(String(30))
    statement: Mapped[str] = mapped_column(Text)
    source_refs_json: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class AnkiStageArtifactModel(Base):
    __tablename__ = "anki_stage_artifacts"
    __table_args__ = (
        UniqueConstraint("job_id", "artifact_id"),
        Index("ix_anki_stage_artifacts_job_stage", "job_id", "stage"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("anki_curation_jobs.id"))
    artifact_id: Mapped[str] = mapped_column(String(200))
    stage: Mapped[str] = mapped_column(String(30))
    kind: Mapped[str] = mapped_column(String(100))
    relative_path: Mapped[str] = mapped_column(Text)
    input_sha256: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class AnkiTagPatchModel(Base):
    __tablename__ = "anki_tag_patches"
    __table_args__ = (
        UniqueConstraint("job_id", "note_id", "revision"),
        Index("ix_anki_tag_patches_job_note", "job_id", "note_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("anki_curation_jobs.id"))
    note_id: Mapped[int]
    revision: Mapped[int] = mapped_column(default=1)
    before_json: Mapped[str] = mapped_column(Text)
    after_json: Mapped[str] = mapped_column(Text)
    add_tags_json: Mapped[str] = mapped_column(Text)
    remove_tags_json: Mapped[str] = mapped_column(Text)
    expected_tag_hash: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class AnkiVerdictCacheModel(Base):
    __tablename__ = "anki_verdict_cache"
    __table_args__ = (
        UniqueConstraint(
            "note_content_hash",
            "lecture_id",
            "lcl_version",
            "rubric_version",
            "instruction_hash",
            "provider",
            "model",
        ),
        Index(
            "ix_anki_verdict_cache_key",
            "note_content_hash",
            "lecture_id",
            "lcl_version",
            "rubric_version",
            "instruction_hash",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    note_content_hash: Mapped[str] = mapped_column(String(64))
    lecture_id: Mapped[int] = mapped_column(ForeignKey("lectures.id"))
    lcl_version: Mapped[str] = mapped_column(String(100))
    rubric_version: Mapped[str] = mapped_column(String(100))
    instruction_hash: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(200))
    verdict_json: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cost_microusd: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class AnkiCoverageJudgmentCacheModel(Base):
    __tablename__ = "anki_coverage_judgment_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    concept_content_hash: Mapped[str] = mapped_column(String(64))
    candidate_digest: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(200))
    result_json: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cost_microusd: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class AnkiEnvelopeModel(Base):
    __tablename__ = "anki_envelopes"
    __table_args__ = (
        UniqueConstraint("job_id"),
        Index("ix_anki_envelopes_state_created", "state", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("anki_curation_jobs.id"))
    payload_json: Mapped[str] = mapped_column(Text)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    snapshot_id: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(30), default="pending")
    receipt_summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(
        String(40),
        default=utc_now,
        onupdate=utc_now,
    )


class AnkiEnvelopeOperationModel(Base):
    __tablename__ = "anki_envelope_operations"
    __table_args__ = (
        UniqueConstraint("envelope_id", "position"),
        UniqueConstraint("envelope_id", "content_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    envelope_id: Mapped[str] = mapped_column(ForeignKey("anki_envelopes.id"))
    position: Mapped[int]
    operation_type: Mapped[str] = mapped_column(String(30))
    content_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(30), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AnkiAgentStateModel(Base):
    __tablename__ = "anki_agent_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    agent_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    heartbeat_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    versions_json: Mapped[str] = mapped_column(Text, default="{}")
    active_snapshot_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_export_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_sync_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    health_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[str] = mapped_column(
        String(40),
        default=utc_now,
        onupdate=utc_now,
    )


class AnkiStageSettingModel(Base):
    __tablename__ = "anki_stage_settings"

    stage: Mapped[str] = mapped_column(String(30), primary_key=True)
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(default=True)
    options_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[str] = mapped_column(
        String(40),
        default=utc_now,
        onupdate=utc_now,
    )


class AnkiAgentCommandModel(Base):
    __tablename__ = "anki_agent_commands"
    __table_args__ = (
        Index("ix_anki_agent_commands_state_created", "state", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    command_type: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(30), default="queued")
    payload_json: Mapped[str] = mapped_column(Text)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_agent_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    claimed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
