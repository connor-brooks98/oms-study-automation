from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from oms_hub.anki.audit import AuditCacheRecord
from oms_hub.anki.contracts import SyncOperation
from oms_hub.anki.domain import (
    ApplyState,
    Candidate,
    CreateCurationJob,
    CurationStage,
    CurationState,
    EnvelopeDraft,
    EnvelopeOperationDraft,
    EvidenceSupport,
    GapCard,
    GapCardEdit,
    RetrievalPass,
    ReviewChangeSet,
    SourceEvidence,
    SourceKind,
    SourceReference,
    StageArtifact,
    StageUsage,
    TagPatch,
)
from oms_hub.anki.envelope import EnvelopeBuilder
from oms_hub.anki.judgment import JudgmentCacheRecord
from oms_hub.anki.repository import (
    AnkiCurationRepository,
    InvalidCurationTransition,
)
from oms_hub.anki.tag_policy import TagPolicy
from oms_hub.db import Database
from oms_hub.llm.domain import ProviderName
from oms_hub.models import LectureModel

_OPEN_DATABASES: list[Database] = []


@pytest.fixture(autouse=True)
def _close_databases() -> None:
    yield
    while _OPEN_DATABASES:
        _OPEN_DATABASES.pop().close()


def _prepared_repository(tmp_path: Path) -> tuple[AnkiCurationRepository, int]:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    _OPEN_DATABASES.append(database)
    database.migrate()
    with database.session() as session:
        lecture = LectureModel(
            subject="Heme Lymph",
            exam_number=1,
            lecture_number=4,
            topic="Anemia I",
            lecturer="Professor",
        )
        session.add(lecture)
        session.flush()
        lecture_id = lecture.id
    return AnkiCurationRepository(database), lecture_id


def _job_request(lecture_id: int, *, snapshot: str = "snapshot-1") -> CreateCurationJob:
    return CreateCurationJob(
        lecture_id=lecture_id,
        block_id="heme-block-1",
        source_revision_ids=(101, 102),
        deck_allowlist=("AnKing Step Deck",),
        tag_allowlist=("#AK_Step2_v12::Hematology",),
        instruction_text="Focus on red-highlighted material.",
        target_deck="OMS-II_Custom_Cards::Heme_Lymph::Exam_1::Lec4_Anemia_I",
        target_tag=(
            "AnkiHub_Optional::LMU_OMS_II::HemeLymph::Block1::Lec4_Anemia_I"
        ),
        index_snapshot_id=snapshot,
        lcl_prompt_version="lcl-v1",
        judgment_rubric_version="judgment-v1",
        gap_prompt_version="gap-v1",
        provider="anthropic",
        model="claude-sonnet-5",
        summary_outline_id=91,
        summary_outline_sha256="b" * 64,
    )


def test_create_job_snapshots_all_mutable_inputs(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)

    job = repository.create_job(_job_request(lecture_id))

    assert job.lecture_id == lecture_id
    assert job.state is CurationState.QUEUED
    assert job.instruction_text == "Focus on red-highlighted material."
    assert len(job.instruction_sha256) == 64
    assert job.block_id == "heme-block-1"
    assert job.source_revision_ids == (101, 102)
    assert job.summary_outline_id == 91
    assert job.summary_outline_sha256 == "b" * 64
    assert job.deck_allowlist == ("AnKing Step Deck",)
    assert job.tag_allowlist == ("#AK_Step2_v12::Hematology",)
    assert job.apply_state is ApplyState.PENDING
    assert job.target_deck.endswith("::Lec4_Anemia_I")
    assert job.target_tag.endswith("::Lec4_Anemia_I")
    assert job.index_snapshot_id == "snapshot-1"
    assert job.lcl_prompt_version == "lcl-v1"
    assert job.judgment_rubric_version == "judgment-v1"
    assert job.gap_prompt_version == "gap-v1"


def test_claim_next_job_claims_oldest_queued_job_once(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    first = repository.create_job(_job_request(lecture_id, snapshot="snapshot-1"))
    second = repository.create_job(_job_request(lecture_id, snapshot="snapshot-2"))
    now = datetime(2026, 7, 27, 15, 0, tzinfo=UTC)

    claimed_first = repository.claim_next_job(now)
    claimed_second = repository.claim_next_job(now)

    assert claimed_first is not None
    assert claimed_first.id == first.id
    assert claimed_first.state is CurationState.PREFLIGHT
    assert claimed_first.attempts == 1
    assert claimed_second is not None
    assert claimed_second.id == second.id
    assert repository.claim_next_job(now) is None


def test_transition_requires_expected_state_and_allowed_edge(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))

    with pytest.raises(InvalidCurationTransition):
        repository.transition(
            job.id,
            CurationState.QUEUED,
            CurationState.JUDGING_PASS_1,
        )

    claimed = repository.claim_next_job(datetime.now(UTC))
    assert claimed is not None
    retrieved = repository.transition(
        claimed.id,
        CurationState.PREFLIGHT,
        CurationState.BUILDING_LCL,
    )
    assert retrieved.state is CurationState.BUILDING_LCL

    with pytest.raises(InvalidCurationTransition):
        repository.transition(
            claimed.id,
            CurationState.PREFLIGHT,
            CurationState.BUILDING_LCL,
        )


