"""P4-B replay, durability, and lease fault matrix for card_centric_v2."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from oms_hub.anki.card_centric import build_source_index
from oms_hub.anki.card_centric_contracts import (
    CardConcept,
    CardConceptLedger,
    CardGapBatch,
    CardGapOutput,
    CardRecord,
    ClassifierResult,
    ClassifierTelemetry,
    FastClassificationResult,
    TagScopeResult,
)
from oms_hub.anki.correction_contracts import OrphanArtifactAdoptionEvidence
from oms_hub.anki.domain import (
    CreateCurationJob,
    CurationStage,
    CurationState,
    PipelineContractVersion,
    SourceKind,
)
from oms_hub.anki.models import AnkiReviewedReconciliationModel
from oms_hub.anki.pipeline import (
    CurationPipeline,
    PinnedInputChanged,
    StageArtifactStore,
    StageProduct,
)
from oms_hub.anki.prompts import AnkiPromptLibrary, StaticPromptSynchronizer
from oms_hub.anki.repository import AnkiCurationRepository, InvalidCurationTransition
from oms_hub.anki.semantic.domain import SemanticHit
from oms_hub.anki.sources import SourcePassage
from oms_hub.anki.stages import CurationServicesRunner
from oms_hub.db import Database
from oms_hub.llm.structured import StructuredJSONResult
from oms_hub.models import LectureModel
from tests.anki.fixtures.card_centric_v2_faults import GenerationSwitchingSemantic


class _ReadyRuntime:
    async def ensure_running(self) -> SimpleNamespace:
        return SimpleNamespace(
            reachable=True,
            ankiconnect_version=6,
            active_profile="fixture",
            collection_accessible=True,
            sync_available=True,
            blocking_reason=None,
        )


class _RequiredSources:
    def extract(self, *_args: object, **_kwargs: object) -> tuple[SourcePassage, ...]:
        return tuple(
            SourcePassage.create(
                revision_id=index,
                lecture_id=12,
                artifact_id=f"fixture-{kind.value}",
                source_kind=kind,
                locator=f"{kind.value}:1",
                text=f"Fixture {kind.value} source.",
                slide_number=1 if kind is SourceKind.SLIDE else None,
            )
            for index, kind in enumerate(
                (SourceKind.SLIDE, SourceKind.TRANSCRIPT, SourceKind.SUMMARY), 1
            )
        )


def test_lease_expiry_reclaim_remains_fenced_by_s0_worker_contract(tmp_path: Path) -> None:
    """C-1: stale completion cannot fail or replace B's reclaimed preflight stage."""

    class WaitingRunner:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def run(self, _context: object) -> StageProduct:
            self.entered.set()
            await self.release.wait()
            return StageProduct(kind="fixture", payload={})

    class CompletingRunner:
        async def run(self, _context: object) -> StageProduct:
            return StageProduct(kind="fixture", payload={})

    async def scenario() -> None:
        database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
        database.migrate()
        try:
            with database.session() as session:
                lecture = LectureModel(
                    subject="Heme",
                    exam_number=1,
                    lecture_number=1,
                    topic="Synthesis",
                    lecturer="Fixture",
                )
                session.add(lecture)
                session.flush()
                lecture_id = lecture.id
            repository = AnkiCurationRepository(database)
            job = repository.create_job(
                CreateCurationJob(
                    lecture_id=lecture_id,
                    block_id=None,
                    source_revision_ids=(1,),
                    deck_allowlist=("AnKing",),
                    tag_allowlist=("#heme",),
                    instruction_text="",
                    target_deck="OMS::Heme",
                    target_tag="fixture",
                    index_snapshot_id="fixture-snapshot",
                    lcl_prompt_version="lecture-concept-ledger",
                    judgment_rubric_version="coverage-rubric",
                    gap_prompt_version="gap-card-generation",
                    provider="openai",
                    model="fixture",
                    pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
                )
            )
            started = datetime(2026, 8, 8, tzinfo=UTC)
            assert repository.claim_next_job(started, worker_id="worker-a", lease_seconds=3)
            current = [started]
            runner_a = WaitingRunner()
            pipeline_a = CurationPipeline(
                repository, StageArtifactStore(tmp_path / "artifacts"), runner_a
            )
            stale = asyncio.create_task(
                pipeline_a.run_stage(job.id, lease_owner="worker-a", lease_clock=lambda: current[0])
            )
            await runner_a.entered.wait()
            current[0] = started + timedelta(seconds=4)
            assert repository.claim_next_job(current[0], worker_id="worker-b", lease_seconds=30)
            pipeline_b = CurationPipeline(
                repository,
                StageArtifactStore(tmp_path / "artifacts"),
                CompletingRunner(),
            )
            completed = await pipeline_b.run_stage(
                job.id, lease_owner="worker-b", lease_clock=lambda: current[0]
            )
            assert completed is not None
            assert completed.state is CurationState.BUILDING_SOURCE_INDEX
            runner_a.release.set()
            with pytest.raises(InvalidCurationTransition, match="not in preflight"):
                await stale
            assert repository.require_job(job.id).state is CurationState.BUILDING_SOURCE_INDEX
        finally:
            database.close()

    asyncio.run(scenario())


