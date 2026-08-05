import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from uuid import UUID

from oms_hub.anki.domain import (
    Candidate,
    CurationJob,
    CurationStage,
    CurationState,
    GapCard,
    PipelineContractVersion,
    SourceEvidence,
    StageArtifact,
    StageUsage,
)
from oms_hub.anki.repository import AnkiCurationRepository


class PinnedInputChanged(ValueError):
    """A source or index generation pinned by the job is no longer valid."""


_EMPTY_DOCUMENT_SHA256 = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True, slots=True)
class PipelineStageDefinition:
    state: CurationState
    stage: CurationStage
    next_state: CurationState


PIPELINE_STAGES = (
    PipelineStageDefinition(
        CurationState.PREFLIGHT,
        CurationStage.PREFLIGHT,
        CurationState.BUILDING_SOURCE_INDEX,
    ),
    PipelineStageDefinition(
        CurationState.BUILDING_SOURCE_INDEX,
        CurationStage.SOURCE_INDEX,
        CurationState.BUILDING_LCL,
    ),
    PipelineStageDefinition(
        CurationState.BUILDING_LCL,
        CurationStage.LCL,
        CurationState.RETRIEVING_PASS_1,
    ),
    PipelineStageDefinition(
        CurationState.RETRIEVING_PASS_1,
        CurationStage.RETRIEVAL_PASS_1,
        CurationState.JUDGING_PASS_1,
    ),
    PipelineStageDefinition(
        CurationState.JUDGING_PASS_1,
        CurationStage.JUDGMENT_PASS_1,
        CurationState.LOCALIZING_MISSED_CONCEPTS,
    ),
    PipelineStageDefinition(
        CurationState.LOCALIZING_MISSED_CONCEPTS,
        CurationStage.RESCUE,
        CurationState.RETRIEVING_PASS_2,
    ),
    PipelineStageDefinition(
        CurationState.RETRIEVING_PASS_2,
        CurationStage.RETRIEVAL_PASS_2,
        CurationState.JUDGING_PASS_2,
    ),
    PipelineStageDefinition(
        CurationState.JUDGING_PASS_2,
        CurationStage.JUDGMENT_PASS_2,
        CurationState.CONVERGING_PASS_3,
    ),
    PipelineStageDefinition(
        CurationState.CONVERGING_PASS_3,
        CurationStage.CONVERGENCE_PASS_3,
        CurationState.CONVERGING_PASS_4,
    ),
    PipelineStageDefinition(
        CurationState.CONVERGING_PASS_4,
        CurationStage.CONVERGENCE_PASS_4,
        CurationState.CONVERGING_PASS_5,
    ),
    PipelineStageDefinition(
        CurationState.CONVERGING_PASS_5,
        CurationStage.CONVERGENCE_PASS_5,
        CurationState.AUDITING_CANDIDATES,
    ),
    PipelineStageDefinition(
        CurationState.AUDITING_CANDIDATES,
        CurationStage.CARD_AUDIT,
        CurationState.RECOMPUTING_COVERAGE,
    ),
    PipelineStageDefinition(
        CurationState.RECOMPUTING_COVERAGE,
        CurationStage.COVERAGE_RECOMPUTE,
        CurationState.DEDUPING,
    ),
    PipelineStageDefinition(
        CurationState.DEDUPING,
        CurationStage.DEDUPE,
        CurationState.GENERATING_GAPS,
    ),
    PipelineStageDefinition(
        CurationState.GENERATING_GAPS,
        CurationStage.GAPS,
        CurationState.RECONCILING,
    ),
    PipelineStageDefinition(
        CurationState.RECONCILING,
        CurationStage.RECONCILIATION,
        CurationState.READY_FOR_REVIEW,
    ),
)
_STAGE_BY_STATE = {definition.state: definition for definition in PIPELINE_STAGES}


class UnsupportedPipelineContract(RuntimeError):
    """A stored job requests a graph this Hub cannot execute yet."""


def pipeline_stages(version: PipelineContractVersion) -> tuple[PipelineStageDefinition, ...]:
    if version is PipelineContractVersion.RETRIEVAL_V4:
        return PIPELINE_STAGES
    raise UnsupportedPipelineContract(
        f"pipeline contract {version.value} is unsupported; upgrade required; no mutation performed"
    )


