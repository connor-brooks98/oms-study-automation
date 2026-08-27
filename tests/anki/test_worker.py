import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from oms_hub.anki.card_centric import (
    CardCentricLedgerService,
    _ledger_repair_instruction,
    build_source_index,
    s2_generation_parameters,
)
from oms_hub.anki.domain import (
    CreateCurationJob,
    CurationStage,
    CurationState,
    PipelineContractVersion,
    SourceKind,
    StageUsage,
)
from oms_hub.anki.models import AnkiEnvelopeModel, AnkiEnvelopeOperationModel
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
from oms_hub.anki.sources import SourcePassage
from oms_hub.anki.worker import AnkiCurationWorker, _is_retryable
from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.llm.domain import (
    DiagnosticSource,
    GeneratedText,
    GenerationOptions,
    LLMRequestError,
    ProviderName,
)
from oms_hub.llm.structured import StructuredOutputError, StructuredTextService
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


class LedgerServiceRunner:
    """Run the production S2 service while keeping the worker fixture local."""

    def __init__(
        self,
        responses: list[str],
        *,
        request_ids: list[str] | None = None,
        response_model: str | None = None,
    ) -> None:
        self.responses = iter(responses)
        self.request_ids = iter(request_ids or ["ledger-request"] * len(responses))
        self.response_model = response_model

    async def run(self, context: StageContext) -> StageProduct:
        assert context.stage is CurationStage.CARD_LEDGER
        assert context.record_card_ledger_attempt is not None

        responses = self.responses
        request_ids = self.request_ids
        response_model = self.response_model

        class Generator:
            def generate_text(
                self,
                instruction: str,
                input_text: str,
                *,
                output_schema: dict[str, object],
                provider: ProviderName,
                model: str,
                options: GenerationOptions,
            ) -> GeneratedText:
                del instruction, input_text, output_schema, options
                return GeneratedText(
                    text=next(responses),
                    provider=provider,
                    model=response_model or model,
                    request_id=next(request_ids),
                    input_tokens=10,
                    output_tokens=5,
                    cost_microusd=1,
                )

        source = build_source_index(
            [
                SourcePassage.create(
                    revision_id=1,
                    lecture_id=context.job.lecture_id,
                    artifact_id="outline:1",
                    source_kind=SourceKind.SUMMARY,
                    locator="summary:1",
                    source_id="SUM:1:CORE:01",
                    summary_section="core",
                    text="Heme synthesis summary.",
                )
            ],
            snapshot_id="worker-s2-source",
            source_revision_hashes={1: "a" * 64},
        )
        route = context.job.resolved_model_config.ledger_s2
        result = CardCentricLedgerService(
            StructuredTextService(Generator()), "S2"
        ).generate(
            source_index=source,
            provider=ProviderName(route.provider),
            model=route.model,
            record_attempt=context.record_card_ledger_attempt,
        )
        return StageProduct(
            kind="card_centric_ledger",
            payload={"ledger": result.ledger.model_dump(mode="json")},
            usage=StageUsage(
                result.request_id,
                result.input_tokens,
                result.output_tokens,
                result.cost_microusd,
            ),
        )


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


