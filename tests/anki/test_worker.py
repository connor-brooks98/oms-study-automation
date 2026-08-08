import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from oms_hub.anki.domain import (
    CreateCurationJob,
    CurationStage,
    CurationState,
    PipelineContractVersion,
)
from oms_hub.anki.pipeline import (
    CurationPipeline,
    PinnedInputChanged,
    StageArtifactStore,
    StageContext,
    StageProduct,
    pipeline_stages,
)
from oms_hub.anki.repository import AnkiCurationRepository, InvalidCurationTransition
from oms_hub.anki.semantic.store import SemanticSnapshotError
from oms_hub.anki.worker import AnkiCurationWorker, _is_retryable
from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.llm.domain import (
    DiagnosticSource,
    GeneratedText,
    LLMRequestError,
    ProviderName,
)
from oms_hub.llm.structured import StructuredOutputError
from oms_hub.models import LectureModel


class ControlledRunner:
    def __init__(self) -> None:
        self.calls: list[CurationStage] = []
        self.error: Exception | None = None
        self.blocking_error: str | None = None
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
            blocking_error=self.blocking_error,
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


def _create_job(
    repository: AnkiCurationRepository,
    *,
    pipeline_contract_version: PipelineContractVersion = PipelineContractVersion.RETRIEVAL_V4,
):
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
            pipeline_contract_version=pipeline_contract_version,
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
    max_stage_attempts: int = 3,
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
        max_stage_attempts=max_stage_attempts,
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
        assert repository.require_job(job.id).state is CurationState.BUILDING_SOURCE_INDEX

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


