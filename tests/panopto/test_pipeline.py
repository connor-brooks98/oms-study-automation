import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oms_hub.config import Settings
from oms_hub.domain import LectureStepName, StepStatus
from oms_hub.panopto.domain import PanoptoSession, RecordingMatch, TranscriptAction
from oms_hub.panopto.openai_client import CleanResult, OpenAITransientError
from oms_hub.panopto.pipeline import (
    TranscriptPipeline,
    TranscriptValidationError,
)
from oms_hub.panopto.prompt import PromptLoader
from oms_hub.panopto.repository import PanoptoRepository
from oms_hub.repositories import CatalogRepository, LectureInput


class FakePanopto:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.downloads = 0

    def download_captions(self, download_url: str, max_bytes: int) -> bytes:
        self.downloads += 1
        return self.payload


class FakeCleaner:
    def __init__(self, text: str | None = None, error: Exception | None = None):
        self.text = text
        self.error = error
        self.calls = 0

    def clean(self, raw_text, prompt):
        self.calls += 1
        if self.error:
            raise self.error
        cleaned = self.text if self.text is not None else raw_text
        return CleanResult(
            cleaned,
            "gpt-5.6-terra",
            f"resp_{self.calls}",
            100,
            80,
            1450,
        )


def prepared_pipeline(database, tmp_path, raw_text="Raw shoulder transcript."):
    catalog = CatalogRepository(database)
    lecture_id = catalog.upsert_lecture(
        LectureInput(
            "MSK",
            1,
            6,
            "Shoulder Disease Injury and Treatment",
            "Joseph Silvers, DO",
            None,
        )
    )
    catalog.update_schedule(lecture_id, "2026-07-23T12:00:00+00:00", "DCOM 101")
    repository = PanoptoRepository(database)
    disposition = repository.upsert_recording(
        PanoptoSession(
            "8796399e-393c-4256-b6e4-b48f0150d156",
            "6H. MSK Shoulder Disease Injury and Treatment",
            datetime(2026, 7, 23, 13, tzinfo=UTC),
            3600.0,
            "MSK",
            "English_USA",
            None,
        ),
        RecordingMatch(lecture_id, 0.98, ("schedule", "topic"), False),
    )
    prompt_path = tmp_path / "vault" / "Transcript Cleaning.md"
    prompt_path.parent.mkdir()
    prompt_path.write_text("Preserve all facts.", encoding="utf-8")
    approved = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
    settings = Settings(
        _env_file=None,
        panopto_revision_root=tmp_path / "ProgramData" / "revisions",
        study_root=tmp_path / "OMS II",
        transcript_prompt_path=prompt_path,
    )
    fake_panopto = FakePanopto(raw_text.encode())
    cleaner = FakeCleaner()
    pipeline = TranscriptPipeline(
        repository,
        catalog,
        fake_panopto,
        PromptLoader(prompt_path, approved),
        cleaner,
        settings,
    )
    return pipeline, catalog, lecture_id, disposition.recording_id, fake_panopto, cleaner


def test_download_clean_file_and_checklist(database, tmp_path):
    pipeline, catalog, lecture_id, recording_id, _, _ = prepared_pipeline(
        database,
        tmp_path,
        "Raw shoulder transcript with substantive medical detail.",
    )

    revision_id = pipeline.ingest_captions(
        recording_id, "https://captions.example/file.txt"
    )
    assert pipeline.run_next()
    assert pipeline.run_next()

    lecture = catalog.get_lecture(lecture_id)
    assert lecture is not None
    statuses = {step.name: step.status for step in lecture.steps}
    assert statuses[LectureStepName.TRANSCRIPT_DOWNLOADED] == StepStatus.COMPLETE
    assert statuses[LectureStepName.TRANSCRIPT_CLEANED] == StepStatus.COMPLETE
    assert statuses[LectureStepName.TRANSCRIPT_FILED] == StepStatus.COMPLETE
    revision = pipeline.repository.get_revision(revision_id)
    assert Path(revision.raw_path).read_text(encoding="utf-8").startswith("Raw shoulder")
    assert Path(revision.cleaned_path or "").is_file()
    assert Path(revision.canonical_path or "").name.endswith("Transcript.txt")
    assert "MSK/Exam 1/Transcripts" in (revision.canonical_path or "")


def test_identical_caption_hash_does_not_call_openai_twice(database, tmp_path):
    pipeline, _, _, recording_id, _, cleaner = prepared_pipeline(
        database, tmp_path, "same transcript"
    )

    first = pipeline.ingest_captions(recording_id, "https://captions.example/file.txt")
    second = pipeline.ingest_captions(recording_id, "https://captions.example/file.txt")
    assert first == second
    assert pipeline.repository.job_count(first, TranscriptAction.CLEAN) == 1
    assert pipeline.run_next()
    assert cleaner.calls == 1


def test_invalid_or_overcompressed_caption_never_reaches_canonical_path(database, tmp_path):
    pipeline, _, _, recording_id, fake_panopto, cleaner = prepared_pipeline(
        database, tmp_path, "A substantive transcript long enough to validate."
    )
    fake_panopto.payload = b"<html>login</html>"
    with pytest.raises(TranscriptValidationError):
        pipeline.ingest_captions(recording_id, "https://captions.example/file.txt")

    fake_panopto.payload = b"A substantive transcript long enough to validate."
    cleaner.text = "short"
    revision_id = pipeline.ingest_captions(
        recording_id, "https://captions.example/file.txt"
    )
    assert pipeline.run_next()
    revision = pipeline.repository.get_revision(revision_id)
    job = pipeline.repository.get_job(revision_id, TranscriptAction.CLEAN)
    assert job is not None and job.state == "needs_review"
    assert revision.cleaned_path is None
    assert revision.canonical_path is None


def test_transient_cleaning_failure_exhausts_three_attempts(database, tmp_path):
    pipeline, _, _, recording_id, _, cleaner = prepared_pipeline(database, tmp_path)
    cleaner.error = OpenAITransientError("temporary")
    revision_id = pipeline.ingest_captions(
        recording_id, "https://captions.example/file.txt"
    )
    start = datetime(2026, 7, 23, 14, tzinfo=UTC)

    assert pipeline.run_next(start)
    assert pipeline.run_next(start + timedelta(days=1))
    assert pipeline.run_next(start + timedelta(days=2))

    job = pipeline.repository.get_job(revision_id, TranscriptAction.CLEAN)
    assert job is not None
    assert job.attempts == 3
    assert job.state == "failed"
    assert job.error == "temporary"


def test_recovery_requeues_running_clean_job_when_raw_is_intact(database, tmp_path):
    pipeline, _, _, recording_id, _, _ = prepared_pipeline(database, tmp_path)
    revision_id = pipeline.ingest_captions(
        recording_id, "https://captions.example/file.txt"
    )
    claimed = pipeline.repository.claim_next_job(datetime.now(UTC))
    assert claimed is not None and claimed.state == "running"

    report = pipeline.recover_abandoned_jobs()

    assert report.requeued == 1
    job = pipeline.repository.get_job(revision_id, TranscriptAction.CLEAN)
    assert job is not None and job.state == "queued"