def test_invalid_orphan_evidence_is_rejected_before_recompute_without_a_scanner() -> None:
    evidence = OrphanArtifactAdoptionEvidence(
        job_id="11111111-1111-4111-8111-111111111111",
        stage=CurationStage.CARD_CLASSIFY,
        stage_input_sha256="a" * 64,
        artifact_kind="card_centric_classification",
        artifact_schema_version=2,
        content_sha256="b" * 64,
        complete_write_marker="atomic-rename+fsync",
        conflicting_committed_artifact=False,
    )
    assert evidence.complete_write_marker == "atomic-rename+fsync"
    with pytest.raises(ValueError):
        OrphanArtifactAdoptionEvidence(**{**evidence.model_dump(), "complete_write_marker": ""})


def test_expected_red_p1_m4_adopts_exact_orphan_without_second_provider_call(
    tmp_path: Path,
) -> None:
    """P1 M-4: only an exactly evidenced orphan may be adopted after reclaim."""

    class CountingRunner:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, _context: object) -> StageProduct:
            self.calls += 1
            return StageProduct(kind="fixture", payload={"run": self.calls})

    async def scenario() -> None:
        database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
        database.migrate()
        try:
            with database.session() as session:
                lecture = LectureModel(
                    subject="Heme",
                    exam_number=1,
                    lecture_number=1,
                    topic="Synthesis",
                    lecturer="Fixture",
                )
                session.add(lecture)
                session.flush()
                lecture_id = lecture.id
            repository = AnkiCurationRepository(database)
            job = repository.create_job(
                CreateCurationJob(
                    lecture_id=lecture_id,
                    block_id=None,
                    source_revision_ids=(1,),
                    deck_allowlist=("AnKing",),
                    tag_allowlist=("#heme",),
                    instruction_text="",
                    target_deck="OMS::Heme",
                    target_tag="fixture",
                    index_snapshot_id="fixture-snapshot",
                    lcl_prompt_version="lecture-concept-ledger",
                    judgment_rubric_version="coverage-rubric",
                    gap_prompt_version="gap-card-generation",
                    provider="openai",
                    model="fixture",
                    pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
                )
            )
            started = datetime(2026, 8, 8, tzinfo=UTC)
            assert repository.claim_next_job(started, worker_id="worker-a", lease_seconds=3)
            repository.start_stage(
                job.id,
                CurationStage.PREFLIGHT,
                provider="openai",
                model="fixture",
                expected_state=CurationState.PREFLIGHT,
                lease_owner="worker-a",
                now=started,
            )
            artifacts = StageArtifactStore(tmp_path / "artifacts")
            prepare = getattr(repository, "prepare_stage_replay_inputs", None)
            assert callable(prepare), (
                "P1 M-4: orphan adoption must use the frozen prepared replay-input identity"
            )
            prepared = prepare(job.id, CurationStage.PREFLIGHT)
            assert prepared.sha256 == hashlib.sha256(prepared.canonical_json.encode()).hexdigest()
            identity = {
                "job_id": str(job.id),
                "stage": CurationStage.PREFLIGHT.value,
                "configuration_sha256": job.configuration_sha256,
                "pipeline_contract_version": job.pipeline_contract_version.value,
                "model_config_sha256": job.model_config_sha256,
                "source_revision_ids": job.source_revision_ids,
                "index_snapshot_id": job.index_snapshot_id,
                "semantic_generation": job.semantic_generation,
                "companion_generation": job.companion_generation,
                "source_index_generation": job.source_index_generation,
                "prompt_versions": {
                    "lcl": job.lcl_prompt_version,
                    "judgment": job.judgment_rubric_version,
                    "gap": job.gap_prompt_version,
                },
                "provider": job.provider,
                "model": job.model,
                "prior_artifacts": [],
                "prepared_replay_inputs": {
                    "sha256": prepared.sha256,
                    "canonical_json": prepared.canonical_json,
                },
            }
            stage_input_sha256 = hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            orphan = artifacts.write(
                job.id,
                CurationStage.PREFLIGHT,
                StageProduct(
                    kind="fixture",
                    payload={"crash": "before-db-commit"},
                    metadata={
                        "orphan_adoption": {
                            "job_id": str(job.id),
                            "stage": CurationStage.PREFLIGHT.value,
                            "stage_input_sha256": stage_input_sha256,
                            "artifact_schema_version": 2,
                            "complete_write_marker": "atomic-rename+fsync",
                        }
                    },
                ),
                input_sha256=stage_input_sha256,
                pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
                model_config_sha256=job.model_config_sha256,
            )
            assert (tmp_path / "artifacts" / orphan.relative_path).is_file()
            assert repository.list_stage_artifacts(job.id) == []
            evidence = OrphanArtifactAdoptionEvidence(
                job_id=job.id,
                stage=CurationStage.PREFLIGHT,
                stage_input_sha256=stage_input_sha256,
                artifact_kind="fixture",
                artifact_schema_version=2,
                content_sha256=orphan.content_sha256,
                complete_write_marker="atomic-rename+fsync",
                conflicting_committed_artifact=False,
            )
            assert evidence.content_sha256 == orphan.content_sha256

            reclaimed_at = started + timedelta(seconds=4)
            assert repository.claim_next_job(reclaimed_at, worker_id="worker-b", lease_seconds=30)
            runner = CountingRunner()
            pipeline = CurationPipeline(repository, artifacts, runner)
            result = await pipeline.run_stage(
                job.id, lease_owner="worker-b", lease_clock=lambda: reclaimed_at
            )

            assert result is not None
            assert runner.calls == 0, (
                "P1 M-4: valid exact orphan evidence must be adopted without a second provider call"
            )
            assert repository.require_job(job.id).state is CurationState.BUILDING_SOURCE_INDEX
            assert len(repository.list_stage_artifacts(job.id)) == 1
        finally:
            database.close()

    asyncio.run(scenario())