def test_s2_importance_conflict_retries_boundedly_and_manual_retry_keeps_attempt_evidence(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        job = _create_job(
            repository,
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
        )
        current = [datetime(2026, 8, 11, 12, 0, tzinfo=UTC)]
        pipeline = CurationPipeline(
            repository,
            StageArtifactStore(tmp_path / "artifacts"),
            ControlledRunner(),
            input_validator=ControlledValidator(),
        )
        worker = AnkiCurationWorker(
            repository,
            pipeline,
            worker_id="worker-s2",
            lease_seconds=30,
            poll_seconds=0.01,
            max_stage_attempts=2,
            now=lambda: current[0],
        )
        # Reach S2 through the real pipeline state machine, then exercise the
        # production S2 service with the original CardConcept importance error.
        assert await worker.run_once()
        assert await worker.run_once()
        assert repository.require_job(job.id).state is CurationState.CARD_BUILDING_LEDGER

        valid = (
            '{"lecture_entity_count":1,"concepts":[{"concept_id":"C01",'
            '"canonical_statement":"fact","primary_entity":"fact",'
            '"aliases":[],"depth":"deep","emphasis_flag":false,'
            '"importance":"high","fact_descriptions":["fact"],'
            '"forbidden_cloze_targets_by_fact":[[]]}]}'
        )
        invalid = valid.replace('"importance":"high"', '"importance":"low"')
        pipeline.runner = LedgerServiceRunner([invalid, invalid])
        assert await worker.run_once()
        deferred = repository.require_job(job.id)
        assert deferred.state is CurationState.CARD_BUILDING_LEDGER
        assert [
            (row["stage_attempt"], row["call_index"], row["outcome"])
            for row in repository.list_card_ledger_attempts(job.id)
        ] == [(1, 1, "validation_failed"), (1, 2, "validation_failed")]
        assert not [
            artifact
            for artifact in repository.list_stage_artifacts(job.id)
            if artifact.stage is CurationStage.CARD_LEDGER
        ]

        current[0] += timedelta(seconds=6)
        pipeline.runner = LedgerServiceRunner([invalid, invalid])
        assert await worker.run_once()
        assert repository.require_job(job.id).state is CurationState.FAILED
        assert [row["stage_attempt"] for row in repository.list_card_ledger_attempts(job.id)] == [
            1,
            1,
            2,
            2,
        ]

        assert repository.retry_job(job.id).state is CurationState.CARD_BUILDING_LEDGER
        pipeline.runner = LedgerServiceRunner([valid])
        assert await worker.run_once()
        assert repository.require_job(job.id).state is CurationState.CARD_AUDITING_EVIDENCE
        assert [
            (row["stage_attempt"], row["call_index"], row["outcome"])
            for row in repository.list_card_ledger_attempts(job.id)
        ] == [
            (1, 1, "validation_failed"),
            (1, 2, "validation_failed"),
            (2, 1, "validation_failed"),
            (2, 2, "validation_failed"),
            (3, 1, "accepted"),
        ]

    asyncio.run(scenario())


def test_s2_invalid_primary_then_valid_repair_commits_one_causal_stage_attempt(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        job = _create_job(
            repository,
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
        )
        current = [datetime(2026, 8, 11, 12, 0, tzinfo=UTC)]
        pipeline = CurationPipeline(
            repository,
            StageArtifactStore(tmp_path / "artifacts"),
            ControlledRunner(),
            input_validator=ControlledValidator(),
        )
        worker = AnkiCurationWorker(
            repository,
            pipeline,
            worker_id="worker-s2",
            lease_seconds=30,
            poll_seconds=0.01,
            max_stage_attempts=2,
            now=lambda: current[0],
        )
        assert await worker.run_once()
        assert await worker.run_once()
        assert repository.require_job(job.id).state is CurationState.CARD_BUILDING_LEDGER

        valid = (
            '{"lecture_entity_count":1,"concepts":[{"concept_id":"C01",'
            '"canonical_statement":"fact","primary_entity":"fact",'
            '"aliases":[],"depth":"deep","emphasis_flag":false,'
            '"importance":"high","fact_descriptions":["fact"],'
            '"forbidden_cloze_targets_by_fact":[[]]}]}'
        )
        invalid = valid.replace('"importance":"high"', '"importance":"low"')
        repair = json.dumps(
            {"replacements": [json.loads(valid)["concepts"][0]], "additions": []},
            separators=(",", ":"),
        )
        pipeline.runner = LedgerServiceRunner(
            [invalid, repair],
            request_ids=["primary-request-id", "repair-request-id"],
            response_model="claude-sonnet-5-2026-08-01",
        )

        assert await worker.run_once()
        advanced = repository.require_job(job.id)
        assert advanced.state is CurationState.CARD_AUDITING_EVIDENCE
        assert advanced.apply_state.value == "pending"
        stage = repository.get_stage(job.id, CurationStage.CARD_LEDGER)
        assert stage is not None
        assert stage.state == "complete"
        assert stage.attempt_count == 1
        expected_request_id = "card_ledger:" + hashlib.sha256(
            json.dumps(
                ("primary-request-id", "repair-request-id"), separators=(",", ":")
            ).encode()
        ).hexdigest()[:24]
        assert (
            stage.request_id,
            stage.input_tokens,
            stage.output_tokens,
            stage.cost_microusd,
        ) == (expected_request_id, 20, 10, 2)

        requested_provider = ProviderName.ANTHROPIC
        requested_model = "claude-sonnet"
        expected_parameters = s2_generation_parameters(requested_provider, requested_model)
        expected_parameters_sha256 = hashlib.sha256(
            json.dumps(expected_parameters, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        rows = repository.list_card_ledger_attempts(job.id)
        assert [
            (
                row["stage_attempt"],
                row["call_index"],
                row["kind"],
                row["outcome"],
                row["request_id"],
            )
            for row in rows
        ] == [
            (1, 1, "primary", "validation_failed", "primary-request-id"),
            (1, 2, "repair", "accepted", "repair-request-id"),
        ]
        assert [(row["provider"], row["model"]) for row in rows] == [
            ("anthropic", requested_model),
            ("anthropic", requested_model),
        ]
        assert [row["generation_parameters"] for row in rows] == [
            expected_parameters,
            expected_parameters,
        ]
        assert [row["generation_parameters_sha256"] for row in rows] == [
            expected_parameters_sha256,
            expected_parameters_sha256,
        ]
        assert [row["instruction_sha256"] for row in rows] == [
            hashlib.sha256(b"S2").hexdigest(),
            hashlib.sha256(_ledger_repair_instruction("S2").encode()).hexdigest(),
        ]

        artifacts = repository.list_stage_artifacts(job.id)
        ledger_artifacts = [
            artifact for artifact in artifacts if artifact.stage is CurationStage.CARD_LEDGER
        ]
        assert len(ledger_artifacts) == 1
        artifact = ledger_artifacts[0]
        assert artifact.kind == "card_centric_ledger"
        assert artifact.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V2
        assert artifact.model_config_sha256 == advanced.model_config_sha256
        assert artifact.relative_path.startswith(f"{job.id}/card_ledger/")
        with repository.database.session() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AnkiEnvelopeModel)
                    .where(AnkiEnvelopeModel.job_id == str(job.id))
                )
                == 0
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AnkiEnvelopeOperationModel)
                    .join(AnkiEnvelopeModel)
                    .where(AnkiEnvelopeModel.job_id == str(job.id))
                )
                == 0
            )

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
