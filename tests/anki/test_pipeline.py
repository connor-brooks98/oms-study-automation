import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from oms_hub.anki.domain import (
    CreateCurationJob,
    CurationStage,
    CurationState,
    PipelineContractVersion,
    StageArtifact,
)
from oms_hub.anki.pipeline import (
    CARD_CENTRIC_V3_STAGES,
    PIPELINE_STAGES,
    CurationPipeline,
    PinnedInputChanged,
    StageArtifactStore,
    StageContext,
    StageProduct,
    UnsupportedPipelineContract,
    _stage_input_hash,
    pipeline_stages,
    stage_definition,
)
from oms_hub.anki.repository import AnkiCurationRepository
from oms_hub.anki.stages import (
    PinnedCurationInputValidator,
    revision_fingerprint,
)
from oms_hub.db import Database
from oms_hub.ingestion.domain import StudyRevision, UploadKind
from oms_hub.models import LectureModel
from oms_hub.study_generation.domain import OutlineRecord


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[CurationStage] = []

    async def run(self, context: StageContext) -> StageProduct:
        self.calls.append(context.stage)
        return StageProduct(
            kind=f"{context.stage.value}_result",
            payload={
                "stage": context.stage.value,
                "prior_artifact_count": len(context.prior_artifacts),
            },
            metadata={"runner": "fake"},
        )


class MutableInputValidator:
    def __init__(self) -> None:
        self.error: str | None = None

    def validate(self, job_id: UUID) -> None:
        del job_id
        if self.error is not None:
            raise PinnedInputChanged(self.error)


def test_v3_graph_is_frozen_but_pipeline_lookup_fails_closed() -> None:
    assert len(CARD_CENTRIC_V3_STAGES) == 13
    assert CARD_CENTRIC_V3_STAGES[0].stage.value == "v3_r0_preflight"
    assert CARD_CENTRIC_V3_STAGES[-1].stage.value == "v3_r12_apply"
    with pytest.raises(UnsupportedPipelineContract, match="no mutation"):
        pipeline_stages(PipelineContractVersion.CARD_CENTRIC_V3)


def test_stage_artifact_store_rejects_document_provenance_mismatch(tmp_path: Path) -> None:
    store = StageArtifactStore(tmp_path / "artifacts")
    job_id = UUID("d1b4bdc2-7d4f-44b4-a2a8-a926fcba6e19")
    artifact = store.write(
        job_id,
        CurationStage.PREFLIGHT,
        StageProduct(kind="preflight_report", payload={"ready": True}),
        input_sha256="a" * 64,
        model_config_sha256="b" * 64,
    )
    path = tmp_path / "artifacts" / artifact.relative_path
    document = json.loads(path.read_text(encoding="utf-8"))
    document["job_id"] = str(UUID("a1b4bdc2-7d4f-44b4-a2a8-a926fcba6e19"))
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    tampered = replace(artifact, content_sha256=hashlib.sha256(encoded.encode()).hexdigest())
    path.write_text(encoded, encoding="utf-8")

    with pytest.raises(PinnedInputChanged, match="provenance"):
        store.read(tampered)