def test_s0_snapshot_must_pin_classifier_prompt_for_replay_identity() -> None:
    """Expected red on S0: P1 M-2 must snapshot this live S4c/S6 prompt."""
    from oms_hub.anki.prompt_catalog import AnkiPromptCatalogService

    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.runtime = _ReadyRuntime()
    runner.prompts = AnkiPromptCatalogService()
    runner.prompt_sync = StaticPromptSynchronizer()
    runner.source_extractor = _RequiredSources()
    context = SimpleNamespace(
        job=SimpleNamespace(
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
            lcl_prompt_version="lecture-concept-ledger",
            judgment_rubric_version="coverage-rubric",
            gap_prompt_version="gap-card-generation",
            source_revision_ids=(1, 2, 3),
            summary_outline_id=None,
        )
    )

    product = asyncio.run(runner._preflight(context))

    assert "card-centric-classifier" in {
        item["id"] for item in product.payload["prompt_snapshot"]
    }, "P1 M-2: S4c/S6 classifier prompt must be pinned at S0"


def test_s6_generation_switch_must_be_passed_to_semantic_search() -> None:
    """Expected red on S0: P1 M-3 requires expected_generation fencing."""
    source = build_source_index(
        (
            SourcePassage.create(
                revision_id=7,
                lecture_id=12,
                artifact_id="slides-7",
                source_kind=SourceKind.SLIDE,
                locator="slide:1",
                text="Heme synthesis begins in mitochondria.",
                slide_number=1,
            ),
        ),
        snapshot_id="fixture-snapshot",
        source_revision_hashes={7: "a" * 64},
    )
    card = CardRecord(
        note_id=1,
        content_sha256="b" * 64,
        text="Heme synthesis begins in mitochondria.",
        extra="",
        tags=("#heme",),
        deck_names=("AnKing",),
    )
    ledger = CardConceptLedger(
        lecture_entity_count=1,
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="Heme synthesis begins in mitochondria.",
                primary_entity="heme synthesis",
                depth="deep",
                emphasis_flag=True,
                importance="high",
            ),
        ),
    )
    empty = ClassifierResult(
        results=(),
        telemetry=ClassifierTelemetry(
            batch_count=0,
            cache_prefix_sha256="c" * 64,
            cache_mode="ordinary_prefix",
            provider="openai",
            model="fixture",
            request_ids=(),
            batches=(),
        ),
    )
    semantic = GenerationSwitchingSemantic(
        before_search="generation-a",
        after_search="generation-b",
        hits={"heme synthesis": [SemanticHit(note_id=1, score=0.3, content_hash="b" * 64)]},
        calls=[],
    )
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.semantic = semantic
    runner.structured = SimpleNamespace(generator=SimpleNamespace())
    from oms_hub.anki.prompt_catalog import AnkiPromptCatalogService

    runner.prompts = AnkiPromptCatalogService()
    context = SimpleNamespace(
        job=SimpleNamespace(
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
            semantic_generation="generation-a",
            resolved_model_config=SimpleNamespace(
                residual_s6=SimpleNamespace(provider="openai", model="fixture")
            ),
        ),
        prior_payloads={
            CurationStage.SOURCE_INDEX: {
                "source_index": source.model_dump(mode="json"),
                "cards": [card.model_dump(mode="json")],
            },
            CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
            CurationStage.CARD_COVERAGE: {
                "coverage": {"C01": {"status": "uncovered", "evidence": []}}
            },
            CurationStage.CARD_TAG_SCOPE: {
                "scope": TagScopeResult(
                    snapshot_id="fixture-snapshot",
                    filters_sha256="d" * 64,
                    scoped_note_ids=(1,),
                    unscoped_note_ids=(),
                ).model_dump(mode="json"),
                "residual_mode": "gaps_only",
            },
            CurationStage.CARD_FAST_CLASSIFY: {
                "fast_classifier": FastClassificationResult(results=()).model_dump(mode="json"),
                "fallback_note_ids": [1],
            },
            CurationStage.CARD_CLASSIFY: {"classifier": empty.model_dump(mode="json")},
        },
    )

    with pytest.raises(PinnedInputChanged, match="generation changed before residual search"):
        asyncio.run(runner._card_residual(context))

    assert semantic.calls == [
        {
            "queries": ("heme synthesis heme synthesis",),
            "eligible_note_ids": {1},
            "limit": 12,
            "expected_generation": "generation-a",
            "generation": "generation-b",
        }
    ], "P1 M-3: S6 semantic search must bind and verify the pinned generation"


