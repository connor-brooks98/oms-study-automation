import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from oms_hub.anki.domain import (
    CreateCurationJob,
    CurationStage,
    CurationState,
)
from oms_hub.anki.pipeline import (
    CurationPipeline,
    PinnedInputChanged,
    StageArtifactStore,
    StageContext,
    StageProduct,
)
from oms_hub.anki.repository import AnkiCurationRepository
from oms_hub.anki.worker import AnkiCurationWorker
from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.llm.domain import DiagnosticSource, LLMRequestError
from oms_hub.models import LectureModel


class ControlledRunner:
    def __init__(self) -> None:
        self.calls: list[CurationStage] = []
        self.error: Exception | None = None
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def run(self, context: StageContext) -> StageProduct:
        self.calls.append(context.stage)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        return StageProduct(
            kind="test",
            payload={"stage": context.stage.value},
        )


class ControlledValidator:
    def __init__(self) -> None:
        self.error: str | None = None

    def validate(self, job_id: UUID) -> None:
        del job_id
        if self.error is not None:
            raise PinnedInputChanged(self.error)


@pytest.fixture
def repository(tmp_path: Path) -> AnkiCurationRepository:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    with database.session() as session:
        lecture = LectureModel(
            subject="Heme Lymph",
            exam_number=1,
            lecture_number=4,
            topic="Anemia",
            lecturer="Professor",
        )
        session.add(lecture)
        session.flush()
        lecture_id = lecture.id
    repository = AnkiCurationRepository(database)
    repository._test_lecture_id = lecture_id  # type: ignore[attr-defined]
    yield repository
    database.close()


def _create_job(repository: AnkiCurationRepository):
    lecture_id = repository._test_lecture_id  # type: ignore[attr-defined]
    return repository.create_job(
        CreateCurationJob(
            lecture_id=lecture_id,
            block_id=None,
            source_revision_ids=(11, 12),
            deck_allowlist=("AnKing Step Deck",),
            tag_allowlist=("#Pathoma",),
            instruction_text="",
            target_deck="OMS::Heme::Lecture 4",
            target_tag="AnkiHub_Optional::LMU_OMS_II::Heme::Lecture_4",
            index_snapshot_id="snapshot-1",
            lcl_prompt_version="lcl-v1",
            judgment_rubric_version="judgment-v1",
            gap_prompt_version="gap-v1",
            provider="anthropic",
            model="claude-sonnet",
        )
    )


def _worker(
    repository: AnkiCurationRepository,
    tmp_path: Path,
    runner: ControlledRunner,
    *,
    validator: ControlledValidator | None = None,
    worker_id: str = "worker-1",
    now: datetime | None = None,
) -> AnkiCurationWorker:
    pipeline = CurationPipeline(
        repository,
        StageArtifactStore(tmp_path / "artifacts"),
        runner,
        input_validator=validator or ControlledValidator(),
    )
    current = now or datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    return AnkiCurationWorker(
        repository,
        pipeline,
        worker_id=worker_id,
        lease_seconds=30,
        poll_seconds=0.01,
        now=lambda: current,
    )


def test_worker_advances_exactly_one_stage_and_releases_lease(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        job = _create_job(repository)
        runner = ControlledRunner()
        worker = _worker(repository, tmp_path, runner)

        assert await worker.run_once()

        advanced = repository.require_job(job.id)
        assert advanced.state is CurationState.BUILDING_SOURCE_INDEX
        assert advanced.lease_owner is None
        assert runner.calls == [CurationStage.PREFLIGHT]

    asyncio.run(scenario())


def test_expired_lease_is_reclaimed_without_resetting_completed_work(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        job = _create_job(repository)
        started = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
        abandoned = repository.claim_next_job(
            started,
            worker_id="abandoned-worker",
            lease_seconds=10,
        )
        assert abandoned is not None
        worker = _worker(
            repository,
            tmp_path,
            ControlledRunner(),
            worker_id="replacement-worker",
            now=started + timedelta(seconds=11),
        )

        assert await worker.run_once()
        assert (
            repository.require_job(job.id).state
            is CurationState.BUILDING_SOURCE_INDEX
        )

    asyncio.run(scenario())


def test_two_workers_racing_claim_only_one_job(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        _create_job(repository)
        runner = ControlledRunner()
        runner.entered = asyncio.Event()
        runner.release = asyncio.Event()
        first = _worker(
            repository,
            tmp_path,
            runner,
            worker_id="worker-1",
        )
        second = _worker(
            repository,
            tmp_path,
            runner,
            worker_id="worker-2",
        )

        first_task = asyncio.create_task(first.run_once())
        await runner.entered.wait()
        second_result = await second.run_once()
        runner.release.set()
        first_result = await first_task

        assert first_result
        assert not second_result
        assert runner.calls == [CurationStage.PREFLIGHT]

    asyncio.run(scenario())


def test_transient_stage_failure_retries_from_the_same_stage(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        job = _create_job(repository)
        runner = ControlledRunner()
        runner.error = LLMRequestError(
            "provider temporarily unavailable",
            source=DiagnosticSource.SERVICE,
        )
        first = _worker(repository, tmp_path, runner)

        assert await first.run_once()
        failed_once = repository.require_job(job.id)
        assert failed_once.state is CurationState.PREFLIGHT
        assert failed_once.available_at is not None

        later = datetime.fromisoformat(failed_once.available_at) + timedelta(
            seconds=1
        )
        retry = _worker(repository, tmp_path, runner, now=later)
        assert await retry.run_once()
        assert (
            repository.require_job(job.id).state
            is CurationState.BUILDING_SOURCE_INDEX
        )
        assert runner.calls == [
            CurationStage.PREFLIGHT,
            CurationStage.PREFLIGHT,
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "message",
    [
        "Selected source revision 12 changed after the job was queued",
        "Pinned semantic generation semantic-1 is no longer active",
    ],
)
def test_changed_pinned_input_blocks_job_with_actionable_error(
    repository: AnkiCurationRepository,
    tmp_path: Path,
    message: str,
) -> None:
    async def scenario() -> None:
        job = _create_job(repository)
        validator = ControlledValidator()
        validator.error = message
        worker = _worker(
            repository,
            tmp_path,
            ControlledRunner(),
            validator=validator,
        )

        assert await worker.run_once()

        blocked = repository.require_job(job.id)
        assert blocked.state is CurationState.FAILED
        assert blocked.error == message

    asyncio.run(scenario())


def test_cancellation_before_review_is_terminal_and_not_claimed(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        job = _create_job(repository)
        canceled = repository.cancel_job(job.id)
        worker = _worker(repository, tmp_path, ControlledRunner())

        assert canceled.state is CurationState.CANCELED
        assert not await worker.run_once()
        assert repository.require_job(job.id).state is CurationState.CANCELED

    asyncio.run(scenario())


def test_application_lifespan_starts_and_stops_curation_worker(
    tmp_path: Path,
) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path / "data",
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
            anki_enabled=True,
            dashboard_port=8787,
            anki_worker_poll_seconds=0.5,
        )
    )
    worker = app.state.anki_curation_worker

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert worker._task is not None
        assert not worker._task.done()

    assert worker._task is None