def test_expired_worker_cannot_commit_or_fail_reclaimed_stage(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        job = _create_job(repository)
        started = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
        claimed_by_a = repository.claim_next_job(
            started,
            worker_id="worker-a",
            lease_seconds=3,
        )
        assert claimed_by_a is not None
        current = [started]

        runner_a = ControlledRunner()
        runner_a.entered = asyncio.Event()
        runner_a.release = asyncio.Event()
        pipeline_a = CurationPipeline(
            repository,
            StageArtifactStore(tmp_path / "artifacts"),
            runner_a,
            input_validator=ControlledValidator(),
        )
        stale_run = asyncio.create_task(
            pipeline_a.run_stage(
                job.id,
                lease_owner="worker-a",
                lease_clock=lambda: current[0],
            )
        )
        await runner_a.entered.wait()

        current[0] = started + timedelta(seconds=4)
        claimed_by_b = repository.claim_next_job(
            current[0],
            worker_id="worker-b",
            lease_seconds=30,
        )
        assert claimed_by_b is not None
        assert claimed_by_b.lease_owner == "worker-b"

        pipeline_b = CurationPipeline(
            repository,
            StageArtifactStore(tmp_path / "artifacts"),
            ControlledRunner(),
            input_validator=ControlledValidator(),
        )
        result = await pipeline_b.run_stage(
            job.id,
            lease_owner="worker-b",
            lease_clock=lambda: current[0],
        )

        assert result is not None
        assert result.state is CurationState.BUILDING_SOURCE_INDEX
        assert repository.get_stage(job.id, CurationStage.PREFLIGHT).state == "complete"  # type: ignore[union-attr]

        runner_a.release.set()
        with pytest.raises(InvalidCurationTransition, match="not in preflight"):
            await stale_run

        assert repository.require_job(job.id).state is CurationState.BUILDING_SOURCE_INDEX
        assert repository.get_stage(job.id, CurationStage.PREFLIGHT).state == "complete"  # type: ignore[union-attr]
        assert len(repository.list_stage_artifacts(job.id)) == 1

    asyncio.run(scenario())


def test_expired_worker_cannot_start_after_reclaim(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        job = _create_job(repository)
        started = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
        assert (
            repository.claim_next_job(
                started,
                worker_id="worker-a",
                lease_seconds=3,
            )
            is not None
        )
        reclaimed_at = started + timedelta(seconds=4)
        assert (
            repository.claim_next_job(
                reclaimed_at,
                worker_id="worker-b",
                lease_seconds=30,
            )
            is not None
        )
        stale = CurationPipeline(
            repository,
            StageArtifactStore(tmp_path / "artifacts"),
            ControlledRunner(),
            input_validator=ControlledValidator(),
        )

        with pytest.raises(InvalidCurationTransition, match="no longer owns"):
            await stale.run_stage(
                job.id,
                lease_owner="worker-a",
                lease_clock=lambda: reclaimed_at,
            )

        assert repository.get_stage(job.id, CurationStage.PREFLIGHT) is None
        assert repository.require_job(job.id).lease_owner == "worker-b"

    asyncio.run(scenario())


def test_expired_lease_cannot_commit_fail_or_renew_before_reclaim(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        job = _create_job(repository)
        started = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
        assert (
            repository.claim_next_job(
                started,
                worker_id="worker-a",
                lease_seconds=3,
            )
            is not None
        )
        with pytest.raises(InvalidCurationTransition, match="not in building_source_index"):
            repository.fail_job(
                job.id,
                "worker-a",
                "wrong-state failure",
                expected_state=CurationState.BUILDING_SOURCE_INDEX,
                now=started,
            )
        runner = ControlledRunner()
        runner.entered = asyncio.Event()
        runner.release = asyncio.Event()
        current = [started]
        pipeline = CurationPipeline(
            repository,
            StageArtifactStore(tmp_path / "artifacts"),
            runner,
            input_validator=ControlledValidator(),
        )
        stale_run = asyncio.create_task(
            pipeline.run_stage(
                job.id,
                lease_owner="worker-a",
                lease_clock=lambda: current[0],
            )
        )
        await runner.entered.wait()

        current[0] = started + timedelta(seconds=4)
        assert not repository.renew_lease(
            job.id,
            "worker-a",
            current[0],
            lease_seconds=30,
        )
        with pytest.raises(InvalidCurationTransition, match="lease expired"):
            repository.fail_stage(
                job.id,
                CurationStage.PREFLIGHT,
                "stale worker failure",
                expected_state=CurationState.PREFLIGHT,
                lease_owner="worker-a",
                now=current[0],
            )
        with pytest.raises(InvalidCurationTransition, match="lease expired"):
            repository.defer_job(
                job.id,
                "worker-a",
                "stale worker retry",
                expected_state=CurationState.PREFLIGHT,
                available_at=current[0] + timedelta(seconds=5),
                now=current[0],
            )
        with pytest.raises(InvalidCurationTransition, match="lease expired"):
            repository.fail_job(
                job.id,
                "worker-a",
                "stale worker terminal failure",
                expected_state=CurationState.PREFLIGHT,
                now=current[0],
            )
        runner.release.set()
        with pytest.raises(InvalidCurationTransition, match="lease expired"):
            await stale_run

        stage = repository.get_stage(job.id, CurationStage.PREFLIGHT)
        assert stage is not None and stage.state == "running"
        assert repository.list_stage_artifacts(job.id) == []

    asyncio.run(scenario())


def test_worker_losing_lease_before_reclaim_leaves_job_reclaimable(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        job = _create_job(repository)
        started = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
        current = [started]
        runner = ControlledRunner()
        runner.entered = asyncio.Event()
        runner.release = asyncio.Event()
        stale = AnkiCurationWorker(
            repository,
            CurationPipeline(
                repository,
                StageArtifactStore(tmp_path / "artifacts"),
                runner,
                input_validator=ControlledValidator(),
            ),
            worker_id="worker-a",
            lease_seconds=3,
            poll_seconds=0.01,
            max_stage_attempts=3,
            now=lambda: current[0],
        )

        stale_run = asyncio.create_task(stale.run_once())
        await runner.entered.wait()
        current[0] = started + timedelta(seconds=4)
        runner.release.set()

        assert await stale_run
        after_expiry = repository.require_job(job.id)
        assert after_expiry.state is CurationState.PREFLIGHT
        assert after_expiry.error is None
        assert after_expiry.lease_owner is None
        stage = repository.get_stage(job.id, CurationStage.PREFLIGHT)
        assert stage is not None and stage.state == "running"
        assert repository.list_stage_artifacts(job.id) == []

        replacement = _worker(
            repository,
            tmp_path,
            ControlledRunner(),
            worker_id="worker-b",
            now=current[0],
        )
        assert await replacement.run_once()
        assert repository.require_job(job.id).state is CurationState.BUILDING_SOURCE_INDEX

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

        later = datetime.fromisoformat(failed_once.available_at) + timedelta(seconds=1)
        retry = _worker(repository, tmp_path, runner, now=later)
        assert await retry.run_once()
        assert repository.require_job(job.id).state is CurationState.BUILDING_SOURCE_INDEX
        assert runner.calls == [
            CurationStage.PREFLIGHT,
            CurationStage.PREFLIGHT,
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("stage", "state"),
    [
        (CurationStage.CARD_EVIDENCE_AUDIT, CurationState.CARD_AUDITING_EVIDENCE),
        (CurationStage.CARD_PREFILTER, CurationState.CARD_PREFILTERING),
        (CurationStage.CARD_FAST_CLASSIFY, CurationState.CARD_FAST_CLASSIFYING),
    ],
)
def test_v2_worker_claims_advances_and_retries_each_new_lifecycle_stage(
    repository: AnkiCurationRepository,
    tmp_path: Path,
    stage: CurationStage,
    state: CurationState,
) -> None:
    async def scenario() -> None:
        job = _create_job(
            repository,
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
        )
        runner = ControlledRunner()
        worker = _worker(repository, tmp_path, runner, max_stage_attempts=1)
        definitions = pipeline_stages(PipelineContractVersion.CARD_CENTRIC_V2)
        definition = next(item for item in definitions if item.stage is stage)

        for prior in definitions:
            if prior.state is state:
                break
            assert await worker.run_once()
            assert runner.calls[-1] is prior.stage

        assert repository.require_job(job.id).state is state
        runner.error = LLMRequestError(
            "provider temporarily unavailable",
            source=DiagnosticSource.SERVICE,
        )
        assert await worker.run_once()
        assert runner.calls[-1] is stage
        assert repository.require_job(job.id).state is CurationState.FAILED

        retried = repository.retry_job(job.id)
        assert retried.state is state
        assert await worker.run_once()
        assert runner.calls[-1] is stage
        assert repository.require_job(job.id).state is definition.next_state

    asyncio.run(scenario())


def test_malformed_structured_output_retries_from_the_same_stage(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        job = _create_job(repository)
        runner = ControlledRunner()
        runner.error = StructuredOutputError(
            "structured output failed JSON schema validation",
            raw_text='{"status":',
            generation=GeneratedText(
                text='{"status":',
                provider=ProviderName.OPENAI,
                model="gpt-5.2",
                request_id="request-malformed",
                input_tokens=20,
                output_tokens=4,
                cost_microusd=2,
            ),
        )
        worker = _worker(repository, tmp_path, runner)

        assert await worker.run_once()

        retryable = repository.require_job(job.id)
        assert retryable.state is CurationState.PREFLIGHT
        assert retryable.available_at is not None

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


def test_returned_terminal_failure_logs_persisted_error_once(
    repository: AnkiCurationRepository,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        job = _create_job(repository)
        runner = ControlledRunner()
        runner.blocking_error = (
            "Card-centric reconciliation failed: A6: selected card count is too low"
        )
        worker = _worker(repository, tmp_path, runner)

        with caplog.at_level(logging.ERROR, logger="oms_hub.anki.worker"):
            assert await worker.run_once()

        current = repository.require_job(job.id)
        assert current.state is CurationState.FAILED
        assert current.error == runner.blocking_error
        messages = [record.getMessage() for record in caplog.records]
        assert messages == [f"Anki curation job {job.id} stopped: {runner.blocking_error}"]

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


def test_sqlite_busy_errors_are_retryable() -> None:
    import sqlite3

    from sqlalchemy.exc import OperationalError

    orig = sqlite3.OperationalError("database is locked")
    orig.sqlite_errorcode = sqlite3.SQLITE_BUSY
    error = OperationalError("stmt", {}, orig)

    assert _is_retryable(error) is True


def test_semantic_snapshot_errors_are_retryable() -> None:
    assert _is_retryable(SemanticSnapshotError("snapshot checksum mismatch")) is True


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