def test_s7_title_edit_must_not_change_provider_input_mid_stage() -> None:
    """Expected red on S0: P1 H-12 requires an immutable lecture metadata snapshot."""

    class ChangingTitleRepository:
        def __init__(self) -> None:
            self.titles = iter(("Heme synthesis", "Heme synthesis revised"))

        def lecture_title(self, _lecture_id: int) -> str:
            return next(self.titles)

    class RecordingGapProvider:
        def __init__(self) -> None:
            self.provider_inputs: list[dict[str, object]] = []

        def generate_json(self, _instruction: str, input_text: str, **kwargs: object) -> object:
            self.provider_inputs.append(json.loads(input_text))
            fact_id = self.provider_inputs[-1]["missing_facts"][0]["fact_id"]
            value = CardGapBatch(
                resolutions=(CardGapOutput(fact_id=fact_id, status="unresolved", reason="fixture"),)
            )
            return StructuredJSONResult(
                value=value,
                raw_text=value.model_dump_json(),
                provider=kwargs["provider"],
                model=kwargs["model"],
                request_id=f"gap-{len(self.provider_inputs)}",
                input_tokens=1,
                output_tokens=1,
                cost_microusd=1,
            )

    passage = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:1",
        text="Heme synthesis begins in mitochondria.",
        slide_number=1,
    )
    source = build_source_index(
        (passage,), snapshot_id="fixture-snapshot", source_revision_hashes={7: "a" * 64}
    )
    ledger = CardConceptLedger(
        lecture_entity_count=1,
        concepts=tuple(
            CardConcept(
                concept_id=concept_id,
                canonical_statement=f"{concept_id} fact.",
                primary_entity=concept_id,
                depth="deep",
                emphasis_flag=True,
                importance="high",
            )
            for concept_id in ("C01", "C02")
        ),
    )
    empty = ClassifierResult(
        results=(),
        telemetry=ClassifierTelemetry(
            batch_count=0,
            cache_prefix_sha256="c" * 64,
            cache_mode="ordinary_prefix",
            provider="openai",
            model="fixture",
            request_ids=(),
            batches=(),
        ),
    )
    gap_prompt = AnkiPromptLibrary().load("card-centric-gap-v2")
    provider = RecordingGapProvider()
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.repository = ChangingTitleRepository()
    runner.structured = provider
    context = SimpleNamespace(
        job=SimpleNamespace(
            lecture_id=12,
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
            resolved_model_config=SimpleNamespace(
                gap_fill_s7=SimpleNamespace(provider="openai", model="fixture")
            ),
        ),
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": gap_prompt.metadata.id,
                        "version": gap_prompt.metadata.version,
                        "prompt_hash": gap_prompt.prompt_hash,
                        "content": gap_prompt.content,
                        "metadata": gap_prompt.metadata.model_dump(mode="json", by_alias=True),
                    }
                ]
            },
            CurationStage.SOURCE_INDEX: {"source_index": source.model_dump(mode="json")},
            CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
            CurationStage.CARD_COVERAGE: {
                "coverage": {
                    concept_id: {"status": "uncovered", "evidence": []}
                    for concept_id in ("C01", "C02")
                }
            },
            CurationStage.CARD_CLASSIFY: {"classifier": empty.model_dump(mode="json")},
            CurationStage.CARD_RESIDUAL: {"classifier": None},
            CurationStage.CARD_FAST_CLASSIFY: {
                "fast_classifier": FastClassificationResult(results=()).model_dump(mode="json"),
                "fallback_note_ids": [],
            },
        },
    )

    asyncio.run(runner._card_gap_fill(context))
    provider_inputs = provider.provider_inputs

    assert provider_inputs[0]["lecture_title"] == provider_inputs[1]["lecture_title"], (
        "P1 H-12: S7 concept requests must use the same pinned lecture title"
    )