def test_stage_artifact_store_rejects_nonlegacy_model_configuration_mismatch(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    store = StageArtifactStore(tmp_path / "artifacts")
    job = _job(repository)
    artifact = store.write(
        job.id,
        CurationStage.PREFLIGHT,
        StageProduct(kind="preflight_report", payload={"ready": True}),
        input_sha256="a" * 64,
        model_config_sha256="a" * 64,
    )

    with pytest.raises(PinnedInputChanged, match="model configuration"):
        store.read(artifact, job=replace(job, model_config_sha256="b" * 64))


def test_stage_artifact_store_rejects_v1_document_without_migration_sentinel(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    store = StageArtifactStore(tmp_path / "artifacts")
    job = _job(repository)
    document = {
        "artifact_version": 1,
        "job_id": str(job.id),
        "stage": CurationStage.PREFLIGHT.value,
        "kind": "preflight_report",
        "payload": {"ready": True},
        "metadata": {"source": "legacy"},
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    content_sha256 = hashlib.sha256(encoded).hexdigest()
    artifact = StageArtifact(
        artifact_id=f"preflight:{content_sha256}",
        stage=CurationStage.PREFLIGHT,
        kind="preflight_report",
        relative_path=f"{job.id}/preflight/{content_sha256}.json",
        input_sha256="a" * 64,
        content_sha256=content_sha256,
        model_config_sha256=job.model_config_sha256,
        metadata={"source": "legacy"},
    )
    path = tmp_path / "artifacts" / artifact.relative_path
    path.parent.mkdir(parents=True)
    path.write_bytes(encoded)

    with pytest.raises(PinnedInputChanged, match="invalid provenance"):
        store.read(artifact, job=job)


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
    repository._test_database = database  # type: ignore[attr-defined]
    repository._test_lecture_id = lecture_id  # type: ignore[attr-defined]
    yield repository
    database.close()


def _job(repository: AnkiCurationRepository):
    lecture_id = repository._test_lecture_id  # type: ignore[attr-defined]
    return repository.create_job(
        CreateCurationJob(
            lecture_id=lecture_id,
            block_id="heme-block",
            source_revision_ids=(11, 12),
            deck_allowlist=("AnKing Step Deck",),
            tag_allowlist=("#Pathoma",),
            instruction_text="Focus on lecture objectives.",
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


def _claimed_job(repository: AnkiCurationRepository):
    job = _job(repository)
    claimed = repository.claim_next_job(
        datetime(2026, 7, 30, 12, 0, tzinfo=UTC),
        worker_id="worker-1",
        lease_seconds=60,
    )
    assert claimed is not None
    assert claimed.id == job.id
    repository.release_lease(job.id, "worker-1")
    return claimed


def _claimed_card_job(repository: AnkiCurationRepository):
    card = repository.create_job(
        CreateCurationJob(
            lecture_id=repository._test_lecture_id,  # type: ignore[attr-defined]
            block_id="heme-block",
            source_revision_ids=(11, 12),
            deck_allowlist=("AnKing Step Deck",),
            tag_allowlist=("#Pathoma",),
            instruction_text="Focus on lecture objectives.",
            target_deck="OMS::Heme::Lecture 4",
            target_tag="AnkiHub_Optional::LMU_OMS_II::Heme::Lecture_4",
            index_snapshot_id="snapshot-1",
            lcl_prompt_version="lcl-v1",
            judgment_rubric_version="judgment-v1",
            gap_prompt_version="gap-v1",
            provider="anthropic",
            model="claude-sonnet",
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V1,
        )
    )
    claimed = repository.transition(
        card.id,
        CurationState.QUEUED,
        CurationState.PREFLIGHT,
    )
    assert claimed.id == card.id
    return claimed


def test_convergence_passes_are_restart_safe_and_precede_audit() -> None:
    audit = stage_definition(CurationState.AUDITING_CANDIDATES)
    recompute = stage_definition(CurationState.RECOMPUTING_COVERAGE)
    after_pass_two = stage_definition(CurationState.JUDGING_PASS_2)
    pass_three = stage_definition(CurationState.CONVERGING_PASS_3)
    pass_four = stage_definition(CurationState.CONVERGING_PASS_4)
    pass_five = stage_definition(CurationState.CONVERGING_PASS_5)

    assert after_pass_two is not None
    assert after_pass_two.next_state is CurationState.CONVERGING_PASS_3
    assert pass_three is not None
    assert pass_three.stage is CurationStage.CONVERGENCE_PASS_3
    assert pass_three.next_state is CurationState.CONVERGING_PASS_4
    assert pass_four is not None
    assert pass_four.stage is CurationStage.CONVERGENCE_PASS_4
    assert pass_four.next_state is CurationState.CONVERGING_PASS_5
    assert pass_five is not None
    assert pass_five.stage is CurationStage.CONVERGENCE_PASS_5
    assert pass_five.next_state is CurationState.AUDITING_CANDIDATES
    assert audit is not None
    assert audit.stage is CurationStage.CARD_AUDIT
    assert audit.next_state is CurationState.RECOMPUTING_COVERAGE
    assert recompute is not None
    assert recompute.stage is CurationStage.COVERAGE_RECOMPUTE
    assert recompute.next_state is CurationState.DEDUPING


def test_reconciliation_is_restart_safe_between_gaps_and_review() -> None:
    gaps = stage_definition(CurationState.GENERATING_GAPS)
    reconciliation = stage_definition(CurationState.RECONCILING)

    assert gaps is not None
    assert gaps.stage is CurationStage.GAPS
    assert gaps.next_state is CurationState.RECONCILING
    assert reconciliation is not None
    assert reconciliation.stage is CurationStage.RECONCILIATION
    assert reconciliation.next_state is CurationState.READY_FOR_REVIEW


def test_blocking_stage_commits_report_and_fails_job(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    class BlockingRunner(RecordingRunner):
        async def run(self, context: StageContext) -> StageProduct:
            self.calls.append(context.stage)
            if context.stage is CurationStage.RECONCILIATION:
                return StageProduct(
                    kind="reconciliation_report_v2",
                    payload={
                        "can_render_envelope": False,
                        "failed": [
                            {
                                "assertion_id": "A2",
                                "message": "Missing facts do not reconcile",
                            }
                        ],
                    },
                    blocking_error="Reconciliation failed: A2",
                )
            return StageProduct(
                kind=f"{context.stage.value}_result",
                payload={"stage": context.stage.value},
            )

    async def scenario() -> None:
        job = _claimed_job(repository)
        artifacts = StageArtifactStore(tmp_path / "artifacts")
        pipeline = CurationPipeline(
            repository,
            artifacts,
            BlockingRunner(),
            input_validator=MutableInputValidator(),
        )

        while repository.require_job(job.id).state is not CurationState.FAILED:
            result = await pipeline.run_stage(job.id)
            assert result is not None

        failed = repository.require_job(job.id)
        reconciliation_artifact = repository.list_stage_artifacts(job.id)[-1]
        report = artifacts.read(reconciliation_artifact)
        stage = repository.get_stage(job.id, CurationStage.RECONCILIATION)

        assert failed.error == "Reconciliation failed: A2"
        assert reconciliation_artifact.stage is CurationStage.RECONCILIATION
        assert report["failed"][0]["assertion_id"] == "A2"
        assert stage is not None
        assert stage.state == "failed"

        retried = repository.retry_job(job.id)

        assert retried.state is CurationState.RECONCILING
        assert retried.error is None

        rerun = await pipeline.run_stage(job.id)

        assert rerun is not None
        assert rerun.stage is CurationStage.RECONCILIATION
        assert rerun.state is CurationState.FAILED
        reconciliation_artifacts = [
            item
            for item in repository.list_stage_artifacts(job.id)
            if item.stage is CurationStage.RECONCILIATION
        ]
        assert len(reconciliation_artifacts) == 1
        retried_stage = repository.get_stage(job.id, CurationStage.RECONCILIATION)
        assert retried_stage is not None
        assert retried_stage.attempt_count == 2

    asyncio.run(scenario())


def test_pending_overflow_reconciliation_reaches_review_without_envelope_authority(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    class PendingOverflowRunner(RecordingRunner):
        async def run(self, context: StageContext) -> StageProduct:
            self.calls.append(context.stage)
            if context.stage is CurationStage.RECONCILIATION:
                return StageProduct(
                    kind="card_centric_reconciliation",
                    payload={
                        "can_render_envelope": False,
                        "failed": [
                            {
                                "assertion_id": "selection_cap",
                                "message": "Selection requires a signed acknowledgement",
                            }
                        ],
                    },
                )
            return StageProduct(
                kind=f"{context.stage.value}_result",
                payload={"stage": context.stage.value},
            )

    async def scenario() -> None:
        job = _claimed_card_job(repository)
        pipeline = CurationPipeline(
            repository,
            StageArtifactStore(tmp_path / "artifacts"),
            PendingOverflowRunner(),
            input_validator=MutableInputValidator(),
        )

        while repository.require_job(job.id).state is not CurationState.READY_FOR_REVIEW:
            result = await pipeline.run_stage(job.id)
            assert result is not None

        reconciled = repository.get_stage(job.id, CurationStage.RECONCILIATION)

        assert reconciled is not None
        assert reconciled.state == "complete"
        assert repository.require_job(job.id).state is CurationState.READY_FOR_REVIEW

    asyncio.run(scenario())


def test_complete_pipeline_commits_one_immutable_artifact_per_stage(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        job = _claimed_job(repository)
        runner = RecordingRunner()
        pipeline = CurationPipeline(
            repository,
            StageArtifactStore(tmp_path / "artifacts"),
            runner,
            input_validator=MutableInputValidator(),
        )

        for expected in PIPELINE_STAGES:
            result = await pipeline.run_stage(job.id)
            assert result is not None
            assert result.stage is expected.stage
        assert await pipeline.run_stage(job.id) is None

        completed = repository.require_job(job.id)
        artifacts = repository.list_stage_artifacts(job.id)
        assert completed.state is CurationState.READY_FOR_REVIEW
        assert runner.calls == [item.stage for item in PIPELINE_STAGES]
        assert len(artifacts) == len(PIPELINE_STAGES)
        assert len({artifact.artifact_id for artifact in artifacts}) == len(artifacts)
        assert all(
            (tmp_path / "artifacts" / artifact.relative_path).is_file() for artifact in artifacts
        )
        assert all(
            artifact.input_sha256
            and artifact.content_sha256
            and len(artifact.input_sha256) == 64
            and len(artifact.content_sha256) == 64
            for artifact in artifacts
        )

    asyncio.run(scenario())


def test_restart_at_every_stage_never_reruns_a_committed_stage(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        job = _claimed_job(repository)
        runner = RecordingRunner()
        validator = MutableInputValidator()

        for index, expected in enumerate(PIPELINE_STAGES, start=1):
            restarted = CurationPipeline(
                repository,
                StageArtifactStore(tmp_path / "artifacts"),
                runner,
                input_validator=validator,
            )
            result = await restarted.run_stage(job.id)
            assert result is not None
            assert result.stage is expected.stage
            assert len(repository.list_stage_artifacts(job.id)) == index

        assert runner.calls == [item.stage for item in PIPELINE_STAGES]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "message",
    [
        "Selected source revision 12 changed after the job was queued",
        "Pinned semantic generation semantic-1 is no longer active",
    ],
)
def test_pinned_input_change_fails_the_current_stage_without_advancing(
    repository: AnkiCurationRepository,
    tmp_path: Path,
    message: str,
) -> None:
    async def scenario() -> None:
        job = _claimed_job(repository)
        runner = RecordingRunner()
        validator = MutableInputValidator()
        validator.error = message
        pipeline = CurationPipeline(
            repository,
            StageArtifactStore(tmp_path / "artifacts"),
            runner,
            input_validator=validator,
        )

        with pytest.raises(PinnedInputChanged, match=message):
            await pipeline.run_stage(job.id)

        assert repository.require_job(job.id).state is CurationState.PREFLIGHT
        assert repository.list_stage_artifacts(job.id) == []
        assert runner.calls == []

    asyncio.run(scenario())


def test_production_input_validator_detects_revision_and_semantic_drift(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    source = tmp_path / "lecture.pptx"
    source.write_bytes(b"immutable source")
    revision = StudyRevision(
        id=11,
        upload_item_id="upload-11",
        lecture_id=repository._test_lecture_id,  # type: ignore[attr-defined]
        kind=UploadKind.SLIDES,
        source_sha256="a" * 64,
        immutable_source_path=source,
        derived_sha256=None,
        immutable_derived_path=None,
        canonical_source_path=None,
        canonical_derived_path=None,
        icloud_path=None,
        prompt_sha256=None,
        state="approved",
        current=True,
    )
    summary_path = tmp_path / "outline.pdf"
    summary_path.write_bytes(b"immutable summary")
    summary_sha = hashlib.sha256(summary_path.read_bytes()).hexdigest()
    outline = OutlineRecord(
        id=9,
        lecture_id=repository._test_lecture_id,  # type: ignore[attr-defined]
        job_id="outline-job",
        path=summary_path,
        sha256=summary_sha,
        current=True,
    )

    class Revisions:
        current = revision

        def get_study_revision(self, revision_id: int) -> StudyRevision:
            assert revision_id == 11
            return self.current

        def has_imported_derived_audit(self, revision_id: int) -> bool:
            assert revision_id == 11
            return False

        def imported_derived_audit_matches(self, selected: StudyRevision) -> bool:
            raise AssertionError(f"unexpected imported-derived audit check: {selected.id}")

    class Companion:
        def snapshot_id(self) -> str:
            return "companion-1"

    class Outlines:
        def outline(self, outline_id: int) -> OutlineRecord | None:
            assert outline_id == 9
            return outline

    class Semantic:
        generation = UUID("4438eabc-3da1-4d6d-a6af-2302de092f8e")

        def load(self, **_: object):
            return SimpleNamespace(manifest=SimpleNamespace(generation=self.generation))

    lecture_id = repository._test_lecture_id  # type: ignore[attr-defined]
    job = repository.create_job(
        CreateCurationJob(
            lecture_id=lecture_id,
            block_id=None,
            source_revision_ids=(11,),
            deck_allowlist=("AnKing Step Deck",),
            tag_allowlist=(),
            instruction_text="",
            target_deck="OMS::Heme",
            target_tag="AnkiHub_Optional::LMU_OMS_II::Heme",
            index_snapshot_id="companion-1",
            lcl_prompt_version="lcl-v1",
            judgment_rubric_version="judgment-v1",
            gap_prompt_version="gap-v1",
            provider="anthropic",
            model="claude-sonnet",
            source_revision_hashes={11: revision_fingerprint(revision)},
            semantic_generation=str(Semantic.generation),
            companion_generation="companion-1",
            summary_outline_id=9,
            summary_outline_sha256=summary_sha,
        )
    )
    revisions = Revisions()
    semantic = Semantic()
    validator = PinnedCurationInputValidator(
        repository,
        revisions,  # type: ignore[arg-type]
        Companion(),  # type: ignore[arg-type]
        semantic,  # type: ignore[arg-type]
        lambda _: (_ for _ in ()).throw(
            AssertionError("source index should not be read before pinning")
        ),
        outlines=Outlines(),
        semantic_model="voyage-4-large",
        semantic_dimensions=1024,
    )

    validator.validate(job.id)
    summary_path.write_bytes(b"mutated summary")
    with pytest.raises(PinnedInputChanged, match="summary changed"):
        validator.validate(job.id)
    summary_path.write_bytes(b"immutable summary")
    revisions.current = replace(revision, source_sha256="b" * 64)
    with pytest.raises(PinnedInputChanged, match="revision 11 changed"):
        validator.validate(job.id)
    revisions.current = revision
    semantic.generation = UUID("a68ee9b9-503a-4688-8514-139f83e82d28")
    with pytest.raises(PinnedInputChanged, match="semantic generation"):
        validator.validate(job.id)


def test_contract_version_controls_graph_and_stage_hash(
    repository: AnkiCurationRepository,
) -> None:
    job = _job(repository)
    assert stage_definition(CurationState.PREFLIGHT) == PIPELINE_STAGES[0]
    assert PIPELINE_STAGES[0].next_state is CurationState.BUILDING_SOURCE_INDEX
    card_job = replace(job, pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V1)
    card_stages = pipeline_stages(card_job.pipeline_contract_version)
    assert card_stages[0].stage is CurationStage.PREFLIGHT
    assert card_stages[1].next_state is CurationState.CARD_BUILDING_LEDGER
    original = _stage_input_hash(job, CurationStage.PREFLIGHT, ())
    assert original != _stage_input_hash(
        replace(job, model_config_sha256="f" * 64), CurationStage.PREFLIGHT, ()
    )
    assert original != _stage_input_hash(card_job, CurationStage.PREFLIGHT, ())
    v2_stages = pipeline_stages(PipelineContractVersion.CARD_CENTRIC_V2)
    assert len(v2_stages) == 14
    assert [stage.stage for stage in v2_stages[2:7]] == [
        CurationStage.CARD_LEDGER,
        CurationStage.CARD_EVIDENCE_AUDIT,
        CurationStage.CARD_TAG_SCOPE,
        CurationStage.CARD_PREFILTER,
        CurationStage.CARD_FAST_CLASSIFY,
    ]
    assert v2_stages[-1].next_state is CurationState.READY_FOR_REVIEW


def test_card_centric_graph_is_version_isolated_and_reaches_review(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        card_job = _claimed_card_job(repository)
        assert card_job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V1
        assert card_job.resolved_model_config.classify_s4.thinking_mode == "disabled"
        pipeline = CurationPipeline(
            repository,
            StageArtifactStore(tmp_path / "artifacts"),
            RecordingRunner(),
            input_validator=MutableInputValidator(),
        )
        for expected in pipeline_stages(PipelineContractVersion.CARD_CENTRIC_V1):
            result = await pipeline.run_stage(card_job.id)
            assert result is not None
            assert result.stage is expected.stage
        completed = repository.require_job(card_job.id)
        assert completed.state is CurationState.READY_FOR_REVIEW
        assert [artifact.stage for artifact in repository.list_stage_artifacts(card_job.id)] == [
            definition.stage
            for definition in pipeline_stages(PipelineContractVersion.CARD_CENTRIC_V1)
        ]

    asyncio.run(scenario())
