import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from oms_hub.anki.domain import (
    Candidate,
    CreateCurationJob,
    CurationStage,
    CurationState,
    EvidenceSupport,
    GapCard,
    PipelineContractVersion,
    RetrievalPass,
    SourceEvidence,
    SourceKind,
    SourceReference,
    StageUsage,
)
from oms_hub.anki.pipeline import (
    CurationPipeline,
    PinnedInputChanged,
    StageArtifactStore,
    StageContext,
    StageProduct,
    _stage_input_hash,
)
from oms_hub.anki.repository import AnkiCurationRepository
from oms_hub.db import Database
from oms_hub.models import LectureModel


class CountingRunner:
    def __init__(self) -> None:
        self.calls = 0
        self.contexts: list[StageContext] = []

    async def run(self, context: StageContext) -> StageProduct:
        self.calls += 1
        self.contexts.append(context)
        return StageProduct(
            kind="preflight_report",
            payload={"ready": True, "call": self.calls},
            metadata={"runner": "counting"},
            cache_hits=7,
            job_pins={"semantic_generation": "semantic-1"},
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
    result = AnkiCurationRepository(database)
    result._test_lecture_id = lecture_id  # type: ignore[attr-defined]
    yield result
    database.close()


def _claimed_job(
    repository: AnkiCurationRepository,
    pipeline_contract_version: PipelineContractVersion = PipelineContractVersion.RETRIEVAL_V4,
):
    job = repository.create_job(
        CreateCurationJob(
            lecture_id=repository._test_lecture_id,  # type: ignore[attr-defined]
            block_id="heme-block",
            source_revision_ids=(11,),
            deck_allowlist=("AnKing Step Deck",),
            tag_allowlist=(),
            instruction_text="Focus on lecture objectives.",
            target_deck="OMS::Heme::Lecture 4",
            target_tag="AnkiHub_Optional::LMU_OMS_II::Heme::Lecture_4",
            index_snapshot_id="snapshot-1",
            lcl_prompt_version="lcl-v1",
            judgment_rubric_version="judgment-v1",
            gap_prompt_version="gap-v1",
            provider="anthropic",
            model="claude-sonnet",
            semantic_generation="semantic-1",
            pipeline_contract_version=pipeline_contract_version,
        )
    )
    claimed = repository.transition(job.id, CurationState.QUEUED, CurationState.PREFLIGHT)
    assert claimed.id == job.id
    return claimed


def _canonical_write(path: Path, document: dict[str, object]) -> None:
    path.write_bytes(
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        + b"\n"
    )


async def _crash_after_durable_write(
    repository: AnkiCurationRepository,
    store: StageArtifactStore,
    runner: CountingRunner,
    job_id: UUID,
) -> None:
    original_commit = repository.commit_stage

    def process_died(*args: object, **kwargs: object):
        del args, kwargs
        raise SystemExit("simulated process death after durable write")

    repository.commit_stage = process_died  # type: ignore[method-assign]
    with pytest.raises(SystemExit, match="simulated process death"):
        await CurationPipeline(repository, store, runner).run_stage(job_id)
    repository.commit_stage = original_commit  # type: ignore[method-assign]


def test_valid_orphan_is_adopted_without_second_runner_call(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        job = _claimed_job(repository)
        store = StageArtifactStore(tmp_path / "artifacts")
        runner = CountingRunner()

        await _crash_after_durable_write(repository, store, runner, job.id)

        result = await CurationPipeline(repository, store, runner).run_stage(job.id)

        assert result is not None
        assert result.stage is CurationStage.PREFLIGHT
        assert runner.calls == 1
        assert result.artifact.input_sha256 == _stage_input_hash(job, CurationStage.PREFLIGHT, ())
        stage = repository.get_stage(job.id, CurationStage.PREFLIGHT)
        assert stage is not None
        assert stage.cache_hits == 7
        assert repository.require_job(job.id).semantic_generation == "semantic-1"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_manifest",
        "corrupt_content",
        "job",
        "stage",
        "input",
        "kind",
        "schema",
        "checksum",
        "marker",
    ],
)
def test_invalid_orphan_evidence_is_recomputed(
    repository: AnkiCurationRepository,
    tmp_path: Path,
    mutation: str,
) -> None:
    async def scenario() -> None:
        job = _claimed_job(repository)
        store = StageArtifactStore(tmp_path / "artifacts")
        runner = CountingRunner()
        input_sha256 = _stage_input_hash(job, CurationStage.PREFLIGHT, ())
        manifest_path = (
            tmp_path
            / "artifacts"
            / str(job.id)
            / CurationStage.PREFLIGHT.value
            / ".orphan"
            / f"{input_sha256}.json"
        )

        await _crash_after_durable_write(repository, store, runner, job.id)
        if mutation == "missing_manifest":
            manifest_path.unlink()
        elif mutation == "corrupt_content":
            manifest = json.loads(manifest_path.read_bytes())
            artifact_path = tmp_path / "artifacts" / manifest["artifact_relative_path"]
            artifact_path.write_bytes(b"not a complete artifact")
        else:
            manifest = json.loads(manifest_path.read_bytes())
            evidence = manifest["evidence"]
            assert isinstance(evidence, dict)
            replacements: dict[str, object] = {
                "job": str(UUID("e1b4bdc2-7d4f-44b4-a2a8-a926fcba6e19")),
                "stage": CurationStage.SOURCE_INDEX.value,
                "input": "b" * 64,
                "kind": "wrong_kind",
                "schema": 2,
                "checksum": "c" * 64,
                "marker": "incomplete",
            }
            evidence[
                {
                    "job": "job_id",
                    "stage": "stage",
                    "input": "stage_input_sha256",
                    "kind": "artifact_kind",
                    "schema": "artifact_schema_version",
                    "checksum": "content_sha256",
                    "marker": "complete_write_marker",
                }[mutation]
            ] = replacements[mutation]
            _canonical_write(manifest_path, manifest)

        result = await CurationPipeline(repository, store, runner).run_stage(job.id)

        assert result is not None
        assert runner.calls == 2

    asyncio.run(scenario())