def test_missing_pinned_prompt_blocks_rather_than_using_live_prompt() -> None:
    """Green S0 guard for M-15; live prompt fallback is never replay-safe."""
    context = SimpleNamespace(prior_payloads={CurationStage.PREFLIGHT: {"prompt_snapshot": []}})

    from oms_hub.anki.stages import _pinned_card_v2_prompt

    with pytest.raises(PinnedInputChanged, match="unavailable or duplicated"):
        _pinned_card_v2_prompt(context, "card-centric-gap-v2")


def test_expected_red_m15_preflight_reports_actionable_missing_v2_prompt(tmp_path: Path) -> None:
    """P2 M-15: preflight reports the missing prompt ID, searched path, and remediation."""
    from shutil import copytree

    from oms_hub.anki.prompt_catalog import AnkiPromptCatalogService
    from oms_hub.anki.prompts import AnkiPromptConfigurationError, AnkiPromptLibrary

    copytree(AnkiPromptLibrary().root, tmp_path, dirs_exist_ok=True)
    (tmp_path / "card-centric-ledger-v2.md").unlink()

    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.runtime = _ReadyRuntime()
    runner.prompt_sync = StaticPromptSynchronizer()
    runner.prompts = AnkiPromptCatalogService(bundled_directory=tmp_path)
    runner.source_extractor = _RequiredSources()
    context = SimpleNamespace(
        job=SimpleNamespace(
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
            lcl_prompt_version="lecture-concept-ledger",
            judgment_rubric_version="coverage-rubric",
            gap_prompt_version="gap-card-generation",
            source_revision_ids=(1, 2, 3),
            summary_outline_id=None,
        )
    )

    with pytest.raises(AnkiPromptConfigurationError) as raised:
        asyncio.run(runner._preflight(context))

    missing_path = tmp_path / "card-centric-ledger-v2.md"
    message = str(raised.value).casefold()
    assert "card-centric-ledger-v2" in message
    assert str(missing_path).casefold() in message
    assert "restore" in message
    assert "configure" in message


