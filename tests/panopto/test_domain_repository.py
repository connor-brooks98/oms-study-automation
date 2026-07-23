from datetime import UTC, datetime

from oms_hub.domain import LectureStepName, StepStatus
from oms_hub.panopto.domain import PanoptoSession, RecordingMatch, TranscriptAction
from oms_hub.panopto.repository import PanoptoRepository
from oms_hub.repositories import CatalogRepository, LectureInput


def test_recording_and_raw_revision_are_idempotent(database, tmp_path):
    catalog = CatalogRepository(database)
    lecture_id = catalog.upsert_lecture(
        LectureInput("MSK", 1, 6, "Shoulder Disease Injury and Treatment", "Silvers", None)
    )
    catalog.update_schedule(lecture_id, "2026-07-23T12:00:00+00:00", "DCOM")
    repository = PanoptoRepository(database, "https://lmunet.hosted.panopto.com")
    session = PanoptoSession(
        "8796399e-393c-4256-b6e4-b48f0150d156",
        "6H. MSK Shoulder Disease Injury and Treatment",
        datetime(2026, 7, 23, 12, tzinfo=UTC),
        3600.0,
        "MSK",
        "English_USA",
        "https://captions.example/download",
    )

    disposition = repository.upsert_recording(
        session,
        RecordingMatch(lecture_id, 0.98, ("schedule", "topic"), False),
    )
    raw_path = tmp_path / "raw.txt"
    raw_path.write_text("shoulder transcript", encoding="utf-8")
    first = repository.create_raw_revision(disposition.recording_id, "a" * 64, str(raw_path))
    second = repository.create_raw_revision(disposition.recording_id, "a" * 64, str(raw_path))
    repository.queue_job(first.id, TranscriptAction.CLEAN)
    repository.queue_job(first.id, TranscriptAction.CLEAN)

    assert first.id == second.id
    assert repository.job_count(first.id, TranscriptAction.CLEAN) == 1
    lecture = catalog.get_lecture(lecture_id)
    assert lecture is not None
    statuses = {step.name: step.status for step in lecture.steps}
    assert statuses[LectureStepName.PANOPTO_RECORDING_FOUND] == StepStatus.COMPLETE


def test_jobs_are_claimed_once_in_creation_order(database):
    catalog = CatalogRepository(database)
    lecture_id = catalog.upsert_lecture(
        LectureInput("MSK", 1, 6, "Shoulder", "Silvers", None)
    )
    repository = PanoptoRepository(database, "https://lmunet.hosted.panopto.com")
    disposition = repository.upsert_recording(
        PanoptoSession(
            "8796399e-393c-4256-b6e4-b48f0150d156",
            "Shoulder",
            datetime(2026, 7, 23, 12, tzinfo=UTC),
            3600.0,
            "MSK",
            "English_USA",
            None,
        ),
        RecordingMatch(lecture_id, 1.0, ("manual",), False),
    )
    revision = repository.create_raw_revision(disposition.recording_id, "b" * 64, "raw.txt")
    repository.queue_job(revision.id, TranscriptAction.CLEAN)

    claimed = repository.claim_next_job(datetime(2026, 7, 23, 14, tzinfo=UTC))

    assert claimed is not None
    assert claimed.state == "running"
    assert repository.claim_next_job(datetime(2026, 7, 23, 14, tzinfo=UTC)) is None


def test_catalog_schedule_queries_use_utc_bounds_and_missing_step(database):
    catalog = CatalogRepository(database)
    lecture_id = catalog.upsert_lecture(LectureInput("MSK", 1, 6, "Shoulder", "Silvers", None))
    catalog.update_schedule(lecture_id, "2026-07-23T12:00:00+00:00", "DCOM")

    scheduled = catalog.list_scheduled_between(
        datetime(2026, 7, 23, 0, tzinfo=UTC),
        datetime(2026, 7, 24, 0, tzinfo=UTC),
    )
    missing = catalog.list_missing_transcripts_before(
        datetime(2026, 7, 24, 0, tzinfo=UTC)
    )

    assert [lecture.id for lecture in scheduled] == [lecture_id]
    assert [lecture.id for lecture in missing] == [lecture_id]
    catalog.set_step_status(
        lecture_id,
        LectureStepName.TRANSCRIPT_DOWNLOADED,
        StepStatus.COMPLETE,
    )
    assert catalog.list_missing_transcripts_before(
        datetime(2026, 7, 24, 0, tzinfo=UTC)
    ) == []
