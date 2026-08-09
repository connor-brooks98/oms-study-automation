import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock

import pytest
from sqlalchemy import text

from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.ingestion.domain import StagedUpload, UploadKind, UploadState
from oms_hub.ingestion.matcher import UploadMatcher
from oms_hub.ingestion.repository import IngestionRepository, TranscriptAdmissionPending
from oms_hub.ingestion.service import IngestionService
from oms_hub.ingestion.staging import StagingService
from oms_hub.llm.domain import CleanResult, ProviderName
from oms_hub.models import IngestionJobModel, StudyRevisionModel
from oms_hub.repositories import CatalogRepository, LectureInput
from oms_hub.transcripts.pipeline import TranscriptPipeline
from oms_hub.transcripts.prompt import ApprovedPrompt


class FixedPrompt:
    def current(self) -> ApprovedPrompt:
        return ApprovedPrompt("Keep all facts.", "a" * 64)


class CountingCleaner:
    def __init__(self) -> None:
        self.calls = 0
        self.lock = Lock()

    def clean(self, raw_text: str, prompt: ApprovedPrompt) -> CleanResult:
        with self.lock:
            self.calls += 1
        return CleanResult(
            text=raw_text,
            provider=ProviderName.OPENAI,
            model="test",
            request_id="request",
            input_tokens=1,
            output_tokens=1,
            cost_microusd=0,
        )


def _prepared(tmp_path: Path) -> tuple[Database, IngestionRepository, int]:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE UNIQUE INDEX "
                "uq_study_revisions_transcript_cleaning_lecture "
                "ON study_revisions(lecture_id) "
                "WHERE kind='transcripts' AND state='cleaning'"
            )
        )
    lecture_id = CatalogRepository(database).upsert_lecture(
        LectureInput("Cardiology", 1, 7, "Heart Failure", "Dr Test", None)
    )
    return database, IngestionRepository(database), lecture_id


def _add(
    repository: IngestionRepository,
    root: Path,
    item_id: str,
) -> None:
    payload = f"{item_id} transcript with enough facts.".encode()
    path = root / f"{item_id}.txt"
    path.write_bytes(payload)
    batch_id = repository.create_batch(UploadKind.TRANSCRIPTS)
    repository.add_item(
        UploadKind.TRANSCRIPTS,
        StagedUpload(
            batch_id,
            item_id,
            path,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            path.name,
        ),
    )


def test_concurrent_first_transcripts_allow_one_paid_cleaner_call(tmp_path: Path) -> None:
    database, repository, lecture_id = _prepared(tmp_path)
    _add(repository, tmp_path, "first")
    _add(repository, tmp_path, "second")
    barrier = Barrier(2)

    def assign(item_id: str) -> None:
        barrier.wait()
        repository.set_manual_assignment(item_id, lecture_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(assign, ("first", "second")))

    queued = [
        item_id
        for item_id in ("first", "second")
        if repository.require_item(item_id).state is UploadState.QUEUED
    ]
    waiting = [
        item_id
        for item_id in ("first", "second")
        if repository.require_item(item_id).state is UploadState.AWAITING_CONFIRMATION
    ]
    assert len(queued) == 1
    assert len(waiting) == 1
    assert repository.count_jobs(queued[0], "process") == 1
    assert repository.count_jobs(waiting[0], "process") == 0

    cleaner = CountingCleaner()
    pipeline = TranscriptPipeline(
        database,
        Settings(
            _env_file=None,
            data_dir=tmp_path / "data",
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
            study_root=tmp_path / "study",
            transcript_min_clean_ratio=0.1,
            transcript_max_clean_ratio=2.0,
        ),
        FixedPrompt(),
        cleaner,
    )
    pipeline.process(queued[0])

    assert cleaner.calls == 1
    assert repository.require_item(waiting[0]).state is UploadState.AWAITING_CONFIRMATION


def test_recovery_requeues_orphaned_cleaning_owner(tmp_path: Path) -> None:
    database, repository, lecture_id = _prepared(tmp_path)
    _add(repository, tmp_path, "orphan")
    repository.set_manual_assignment("orphan", lecture_id)
    with database.session() as session:
        job = session.scalar(
            text("SELECT id FROM ingestion_jobs WHERE upload_item_id='orphan'")
        )
        assert job is not None
        stored = session.get(IngestionJobModel, job)
        assert stored is not None
        session.delete(stored)

    recovered = repository.recover_interrupted_jobs()

    assert recovered >= 1
    assert repository.require_item("orphan").state is UploadState.QUEUED
    assert repository.count_jobs("orphan", "process") == 1
    with database.session() as session:
        revision = session.scalar(
            text("SELECT id FROM study_revisions WHERE upload_item_id='orphan'")
        )
        assert revision is not None
        assert session.get(StudyRevisionModel, revision).state == "cleaning"  # type: ignore[union-attr]


def test_confirmed_loser_cannot_bypass_cleaning_admission(tmp_path: Path) -> None:
    database, repository, lecture_id = _prepared(tmp_path)
    _add(repository, tmp_path, "winner")
    _add(repository, tmp_path, "loser")
    repository.set_manual_assignment("winner", lecture_id)
    repository.set_manual_assignment("loser", lecture_id)
    assert repository.require_item("winner").state is UploadState.QUEUED
    assert repository.require_item("loser").state is UploadState.AWAITING_CONFIRMATION

    service = IngestionService(
        repository,
        CatalogRepository(database),
        UploadMatcher(),
        StagingService(tmp_path / "staging", 1_000_000, 2_000_000),
    )
    confirmed = service.confirm_processing("loser")
    assert confirmed.state is UploadState.QUEUED
    assert repository.count_jobs("loser", "confirmed_process") == 1

    cleaner = CountingCleaner()
    pipeline = TranscriptPipeline(
        database,
        Settings(
            _env_file=None,
            data_dir=tmp_path / "data",
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
            study_root=tmp_path / "study",
            transcript_min_clean_ratio=0.1,
            transcript_max_clean_ratio=2.0,
        ),
        FixedPrompt(),
        cleaner,
    )
    with pytest.raises(TranscriptAdmissionPending):
        pipeline.process("loser")
    assert cleaner.calls == 0

    pipeline.process("winner")
    assert cleaner.calls == 1

    replacement = pipeline.process("loser")
    assert cleaner.calls == 2
    assert replacement.current is False
    assert repository.require_item("loser").state is UploadState.NEEDS_REVIEW