def test_expected_red_m1_a11_history_is_distinct_bounded_and_frozen_after_s9_input(
    tmp_path: Path,
) -> None:
    """P1 M-1/D16: A11 uses 12 distinct latest-job rates and freezes its S9 replay input."""
    database = Database(f"sqlite:///{tmp_path / 'history.db'}")
    database.migrate()
    try:
        with database.session() as session:
            lecture = LectureModel(
                subject="Heme",
                exam_number=1,
                lecture_number=1,
                topic="Synthesis",
                lecturer="Fixture",
            )
            session.add(lecture)
            session.flush()
            lecture_id = lecture.id
        repository = AnkiCurationRepository(database)

        def create_job() -> object:
            return repository.create_job(
                CreateCurationJob(
                    lecture_id=lecture_id,
                    block_id=None,
                    source_revision_ids=(1,),
                    deck_allowlist=("AnKing",),
                    tag_allowlist=("#heme",),
                    instruction_text="",
                    target_deck="OMS::Heme",
                    target_tag="fixture",
                    index_snapshot_id="fixture-snapshot",
                    lcl_prompt_version="lecture-concept-ledger",
                    judgment_rubric_version="coverage-rubric",
                    gap_prompt_version="gap-card-generation",
                    provider="openai",
                    model="fixture",
                    pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
                )
            )

        current = create_job()
        prior = [create_job() for _ in range(13)]

        def payload(rate: float) -> str:
            keeps = round(rate * 100)
            return json.dumps(
                {
                    "snapshot": {
                        "classifications": [
                            {"verdict": "keep" if index < keeps else "drop"} for index in range(100)
                        ]
                    }
                }
            )

        with database.session() as session:
            for index, job in enumerate(prior, 1):
                session.add(
                    AnkiReviewedReconciliationModel(
                        job_id=str(job.id), review_revision=1, payload_json=payload(index / 100)
                    )
                )
            # The latest revision is authoritative for this job; revision 1 must not count twice.
            session.add(
                AnkiReviewedReconciliationModel(
                    job_id=str(prior[-1].id), review_revision=2, payload_json=payload(0.0)
                )
            )
        prepare = getattr(repository, "prepare_stage_replay_inputs", None)
        assert callable(prepare), (
            "P1 M-1/D16: repository must prepare and freeze A11 history for S9 replay"
        )
        before_replay = prepare(current.id, CurationStage.RECONCILIATION)
        entries = before_replay.document["a11_history"]["entries"]
        assert len(entries) == 12
        assert len({entry["job_id"] for entry in entries}) == 12
        assert (
            before_replay.sha256
            == hashlib.sha256(before_replay.canonical_json.encode()).hexdigest()
        )
        with database.session() as session:
            session.add(
                AnkiReviewedReconciliationModel(
                    job_id=str(prior[-1].id), review_revision=3, payload_json=payload(1.0)
                )
            )
        after_later_review = prepare(current.id, CurationStage.RECONCILIATION)

        assert after_later_review.canonical_json == before_replay.canonical_json
        assert after_later_review.document == before_replay.document
        assert after_later_review.sha256 == before_replay.sha256
    finally:
        database.close()