@dataclass(frozen=True, slots=True)
class StageContext:
    job: CurationJob
    stage: CurationStage
    input_sha256: str
    prior_artifacts: tuple[StageArtifact, ...]
    prior_payloads: Mapping[CurationStage, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class StageProduct:
    kind: str
    payload: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    usage: StageUsage | None = None
    cache_hits: int = 0
    candidates: tuple[Candidate, ...] | None = None
    source_evidence: tuple[SourceEvidence, ...] | None = None
    gap_cards: tuple[GapCard, ...] | None = None
    job_pins: dict[str, str] = field(default_factory=dict)
    blocking_error: str | None = None


@dataclass(frozen=True, slots=True)
class StageRunResult:
    job_id: UUID
    stage: CurationStage
    state: CurationState
    artifact: StageArtifact


class CurationStageRunner(Protocol):
    async def run(self, context: StageContext) -> StageProduct: ...


class CurationInputValidator(Protocol):
    def validate(self, job_id: UUID) -> None: ...


class _AllowPinnedInputs:
    def validate(self, job_id: UUID) -> None:
        del job_id


class StageArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write(
        self,
        job_id: UUID,
        stage: CurationStage,
        product: StageProduct,
        *,
        input_sha256: str,
        pipeline_contract_version: PipelineContractVersion = PipelineContractVersion.RETRIEVAL_V4,
        model_config_sha256: str = "",
    ) -> StageArtifact:
        if len(input_sha256) != 64:
            raise ValueError("stage input hash is invalid")
        if not product.kind.strip():
            raise ValueError("stage artifact kind cannot be blank")
        document = {
            "artifact_version": 2,
            "job_id": str(job_id),
            "stage": stage.value,
            "kind": product.kind,
            "pipeline_contract_version": pipeline_contract_version.value,
            "model_config_sha256": model_config_sha256,
            "payload": product.payload,
            "metadata": product.metadata,
        }
        encoded = _canonical_json(document).encode("utf-8") + b"\n"
        content_sha256 = hashlib.sha256(encoded).hexdigest()
        relative = Path(str(job_id)) / stage.value / f"{content_sha256}.json"
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.read_bytes() != encoded:
                raise ValueError("immutable stage artifact has conflicting content")
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{content_sha256}-",
                suffix=".tmp",
                dir=destination.parent,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
                _fsync_directory(destination.parent)
            finally:
                temporary.unlink(missing_ok=True)
        return StageArtifact(
            artifact_id=f"{stage.value}:{content_sha256}",
            stage=stage,
            kind=product.kind.strip(),
            relative_path=relative.as_posix(),
            input_sha256=input_sha256,
            content_sha256=content_sha256,
            pipeline_contract_version=pipeline_contract_version,
            model_config_sha256=model_config_sha256,
            metadata=dict(product.metadata),
        )

    def read(
        self,
        artifact: StageArtifact,
        *,
        job: CurationJob | None = None,
    ) -> dict[str, Any]:
        relative = PurePosixPath(artifact.relative_path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("stage artifact path is unsafe")
        path = self.root.joinpath(*relative.parts)
        try:
            encoded = path.read_bytes()
        except OSError as exc:
            raise PinnedInputChanged(
                f"Committed artifact {artifact.artifact_id} is unavailable"
            ) from exc
        if hashlib.sha256(encoded).hexdigest() != artifact.content_sha256:
            raise PinnedInputChanged(f"Committed artifact {artifact.artifact_id} changed")
        try:
            document = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise PinnedInputChanged(
                f"Committed artifact {artifact.artifact_id} is invalid"
            ) from exc
        if not isinstance(document, dict) or not isinstance(document.get("payload"), dict):
            raise PinnedInputChanged(f"Committed artifact {artifact.artifact_id} is invalid")
        expected_path = (
            f"{document.get('job_id')}/{artifact.stage.value}/{artifact.content_sha256}.json"
        )
        if (
            document.get("artifact_version") != 2
            or document.get("stage") != artifact.stage.value
            or document.get("kind") != artifact.kind
            or document.get("pipeline_contract_version")
            != artifact.pipeline_contract_version.value
            or document.get("model_config_sha256") != artifact.model_config_sha256
            or document.get("metadata") != artifact.metadata
            or artifact.artifact_id != f"{artifact.stage.value}:{artifact.content_sha256}"
            or artifact.relative_path != expected_path
        ):
            raise PinnedInputChanged(
                f"Committed artifact {artifact.artifact_id} has invalid provenance"
            )
        if job is not None:
            self._validate_job_provenance(artifact, job)
        return dict(document["payload"])

    @staticmethod
    def _validate_job_provenance(artifact: StageArtifact, job: CurationJob) -> None:
        job_id = str(job.id)
        if not artifact.relative_path.startswith(f"{job_id}/"):
            raise PinnedInputChanged(
                f"Committed artifact {artifact.artifact_id} job provenance changed"
            )
        if artifact.pipeline_contract_version is not job.pipeline_contract_version:
            raise PinnedInputChanged(
                f"Committed artifact {artifact.artifact_id} pipeline contract changed"
            )
        if artifact.model_config_sha256 == job.model_config_sha256:
            return
        if (
            artifact.pipeline_contract_version is PipelineContractVersion.RETRIEVAL_V4
            and job.pipeline_contract_version is PipelineContractVersion.RETRIEVAL_V4
            and artifact.model_config_sha256 == _EMPTY_DOCUMENT_SHA256
        ):
            return
        raise PinnedInputChanged(
            f"Committed artifact {artifact.artifact_id} model configuration changed"
        )


class CurationPipeline:
    def __init__(
        self,
        repository: AnkiCurationRepository,
        artifacts: StageArtifactStore,
        runner: CurationStageRunner,
        *,
        input_validator: CurationInputValidator | None = None,
    ) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.runner = runner
        self.input_validator = input_validator or _AllowPinnedInputs()

    async def run_stage(
        self,
        job_id: UUID,
        *,
        lease_owner: str | None = None,
    ) -> StageRunResult | None:
        job = self.repository.require_job(job_id)
        if job.pipeline_contract_version is not PipelineContractVersion.RETRIEVAL_V4:
            raise UnsupportedPipelineContract(
                f"pipeline contract {job.pipeline_contract_version.value} is "
                "unsupported; upgrade required; no mutation performed"
            )
        definition = stage_definition(job.state, job.pipeline_contract_version)
        if definition is None:
            return None
        started = self.repository.start_stage(
            job_id,
            definition.stage,
            provider=job.provider,
            model=job.model,
        )
        try:
            self.input_validator.validate(job_id)
            prior_artifacts = tuple(self.repository.list_stage_artifacts(job_id))
            prior_payloads = {
                artifact.stage: self.artifacts.read(artifact, job=job)
                for artifact in prior_artifacts
            }
            input_sha256 = _stage_input_hash(
                job,
                definition.stage,
                prior_artifacts,
            )
            product = await self.runner.run(
                StageContext(
                    job=job,
                    stage=definition.stage,
                    input_sha256=input_sha256,
                    prior_artifacts=prior_artifacts,
                    prior_payloads=prior_payloads,
                )
            )
            artifact = self.artifacts.write(
                job_id,
                definition.stage,
                product,
                input_sha256=input_sha256,
                pipeline_contract_version=job.pipeline_contract_version,
                model_config_sha256=job.model_config_sha256,
            )
            target_state = (
                CurationState.FAILED
                if product.blocking_error is not None
                else definition.next_state
            )
            advanced = self.repository.commit_stage(
                job_id,
                expected_state=definition.state,
                target_state=target_state,
                stage=definition.stage,
                artifact=artifact,
                usage=product.usage,
                cache_hits=product.cache_hits,
                lease_owner=lease_owner,
                candidates=product.candidates,
                source_evidence=product.source_evidence,
                gap_cards=product.gap_cards,
                job_pins=product.job_pins,
                failure_detail=product.blocking_error,
            )
        except Exception as exc:
            self.repository.fail_stage(
                job_id,
                definition.stage,
                _safe_error(exc),
            )
            raise
        if started.attempt_count < 1:
            raise AssertionError("stage attempt was not recorded")
        return StageRunResult(
            job_id=job_id,
            stage=definition.stage,
            state=advanced.state,
            artifact=artifact,
        )


def stage_definition(
    state: CurationState,
    version: PipelineContractVersion = PipelineContractVersion.RETRIEVAL_V4,
) -> PipelineStageDefinition | None:
    return {definition.state: definition for definition in pipeline_stages(version)}.get(state)


def _stage_input_hash(
    job: CurationJob,
    stage: CurationStage,
    artifacts: Sequence[StageArtifact],
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "job_id": str(job.id),
                "stage": stage.value,
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
                "prior_artifacts": [
                    {
                        "artifact_id": artifact.artifact_id,
                        "content_sha256": artifact.content_sha256,
                    }
                    for artifact in artifacts
                ],
            }
        ).encode("utf-8")
    ).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _safe_error(error: Exception) -> str:
    return (" ".join(str(error).split()) or type(error).__name__)[:1_000]


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
