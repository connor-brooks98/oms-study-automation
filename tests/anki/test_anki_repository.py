from datetime import UTC, datetime
from pathlib import Path

import pytest

from oms_hub.anki.domain import (
    Candidate,
    CreateCurationJob,
    CurationStage,
    CurationState,
    EnvelopeDraft,
    EnvelopeOperationDraft,
    GapCard,
    GapCardEdit,
    ReviewChangeSet,
    StageUsage,
)
from oms_hub.anki.repository import (
    AnkiCurationRepository,
    InvalidCurationTransition,
)
from oms_hub.db import Database
from oms_hub.models import LectureModel


def _prepared_repository(tmp_path: Path) -> tuple[AnkiCurationRepository, int]:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
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
        amboss_input="nid:1479430487028 OR nid:1517176548564",
        instruction_text="Focus on red-highlighted material.",
        target_deck="OMS-II_Custom_Cards::Heme_Lymph::Exam_1::Lec4_Anemia_I",
        target_tag=(
            "AnkiHub_Optional::LMU_OMS_II::HemeLymph::Block1::Lec4_Anemia_I"
        ),
        index_snapshot_id=snapshot,
        lcl_prompt_version="lcl-v1",
        judgment_rubric_version="judgment-v1",
        gap_prompt_version="gap-v1",
    )


def test_create_job_snapshots_all_mutable_inputs(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)

    job = repository.create_job(_job_request(lecture_id))

    assert job.lecture_id == lecture_id
    assert job.state is CurationState.QUEUED
    assert job.instruction_text == "Focus on red-highlighted material."
    assert len(job.instruction_sha256) == 64
    assert job.amboss_input == "nid:1479430487028 OR nid:1517176548564"
    assert len(job.amboss_sha256) == 64
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
    assert claimed_first.state is CurationState.BUILDING_LCL
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
            CurationState.JUDGING,
        )

    claimed = repository.claim_next_job(datetime.now(UTC))
    assert claimed is not None
    retrieved = repository.transition(
        claimed.id,
        CurationState.BUILDING_LCL,
        CurationState.RETRIEVING,
    )
    assert retrieved.state is CurationState.RETRIEVING

    with pytest.raises(InvalidCurationTransition):
        repository.transition(
            claimed.id,
            CurationState.BUILDING_LCL,
            CurationState.RETRIEVING,
        )


def test_recovery_requeues_interrupted_pre_review_jobs_only(tmp_path) -> None:
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
        CurationState.BUILDING_LCL,
    )
    for current, target in [
        (CurationState.BUILDING_LCL, CurationState.RETRIEVING),
        (CurationState.RETRIEVING, CurationState.JUDGING),
        (CurationState.JUDGING, CurationState.DEDUPING),
        (CurationState.DEDUPING, CurationState.PROPOSING_GAPS),
        (CurationState.PROPOSING_GAPS, CurationState.READY_FOR_REVIEW),
        (CurationState.READY_FOR_REVIEW, CurationState.ENVELOPE_PENDING),
    ]:
        repository.transition(envelope_pending.id, current, target)

    assert repository.recover_interrupted_jobs() == 1
    assert repository.require_job(interrupted.id).state is CurationState.QUEUED
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
    failed = repository.start_stage(job.id, CurationStage.RETRIEVAL)
    failed = repository.fail_stage(
        job.id,
        CurationStage.RETRIEVAL,
        "index is unavailable",
    )

    assert running.attempt_count == 1
    assert completed.state == "complete"
    assert completed.request_id == "request-1"
    assert completed.input_tokens == 100
    assert completed.cache_hits == 3
    assert failed.state == "failed"
    assert failed.error == "index is unavailable"


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
                provenance={"amboss": True},
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