def test_recovery_releases_interrupted_pre_review_jobs_in_place(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    interrupted = repository.create_job(_job_request(lecture_id, snapshot="snapshot-1"))
    envelope_pending = repository.create_job(
        _job_request(lecture_id, snapshot="snapshot-2")
    )
    claimed = repository.claim_next_job(datetime.now(UTC))
    assert claimed is not None
    repository.transition(
        envelope_pending.id,
        CurationState.QUEUED,
        CurationState.PREFLIGHT,
    )
    for current, target in [
        (CurationState.PREFLIGHT, CurationState.BUILDING_LCL),
        (CurationState.BUILDING_LCL, CurationState.RETRIEVING_PASS_1),
        (CurationState.RETRIEVING_PASS_1, CurationState.JUDGING_PASS_1),
        (
            CurationState.JUDGING_PASS_1,
            CurationState.LOCALIZING_MISSED_CONCEPTS,
        ),
        (
            CurationState.LOCALIZING_MISSED_CONCEPTS,
            CurationState.RETRIEVING_PASS_2,
        ),
        (CurationState.RETRIEVING_PASS_2, CurationState.JUDGING_PASS_2),
        (CurationState.JUDGING_PASS_2, CurationState.DEDUPING),
        (CurationState.DEDUPING, CurationState.GENERATING_GAPS),
        (CurationState.GENERATING_GAPS, CurationState.READY_FOR_REVIEW),
        (CurationState.READY_FOR_REVIEW, CurationState.ENVELOPE_PENDING),
    ]:
        repository.transition(envelope_pending.id, current, target)

    assert repository.recover_interrupted_jobs() == 1
    recovered = repository.require_job(interrupted.id)
    assert recovered.state is CurationState.PREFLIGHT
    assert recovered.lease_owner is None
    assert recovered.lease_expires_at is None
    assert (
        repository.require_job(envelope_pending.id).state
        is CurationState.ENVELOPE_PENDING
    )


def test_stage_lifecycle_records_usage_and_safe_failure(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))

    running = repository.start_stage(
        job.id,
        CurationStage.LCL,
        provider="gemini",
        model="gemini-model",
    )
    completed = repository.finish_stage(
        job.id,
        CurationStage.LCL,
        StageUsage(
            request_id="request-1",
            input_tokens=100,
            output_tokens=20,
            cost_microusd=42,
        ),
        cache_hits=3,
    )
    failed = repository.start_stage(job.id, CurationStage.RETRIEVAL_PASS_1)
    failed = repository.fail_stage(
        job.id,
        CurationStage.RETRIEVAL_PASS_1,
        "index is unavailable",
    )

    assert running.attempt_count == 1
    assert completed.state == "complete"
    assert completed.request_id == "request-1"
    assert completed.input_tokens == 100
    assert completed.cache_hits == 3
    assert failed.state == "failed"
    assert failed.error == "index is unavailable"