def test_conflicting_committed_artifact_is_not_adopted(tmp_path: Path) -> None:
    store = StageArtifactStore(tmp_path / "artifacts")
    job_id = UUID("d1b4bdc2-7d4f-44b4-a2a8-a926fcba6e19")
    artifact = store.write(
        job_id,
        CurationStage.PREFLIGHT,
        StageProduct(kind="preflight_report", payload={"ready": True}),
        input_sha256="a" * 64,
        model_config_sha256="b" * 64,
    )

    assert (
        store.orphans.recover(
            job_id=job_id,
            stage=CurationStage.PREFLIGHT,
            input_sha256="a" * 64,
            pipeline_contract_version=PipelineContractVersion.RETRIEVAL_V4,
            model_config_sha256="b" * 64,
            committed_artifacts=(artifact,),
        )
        is None
    )


def test_orphan_recovery_reconstructs_every_stage_commit_field(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    job = _claimed_job(repository)
    source_ref = SourceReference(SourceKind.SLIDE, 11, "slide:4", "source-sha")
    product = StageProduct(
        kind="preflight_report",
        payload={"ready": True},
        metadata={"test": "durable"},
        usage=StageUsage("request-1", 10, 20, 30),
        cache_hits=7,
        candidates=(
            Candidate(
                note_id=42,
                content_hash="candidate-sha",
                best_concept_id="concept-1",
                provenance={"source": "test"},
                scores={"coverage": 0.9},
                predicted_band="high",
                verdict="keep",
                confidence=0.9,
                reason="grounded",
                context_trap=False,
                recall_direction="forward",
                mnemonic_classification="none",
                dedupe_disposition="unique",
                selected=True,
                retrieval_pass=RetrievalPass.PASS_1,
            ),
        ),
        source_evidence=(
            SourceEvidence(
                "evidence-1",
                "concept-1",
                EvidenceSupport.SUPPORTED,
                "Supported by slide 4.",
                (source_ref,),
                "evidence-sha",
            ),
        ),
        gap_cards=(
            GapCard(
                "concept-2",
                "Front",
                "Back",
                source_refs=(source_ref,),
                evidence_ids=("evidence-1",),
                provenance={"generated": True},
                content_hash="gap-sha",
                card_id="gap-1",
            ),
        ),
        job_pins={"semantic_generation": "semantic-1"},
    )
    store = StageArtifactStore(tmp_path / "artifacts")
    artifact = store.write(
        job.id,
        CurationStage.PREFLIGHT,
        product,
        input_sha256="a" * 64,
        pipeline_contract_version=job.pipeline_contract_version,
        model_config_sha256=job.model_config_sha256,
    )

    recovered = store.recover_orphan(
        job=job,
        stage=CurationStage.PREFLIGHT,
        input_sha256="a" * 64,
        committed_artifacts=(),
    )

    assert recovered == (artifact, product)


def test_prepared_replay_inputs_are_hashed_and_exposed_to_runner(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        job = _claimed_job(repository, PipelineContractVersion.CARD_CENTRIC_V2)
        canonical_json = '{"history":["prior-job"],"lecture":{"title":"Heme"}}'
        repository.prepare_stage_replay_inputs = lambda job_id, stage: SimpleNamespace(  # type: ignore[attr-defined]
            job_id=job_id,
            stage=stage,
            canonical_json=canonical_json,
            sha256=hashlib.sha256(canonical_json.encode()).hexdigest(),
            document=json.loads(canonical_json),
        )
        runner = CountingRunner()

        result = await CurationPipeline(
            repository,
            StageArtifactStore(tmp_path / "artifacts"),
            runner,
        ).run_stage(job.id)

        assert result is not None
        assert (
            runner.contexts[0].replay_inputs_sha256
            == hashlib.sha256(canonical_json.encode()).hexdigest()
        )
        assert runner.contexts[0].replay_inputs["lecture"]["title"] == "Heme"
        assert result.artifact.input_sha256 != _stage_input_hash(job, CurationStage.PREFLIGHT, ())
        with pytest.raises(TypeError):
            runner.contexts[0].replay_inputs["new"] = "mutable"  # type: ignore[index]

    asyncio.run(scenario())


def test_card_centric_v2_rejects_missing_prepared_replay_inputs(
    repository: AnkiCurationRepository,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        job = _claimed_job(repository, PipelineContractVersion.CARD_CENTRIC_V2)
        runner = CountingRunner()

        with pytest.raises(PinnedInputChanged, match="replay inputs are unavailable"):
            await CurationPipeline(
                repository,
                StageArtifactStore(tmp_path / "artifacts"),
                runner,
            ).run_stage(job.id)

    asyncio.run(scenario())
