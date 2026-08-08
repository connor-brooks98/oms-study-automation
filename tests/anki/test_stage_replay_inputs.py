import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from oms_hub.anki.domain import CreateCurationJob, CurationStage, PipelineContractVersion
from oms_hub.anki.models import AnkiCurationJobModel, AnkiReviewedReconciliationModel
from oms_hub.anki.replay_inputs import PreparedStageReplayInputs, canonical_json, sha256_text
from oms_hub.anki.repository import AnkiCurationRepository
from oms_hub.db import Database
from oms_hub.models import LectureModel


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_replay_input_canonical_json_rejects_nonfinite_values(value: float) -> None:
    with pytest.raises(ValueError, match="finite JSON"):
        canonical_json({"value": value})


@pytest.mark.parametrize("constant", ("NaN", "Infinity", "-Infinity"))
def test_prepared_replay_inputs_reject_nonfinite_json_constants(constant: str) -> None:
    serialized = f'{{"value":{constant}}}'

    with pytest.raises(ValueError, match="finite JSON"):
        PreparedStageReplayInputs(
            job_id=uuid4(),
            stage=CurationStage.RECONCILIATION,
            canonical_json=serialized,
            sha256=sha256_text(serialized),
        )


def test_prepared_replay_inputs_keep_finite_canonical_document_stable() -> None:
    document = {"score": 0.5, "stage": "reconciliation"}
    serialized = canonical_json(document)
    prepared = PreparedStageReplayInputs(
        job_id=uuid4(),
        stage=CurationStage.RECONCILIATION,
        canonical_json=serialized,
        sha256=sha256_text(serialized),
    )

    assert canonical_json(prepared.document) == serialized


def _repository(tmp_path: Path) -> tuple[AnkiCurationRepository, int]:
    database = Database(f"sqlite:///{tmp_path / 'replay-inputs.db'}")
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
        return AnkiCurationRepository(database), lecture.id


def _request(lecture_id: int) -> CreateCurationJob:
    return CreateCurationJob(
        lecture_id=lecture_id,
        block_id=None,
        source_revision_ids=(),
        deck_allowlist=(),
        tag_allowlist=("#scope",),
        instruction_text="focus",
        target_deck="deck",
        target_tag="tag",
        index_snapshot_id="snapshot",
        lcl_prompt_version="lcl-v1",
        judgment_rubric_version="judgment-v1",
        gap_prompt_version="gap-v1",
        provider="anthropic",
        model="claude-sonnet-5",
        pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
    )


def _review_payload(kept: int, total: int) -> str:
    return json.dumps(
        {
            "snapshot": {
                "classifications": [
                    {"verdict": "keep" if index < kept else "discard"}
                    for index in range(total)
                ]
            }
        }
    )


def _add_review(
    repository: AnkiCurationRepository,
    job_id: str,
    revision: int,
    created_at: str,
    *,
    kept: int,
    total: int,
) -> None:
    with repository.database.session() as session:
        session.add(
            AnkiReviewedReconciliationModel(
                job_id=job_id,
                review_revision=revision,
                payload_json=_review_payload(kept, total),
                created_at=created_at,
            )
        )


def test_reconciliation_replay_inputs_freeze_distinct_latest_a11_history(tmp_path: Path) -> None:
    repository, lecture_id = _repository(tmp_path)
    target = repository.create_job(_request(lecture_id))
    crowded = repository.create_job(_request(lecture_id))
    _add_review(
        repository, str(crowded.id), 1, "2026-08-01T00:00:00+00:00", kept=0, total=2
    )
    _add_review(
        repository, str(crowded.id), 2, "2026-08-02T00:00:00+00:00", kept=1, total=2
    )
    _add_review(
        repository, str(crowded.id), 3, "2026-08-03T00:00:00+00:00", kept=2, total=2
    )
    prior_jobs = []
    for index in range(11):
        prior = repository.create_job(_request(lecture_id))
        prior_jobs.append(prior)
        _add_review(
            repository,
            str(prior.id),
            1,
            f"2026-08-{index + 4:02d}T00:00:00+00:00",
            kept=1,
            total=2,
        )

    prepared = repository.prepare_stage_replay_inputs(target.id, CurationStage.RECONCILIATION)
    document = prepared.document
    entries = document["a11_history"]["entries"]

    assert len(entries) == 12
    assert len({entry["job_id"] for entry in entries}) == 12
    crowded_entry = next(entry for entry in entries if entry["job_id"] == str(crowded.id))
    assert crowded_entry["review_revision"] == 3
    assert crowded_entry["yes_rate"] == 1
    assert entries[0]["job_id"] == str(prior_jobs[-1].id)
    assert len(prepared.sha256) == 64
    assert prepared.sha256 == repository.prepare_stage_replay_inputs(
        target.id, CurationStage.RECONCILIATION
    ).sha256

    # A later reviewed revision cannot alter the already-persisted A11 window.
    _add_review(
        repository,
        str(prior_jobs[-1].id),
        2,
        "2026-08-31T00:00:00+00:00",
        kept=0,
        total=2,
    )
    reloaded = repository.prepare_stage_replay_inputs(target.id, CurationStage.RECONCILIATION)
    assert reloaded.canonical_json == prepared.canonical_json
    assert reloaded.document == document


def test_v2_lecture_pin_survives_live_title_edits_and_legacy_first_prepare(tmp_path: Path) -> None:
    repository, lecture_id = _repository(tmp_path)
    created_with_pin = repository.create_job(_request(lecture_id))
    legacy_v2 = repository.create_job(_request(lecture_id))
    with repository.database.session() as session:
        lecture = session.get(LectureModel, lecture_id)
        created = session.get(AnkiCurationJobModel, str(created_with_pin.id))
        legacy = session.get(AnkiCurationJobModel, str(legacy_v2.id))
        assert lecture is not None and created is not None and legacy is not None
        assert created.lecture_title_snapshot == "Heme Lymph Exam 1 Lecture 4: Anemia I"
        legacy.lecture_title_snapshot = None
        legacy.lecture_metadata_json = None
        legacy.lecture_metadata_sha256 = None
        lecture.topic = "Edited before legacy snapshot"

    created_inputs = repository.prepare_stage_replay_inputs(
        created_with_pin.id, CurationStage.CARD_GAP_FILL
    )
    legacy_inputs = repository.prepare_stage_replay_inputs(
        legacy_v2.id, CurationStage.CARD_GAP_FILL
    )
    with repository.database.session() as session:
        lecture = session.get(LectureModel, lecture_id)
        assert lecture is not None
        lecture.topic = "Edited after snapshots"

    assert created_inputs.document["pinned_lecture"]["title"].endswith("Anemia I")
    assert legacy_inputs.document["pinned_lecture"]["title"].endswith(
        "Edited before legacy snapshot"
    )
    assert repository.prepare_stage_replay_inputs(
        created_with_pin.id, CurationStage.CARD_GAP_FILL
    ).canonical_json == created_inputs.canonical_json
    assert repository.prepare_stage_replay_inputs(
        legacy_v2.id, CurationStage.CARD_GAP_FILL
    ).canonical_json == legacy_inputs.canonical_json
    with repository.database.session() as session:
        rows = session.scalars(
            select(AnkiCurationJobModel).where(
                AnkiCurationJobModel.id.in_((str(created_with_pin.id), str(legacy_v2.id)))
            )
        ).all()
    assert all(row.lecture_metadata_sha256 is not None for row in rows)