def test_failed_job_can_retry_its_failed_stage_without_losing_artifacts(
    tmp_path: Path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    claimed = repository.claim_next_job(
        now,
        worker_id="worker-1",
        lease_seconds=30,
    )
    assert claimed is not None
    repository.start_stage(job.id, CurationStage.PREFLIGHT)
    repository.fail_stage(
        job.id,
        CurationStage.PREFLIGHT,
        "provider returned malformed output",
    )
    repository.fail_job(
        job.id,
        "worker-1",
        "provider returned malformed output",
    )

    retried = repository.retry_job(job.id)

    assert retried.state is CurationState.PREFLIGHT
    assert retried.error is None
    assert retried.available_at is None
    claimed_again = repository.claim_next_job(
        now,
        worker_id="worker-2",
        lease_seconds=30,
    )
    assert claimed_again is not None
    assert claimed_again.id == job.id


def test_failed_job_can_be_removed_from_the_run_list(tmp_path: Path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    claimed = repository.claim_next_job(
        now,
        worker_id="worker-1",
        lease_seconds=30,
    )
    assert claimed is not None
    repository.start_stage(job.id, CurationStage.PREFLIGHT)
    repository.fail_stage(job.id, CurationStage.PREFLIGHT, "malformed output")
    repository.fail_job(job.id, "worker-1", "malformed output")

    removed = repository.remove_failed_job(job.id)

    assert removed.state.value == "removed"
    assert repository.list_jobs() == []


def test_nonfailed_job_cannot_be_removed_from_the_run_list(
    tmp_path: Path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))

    with pytest.raises(ValueError, match="failed"):
        repository.remove_failed_job(job.id)

    assert [listed.id for listed in repository.list_jobs()] == [job.id]


def test_source_evidence_and_stage_artifacts_round_trip(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    source_ref = SourceReference(
        source_kind=SourceKind.SLIDE,
        revision_id=101,
        locator="slide:7",
        content_hash="b" * 64,
    )
    evidence = SourceEvidence(
        evidence_id="evidence-1",
        concept_id="concept-anemia",
        support=EvidenceSupport.SUPPORTED,
        statement="Iron deficiency causes microcytic anemia.",
        source_refs=(source_ref,),
        content_hash="c" * 64,
    )
    artifact = StageArtifact(
        artifact_id="artifact-1",
        stage=CurationStage.SOURCE_INDEX,
        kind="source-index-manifest",
        relative_path="jobs/example/source-index-manifest.json",
        input_sha256="d" * 64,
        content_sha256="e" * 64,
        metadata={"passages": 12},
    )

    repository.replace_source_evidence(job.id, (evidence,))
    repository.save_stage_artifact(job.id, artifact)

    assert repository.list_source_evidence(job.id) == [evidence]
    assert repository.list_stage_artifacts(job.id) == [artifact]


def test_candidates_gaps_and_review_revision_are_persisted(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    repository.replace_candidates(
        job.id,
        [
            Candidate(
                note_id=1479430487028,
                content_hash="a" * 64,
                best_concept_id="concept-anemia",
                provenance={"lecture_tag": True},
                scores={"semantic": 0.91},
                predicted_band="auto_include",
                verdict="include",
                confidence=0.98,
                reason="Directly tests the lecture objective.",
                context_trap=False,
                recall_direction="forward",
                mnemonic_classification="none",
                dedupe_disposition="survivor",
                selected=True,
                retrieval_pass=RetrievalPass.PASS_1,
            )
        ],
    )
    repository.save_gap_cards(
        job.id,
        [
            GapCard(
                concept_id="concept-retic",
                text="{{c1::Reticulocytes}} rise after treatment.",
                extra="Tracks marrow response.",
            )
        ],
    )

    saved = repository.save_review(
        job.id,
        ReviewChangeSet(
            expected_revision=0,
            candidate_selections={1479430487028: False},
            gap_edits=(
                GapCardEdit(
                    concept_id="concept-retic",
                    text="{{c1::Reticulocyte count}} rises after treatment.",
                    extra="Tracks marrow response after iron replacement.",
                    selected=True,
                ),
            ),
        ),
    )

    assert saved.revision == 1
    assert repository.list_candidates(job.id)[0].selected is False
    stored_gap = repository.list_gap_cards(job.id)[0]
    assert stored_gap.revision == 2
    assert stored_gap.text.startswith("{{c1::Reticulocyte count}}")

    with pytest.raises(ValueError, match="review revision"):
        repository.save_review(
            job.id,
            ReviewChangeSet(expected_revision=0),
        )


def test_coverage_judgment_cache_round_trips_immutable_record(
    tmp_path,
) -> None:
    repository, _ = _prepared_repository(tmp_path)
    record = JudgmentCacheRecord(
        cache_key="a" * 64,
        concept_content_hash="b" * 64,
        candidate_digest="c" * 64,
        prompt_version="judgment-v1",
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        result={
            "status": "missing",
            "supporting_note_ids": [],
            "missing_facts": ["Treatment response is absent."],
            "rationale": "No candidate covers treatment response.",
        },
        input_tokens=20,
        output_tokens=10,
        cost_microusd=5,
        created_at="2026-07-30T12:00:00+00:00",
    )

    repository.save_judgment_cache(record)
    repository.save_judgment_cache(record)

    assert repository.get_judgment_cache(record.cache_key) == record


def test_card_audit_cache_round_trips_immutable_record(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    record = AuditCacheRecord(
        cache_key="d" * 64,
        note_id=123,
        lecture_id=lecture_id,
        note_content_hash="a" * 64,
        source_digest="b" * 64,
        prompt_hash="123456789abc",
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        result={
            "nid": 123,
            "verdict": "keep",
            "primary_subject": "iron deficiency",
            "support": "both",
            "reason": "Supported by slides and transcript",
            "structure_issue": [],
        },
        input_tokens=100,
        output_tokens=20,
        cost_microusd=30,
        created_at="2026-07-31T12:00:00+00:00",
    )

    repository.save_audit_cache(record)
    repository.save_audit_cache(record)

    assert repository.get_audit_cache(record.cache_key) == record


def test_lecture_title_is_available_for_blind_audit_context(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)

    assert repository.lecture_title(lecture_id) == (
        "Heme Lymph Exam 1 Lecture 4: Anemia I"
    )


def test_review_changes_and_tag_patches_are_append_only(
    tmp_path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    first_patch = TagPatch(
        note_id=42,
        before=("OMS::Old",),
        after=("OMS::New",),
        add_tags=("OMS::New",),
        remove_tags=("OMS::Old",),
        expected_tag_hash="a" * 64,
        tag_policy_version="tags-v1",
    )
    second_patch = TagPatch(
        note_id=42,
        before=("OMS::New",),
        after=("OMS::Final",),
        add_tags=("OMS::Final",),
        remove_tags=("OMS::New",),
        expected_tag_hash="b" * 64,
        tag_policy_version="tags-v1",
    )

    repository.save_review(
        job.id,
        ReviewChangeSet(
            expected_revision=0,
            reviewer="connor",
            tag_patches=(first_patch,),
        ),
    )
    repository.save_review(
        job.id,
        ReviewChangeSet(
            expected_revision=1,
            reviewer="connor",
            tag_patches=(second_patch,),
        ),
    )

    assert repository.list_tag_patches(job.id) == [
        first_patch,
        second_patch,
    ]
    changes = repository.list_review_changes(job.id)
    assert [change.revision for change in changes] == [1, 2]
    assert [change.prior_revision for change in changes] == [0, 1]
    assert all(change.reviewer == "connor" for change in changes)


def test_envelope_is_immutable_and_receipt_updates_delivery_state(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    draft = EnvelopeDraft(
        envelope_id="5dc4f15e-df92-4a32-964e-026b5d518a80",
        snapshot_id="snapshot-1",
        payload={"target_tag": "AnkiHub_Optional::LMU_OMS_II::HemeLymph"},
        operations=(
            EnvelopeOperationDraft(
                operation_id="3b9d1dbb-b57b-46f4-8346-fd45e0105042",
                operation_type="add_tags",
                payload={"note_ids": [1479430487028]},
            ),
        ),
    )

    stored = repository.create_envelope(job.id, draft)
    delivered = repository.record_receipt(
        stored.id,
        {"sync_status": "complete", "verified": True},
    )

    assert len(stored.payload_sha256) == 64
    assert stored.state == "pending"
    assert delivered.state == "complete"
    assert delivered.receipt_summary == {
        "sync_status": "complete",
        "verified": True,
    }
    with pytest.raises(ValueError, match="already has an envelope"):
        repository.create_envelope(job.id, draft)


def test_action_envelope_operation_journal_is_durable(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    envelope = EnvelopeBuilder(
        TagPolicy(
            pipeline_owned_roots=("OMS",),
            approved_optional_roots=("AnkiHub_Optional::LMU_OMS_II",),
            source_managed_roots=("#Pathoma",),
            version="tags-v1",
        )
    ).build(
        ReviewChangeSet(expected_revision=0),
        {},
        envelope_id=UUID("5dc4f15e-df92-4a32-964e-026b5d518a80"),
        snapshot_id="snapshot-1",
        target_deck="OMS::Heme::Lecture 3",
        target_tag="AnkiHub_Optional::LMU_OMS_II::Heme::Lecture_3",
    )
    sync = next(
        operation
        for operation in envelope.operations
        if isinstance(operation, SyncOperation)
    )

    stored = repository.create_action_envelope(job.id, envelope)
    repository.begin_operation(envelope.envelope_id, sync.operation_id)
    repository.complete_operation(
        envelope.envelope_id,
        sync.operation_id,
        {"sync_status": "complete"},
    )
    repository.set_apply_state(
        envelope.envelope_id,
        ApplyState.APPLIED_LOCAL_SYNC_RETRYABLE,
        {"safe_error": "network unavailable"},
    )

    assert stored.payload_sha256 == envelope.payload_sha256
    assert repository.get_envelope(envelope.envelope_id) == envelope
    operation = repository.operation_record(
        envelope.envelope_id,
        sync.operation_id,
    )
    assert operation.state == "complete"
    assert operation.attempts == 1
    assert operation.result == {"sync_status": "complete"}
    assert (
        repository.require_job(job.id).apply_state
        is ApplyState.APPLIED_LOCAL_SYNC_RETRYABLE
    )
