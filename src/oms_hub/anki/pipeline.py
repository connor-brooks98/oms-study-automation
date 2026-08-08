import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Protocol, cast
from uuid import UUID

from oms_hub.anki.domain import (
    Candidate,
    CurationJob,
    CurationStage,
    CurationState,
    EvidenceSupport,
    GapCard,
    PipelineContractVersion,
    RetrievalPass,
    SourceEvidence,
    SourceKind,
    SourceReference,
    StageArtifact,
    StageUsage,
)
from oms_hub.anki.orphan_artifacts import OrphanArtifactStore, product_snapshot
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
CARD_CENTRIC_V1_STAGES = (
    PipelineStageDefinition(
        CurationState.PREFLIGHT,
        CurationStage.PREFLIGHT,
        CurationState.BUILDING_SOURCE_INDEX,
    ),
    PipelineStageDefinition(
        CurationState.BUILDING_SOURCE_INDEX,
        CurationStage.SOURCE_INDEX,
        CurationState.CARD_BUILDING_LEDGER,
    ),
    PipelineStageDefinition(
        CurationState.CARD_BUILDING_LEDGER,
        CurationStage.CARD_LEDGER,
        CurationState.CARD_SCOPING_TAGS,
    ),
    PipelineStageDefinition(
        CurationState.CARD_SCOPING_TAGS,
        CurationStage.CARD_TAG_SCOPE,
        CurationState.CARD_CLASSIFYING,
    ),
    PipelineStageDefinition(
        CurationState.CARD_CLASSIFYING,
        CurationStage.CARD_CLASSIFY,
        CurationState.CARD_COVERAGE,
    ),
    PipelineStageDefinition(
        CurationState.CARD_COVERAGE,
        CurationStage.CARD_COVERAGE,
        CurationState.CARD_SWEEPING_RESIDUAL,
    ),
    PipelineStageDefinition(
        CurationState.CARD_SWEEPING_RESIDUAL,
        CurationStage.CARD_RESIDUAL,
        CurationState.CARD_GENERATING_GAPS,
    ),
    PipelineStageDefinition(
        CurationState.CARD_GENERATING_GAPS,
        CurationStage.CARD_GAP_FILL,
        CurationState.CARD_DEDUPING,
    ),
    PipelineStageDefinition(
        CurationState.CARD_DEDUPING,
        CurationStage.DEDUPE,
        CurationState.CARD_SELECTING,
    ),
    PipelineStageDefinition(
        CurationState.CARD_SELECTING,
        CurationStage.CARD_SELECTION,
        CurationState.CARD_RECONCILING,
    ),
    PipelineStageDefinition(
        CurationState.CARD_RECONCILING,
        CurationStage.RECONCILIATION,
        CurationState.READY_FOR_REVIEW,
    ),
)
CARD_CENTRIC_V2_STAGES = (
    PipelineStageDefinition(
        CurationState.PREFLIGHT, CurationStage.PREFLIGHT, CurationState.BUILDING_SOURCE_INDEX
    ),
    PipelineStageDefinition(
        CurationState.BUILDING_SOURCE_INDEX,
        CurationStage.SOURCE_INDEX,
        CurationState.CARD_BUILDING_LEDGER,
    ),
    PipelineStageDefinition(
        CurationState.CARD_BUILDING_LEDGER,
        CurationStage.CARD_LEDGER,
        CurationState.CARD_AUDITING_EVIDENCE,
    ),
    PipelineStageDefinition(
        CurationState.CARD_AUDITING_EVIDENCE,
        CurationStage.CARD_EVIDENCE_AUDIT,
        CurationState.CARD_SCOPING_TAGS,
    ),
    PipelineStageDefinition(
        CurationState.CARD_SCOPING_TAGS,
        CurationStage.CARD_TAG_SCOPE,
        CurationState.CARD_PREFILTERING,
    ),
    PipelineStageDefinition(
        CurationState.CARD_PREFILTERING,
        CurationStage.CARD_PREFILTER,
        CurationState.CARD_FAST_CLASSIFYING,
    ),
    PipelineStageDefinition(
        CurationState.CARD_FAST_CLASSIFYING,
        CurationStage.CARD_FAST_CLASSIFY,
        CurationState.CARD_CLASSIFYING,
    ),
    PipelineStageDefinition(
        CurationState.CARD_CLASSIFYING, CurationStage.CARD_CLASSIFY, CurationState.CARD_COVERAGE
    ),
    PipelineStageDefinition(
        CurationState.CARD_COVERAGE,
        CurationStage.CARD_COVERAGE,
        CurationState.CARD_SWEEPING_RESIDUAL,
    ),
    PipelineStageDefinition(
        CurationState.CARD_SWEEPING_RESIDUAL,
        CurationStage.CARD_RESIDUAL,
        CurationState.CARD_GENERATING_GAPS,
    ),
    PipelineStageDefinition(
        CurationState.CARD_GENERATING_GAPS, CurationStage.CARD_GAP_FILL, CurationState.CARD_DEDUPING
    ),
    PipelineStageDefinition(
        CurationState.CARD_DEDUPING, CurationStage.DEDUPE, CurationState.CARD_SELECTING
    ),
    PipelineStageDefinition(
        CurationState.CARD_SELECTING, CurationStage.CARD_SELECTION, CurationState.CARD_RECONCILING
    ),
    PipelineStageDefinition(
        CurationState.CARD_RECONCILING, CurationStage.RECONCILIATION, CurationState.READY_FOR_REVIEW
    ),
)
_STAGE_BY_STATE = {definition.state: definition for definition in PIPELINE_STAGES}


class UnsupportedPipelineContract(RuntimeError):
    """A stored job requests a graph this Hub cannot execute yet."""


def pipeline_stages(version: PipelineContractVersion) -> tuple[PipelineStageDefinition, ...]:
    if version is PipelineContractVersion.RETRIEVAL_V4:
        return PIPELINE_STAGES
    if version is PipelineContractVersion.CARD_CENTRIC_V1:
        return CARD_CENTRIC_V1_STAGES
    if version is PipelineContractVersion.CARD_CENTRIC_V2:
        return CARD_CENTRIC_V2_STAGES
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
    # I0 hook: S4c/S6/S7/S9 consumers receive this immutable, repository-prepared
    # document through their existing StageContext argument; stages.py remains
    # untouched until its owner consumes the named replay inputs.
    replay_inputs: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    replay_inputs_sha256: str = ""


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


class _PreparedStageReplayInputs(Protocol):
    """Structural P1-A seam; its concrete type intentionally remains repository-owned."""

    job_id: UUID
    stage: CurationStage
    canonical_json: str
    sha256: str

    @property
    def document(self) -> dict[str, Any]: ...


class _ReplayInputRepository(Protocol):
    def prepare_stage_replay_inputs(
        self, job_id: UUID, stage: CurationStage
    ) -> _PreparedStageReplayInputs: ...


@dataclass(frozen=True, slots=True)
class _StageReplayInputs:
    canonical_json: str
    sha256: str
    document: Mapping[str, Any]


class _AllowPinnedInputs:
    def validate(self, job_id: UUID) -> None:
        del job_id


class StageArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.orphans = OrphanArtifactStore(root)

    def write(
        self,
        job_id: UUID,
        stage: CurationStage,
        product: StageProduct,
        *,
        input_sha256: str,
        pipeline_contract_version: PipelineContractVersion = PipelineContractVersion.RETRIEVAL_V4,
        model_config_sha256: str = "",
        replace_exact_invalid_uncommitted: bool = False,
    ) -> StageArtifact:
        if len(input_sha256) != 64:
            raise ValueError("stage input hash is invalid")
        if not product.kind.strip():
            raise ValueError("stage artifact kind cannot be blank")
        document = {
            "artifact_version": 3,
            "job_id": str(job_id),
            "stage": stage.value,
            "kind": product.kind,
            "pipeline_contract_version": pipeline_contract_version.value,
            "model_config_sha256": model_config_sha256,
            "input_sha256": input_sha256,
            "payload": product.payload,
            "metadata": product.metadata,
            "recovery_product": product_snapshot(product),
        }
        encoded = _canonical_json(document).encode("utf-8") + b"\n"
        content_sha256 = hashlib.sha256(encoded).hexdigest()
        relative = Path(str(job_id)) / stage.value / f"{content_sha256}.json"
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        needs_write = not destination.exists()
        if destination.exists():
            existing = destination.read_bytes()
            if existing != encoded:
                if (
                    not replace_exact_invalid_uncommitted
                    or hashlib.sha256(existing).hexdigest() == content_sha256
                ):
                    raise ValueError("immutable stage artifact has conflicting content")
                # The pipeline just confirmed that this exact deterministic
                # content path has no committed stage artifact. The bytes do
                # not match their content-addressed filename, so atomically
                # replacing them is safe and permits a deterministic rerun.
                needs_write = True
        if needs_write:
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
        artifact = StageArtifact(
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
        self.orphans.record_complete(
            artifact,
            job_id=job_id,
            artifact_schema_version=3,
        )
        return artifact

    def recover_orphan(
        self,
        *,
        job: CurationJob,
        stage: CurationStage,
        input_sha256: str,
        committed_artifacts: Sequence[StageArtifact],
    ) -> tuple[StageArtifact, StageProduct] | None:
        recovered = self.orphans.recover(
            job_id=job.id,
            stage=stage,
            input_sha256=input_sha256,
            pipeline_contract_version=job.pipeline_contract_version,
            model_config_sha256=job.model_config_sha256,
            committed_artifacts=committed_artifacts,
        )
        if recovered is None:
            return None
        try:
            return recovered.artifact, _stage_product_from_snapshot(recovered.product)
        except (KeyError, TypeError, ValueError):
            return None

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
        artifact_version = document.get("artifact_version")
        is_v2 = type(artifact_version) is int and artifact_version == 2
        is_v3 = type(artifact_version) is int and artifact_version == 3
        is_migrated_v1 = (
            type(artifact_version) is int
            and artifact_version == 1
            and job is not None
            and artifact.pipeline_contract_version is PipelineContractVersion.RETRIEVAL_V4
            and artifact.model_config_sha256 == _EMPTY_DOCUMENT_SHA256
            and job.pipeline_contract_version is PipelineContractVersion.RETRIEVAL_V4
        )
        if (
            not (is_v2 or is_v3 or is_migrated_v1)
            or document.get("stage") != artifact.stage.value
            or document.get("kind") != artifact.kind
            or document.get("metadata") != artifact.metadata
            or artifact.artifact_id != f"{artifact.stage.value}:{artifact.content_sha256}"
            or artifact.relative_path != expected_path
            or (
                "pipeline_contract_version" in document
                and document["pipeline_contract_version"]
                != artifact.pipeline_contract_version.value
            )
            or (
                "model_config_sha256" in document
                and document["model_config_sha256"] != artifact.model_config_sha256
            )
            or (
                (is_v2 or is_v3)
                and (
                    "pipeline_contract_version" not in document
                    or "model_config_sha256" not in document
                )
            )
            or (is_v3 and document.get("input_sha256") != artifact.input_sha256)
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
        lease_clock: Callable[[], datetime] | None = None,
    ) -> StageRunResult | None:
        job = self.repository.require_job(job_id)
        definition = stage_definition(job.state, job.pipeline_contract_version)
        if definition is None:
            return None
        started = self.repository.start_stage(
            job_id,
            definition.stage,
            provider=job.provider,
            model=job.model,
            expected_state=definition.state,
            lease_owner=lease_owner,
            now=_lease_now(lease_clock),
        )
        try:
            self.input_validator.validate(job_id)
            replay_inputs = _prepare_stage_replay_inputs(
                self.repository,
                job,
                definition.stage,
            )
            committed_artifacts = tuple(self.repository.list_stage_artifacts(job_id))
            prior_artifacts = tuple(
                artifact
                for artifact in committed_artifacts
                if artifact.stage is not definition.stage
            )
            prior_payloads = {
                artifact.stage: self.artifacts.read(artifact, job=job)
                for artifact in prior_artifacts
            }
            input_sha256 = _stage_input_hash(
                job,
                definition.stage,
                prior_artifacts,
                replay_inputs=replay_inputs,
            )
            recovered = self.artifacts.recover_orphan(
                job=job,
                stage=definition.stage,
                input_sha256=input_sha256,
                committed_artifacts=committed_artifacts,
            )
            if recovered is None:
                product = await self.runner.run(
                    StageContext(
                        job=job,
                        stage=definition.stage,
                        input_sha256=input_sha256,
                        prior_artifacts=prior_artifacts,
                        prior_payloads=prior_payloads,
                        replay_inputs=replay_inputs.document,
                        replay_inputs_sha256=replay_inputs.sha256,
                    )
                )
                artifact = self.artifacts.write(
                    job_id,
                    definition.stage,
                    product,
                    input_sha256=input_sha256,
                    pipeline_contract_version=job.pipeline_contract_version,
                    model_config_sha256=job.model_config_sha256,
                    replace_exact_invalid_uncommitted=not any(
                        artifact.stage is definition.stage
                        for artifact in self.repository.list_stage_artifacts(job_id)
                    ),
                )
            else:
                artifact, product = recovered
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
                now=_lease_now(lease_clock),
            )
        except Exception as exc:
            self.repository.fail_stage(
                job_id,
                definition.stage,
                _safe_error(exc),
                expected_state=definition.state,
                lease_owner=lease_owner,
                now=_lease_now(lease_clock),
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
    *,
    replay_inputs: _StageReplayInputs | None = None,
) -> str:
    identity: dict[str, object] = {
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
    # Leaving this key out preserves historical v1/legacy stage hashes exactly.
    if replay_inputs is not None and replay_inputs.sha256:
        identity["prepared_replay_inputs"] = {
            "sha256": replay_inputs.sha256,
            "canonical_json": replay_inputs.canonical_json,
        }
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


def _prepare_stage_replay_inputs(
    repository: AnkiCurationRepository,
    job: CurationJob,
    stage: CurationStage,
) -> _StageReplayInputs:
    # P1-A replay documents are a v2-only identity extension. Legacy and v1
    # jobs retain their exact historical stage hashes even after P1-A is
    # deployed and exposes its repository API.
    if job.pipeline_contract_version is not PipelineContractVersion.CARD_CENTRIC_V2:
        return _StageReplayInputs("", "", MappingProxyType({}))
    prepare = getattr(repository, "prepare_stage_replay_inputs", None)
    if prepare is None:
        raise PinnedInputChanged("card-centric-v2 replay inputs are unavailable")
    prepared = cast(_ReplayInputRepository, repository).prepare_stage_replay_inputs(job.id, stage)
    if prepared.job_id != job.id or prepared.stage is not stage:
        raise PinnedInputChanged("prepared replay inputs are for a different job or stage")
    canonical_json = prepared.canonical_json
    sha256 = prepared.sha256
    if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
        raise PinnedInputChanged("prepared replay inputs have an invalid SHA-256")
    if hashlib.sha256(canonical_json.encode("utf-8")).hexdigest() != sha256:
        raise PinnedInputChanged("prepared replay inputs SHA-256 does not match its document")
    try:
        document = prepared.document
    except json.JSONDecodeError as exc:
        raise PinnedInputChanged("prepared replay inputs are not valid JSON") from exc
    if not isinstance(document, dict) or _canonical_json(document) != canonical_json:
        raise PinnedInputChanged("prepared replay inputs are not canonical JSON")
    return _StageReplayInputs(
        canonical_json,
        sha256,
        _immutable_replay_document(document),
    )


def _immutable_replay_document(value: Mapping[str, Any]) -> Mapping[str, Any]:
    def freeze(item: Any) -> Any:
        if isinstance(item, dict):
            return MappingProxyType({key: freeze(value) for key, value in item.items()})
        if isinstance(item, list):
            return tuple(freeze(value) for value in item)
        return item

    return cast(Mapping[str, Any], freeze(dict(value)))


def _stage_product_from_snapshot(snapshot: Mapping[str, Any]) -> StageProduct:
    kind = _required_str(snapshot, "kind")
    payload = _required_mapping(snapshot, "payload")
    metadata = _required_mapping(snapshot, "metadata")
    cache_hits = snapshot.get("cache_hits")
    blocking_error = snapshot.get("blocking_error")
    if type(cache_hits) is not int or (
        blocking_error is not None and not isinstance(blocking_error, str)
    ):
        raise ValueError("orphan stage product has invalid scalar fields")
    usage = _stage_usage_from_snapshot(snapshot.get("usage"))
    candidates = _candidates_from_snapshot(snapshot.get("candidates"))
    source_evidence = _source_evidence_from_snapshot(snapshot.get("source_evidence"))
    gap_cards = _gap_cards_from_snapshot(snapshot.get("gap_cards"))
    job_pins_raw = _required_mapping(snapshot, "job_pins")
    job_pins = {key: _string_value(value) for key, value in job_pins_raw.items()}
    return StageProduct(
        kind=kind,
        payload=payload,
        metadata=metadata,
        usage=usage,
        cache_hits=cache_hits,
        candidates=candidates,
        source_evidence=source_evidence,
        gap_cards=gap_cards,
        job_pins=job_pins,
        blocking_error=blocking_error,
    )


def _stage_usage_from_snapshot(value: object) -> StageUsage | None:
    if value is None:
        return None
    item = _mapping(value)
    request_id = _required_str(item, "request_id")
    numeric = tuple(item.get(name) for name in ("input_tokens", "output_tokens", "cost_microusd"))
    if any(type(number) is not int for number in numeric):
        raise ValueError("orphan stage usage is invalid")
    return StageUsage(request_id, *cast(tuple[int, int, int], numeric))


def _candidates_from_snapshot(value: object) -> tuple[Candidate, ...] | None:
    if value is None:
        return None
    candidates: list[Candidate] = []
    for raw in _sequence(value):
        item = _mapping(raw)
        note_id = item.get("note_id")
        confidence = item.get("confidence")
        context_trap = item.get("context_trap")
        selected = item.get("selected")
        if (
            type(note_id) is not int
            or type(confidence) not in {int, float}
            or type(context_trap) is not bool
            or type(selected) is not bool
        ):
            raise ValueError("orphan candidate is invalid")
        scores_raw = _required_mapping(item, "scores")
        if any(type(score) not in {int, float} for score in scores_raw.values()):
            raise ValueError("orphan candidate scores are invalid")
        candidates.append(
            Candidate(
                note_id=note_id,
                content_hash=_required_str(item, "content_hash"),
                best_concept_id=_required_str(item, "best_concept_id"),
                provenance=_required_mapping(item, "provenance"),
                scores={key: float(cast(float, score)) for key, score in scores_raw.items()},
                predicted_band=_required_str(item, "predicted_band"),
                verdict=_required_str(item, "verdict"),
                confidence=float(cast(float, confidence)),
                reason=_required_str(item, "reason"),
                context_trap=context_trap,
                recall_direction=_required_str(item, "recall_direction"),
                mnemonic_classification=_required_str(item, "mnemonic_classification"),
                dedupe_disposition=_required_str(item, "dedupe_disposition"),
                selected=selected,
                retrieval_pass=RetrievalPass(_required_str(item, "retrieval_pass")),
            )
        )
    return tuple(candidates)


def _source_evidence_from_snapshot(value: object) -> tuple[SourceEvidence, ...] | None:
    if value is None:
        return None
    evidence: list[SourceEvidence] = []
    for raw in _sequence(value):
        item = _mapping(raw)
        evidence.append(
            SourceEvidence(
                evidence_id=_required_str(item, "evidence_id"),
                concept_id=_required_str(item, "concept_id"),
                support=EvidenceSupport(_required_str(item, "support")),
                statement=_required_str(item, "statement"),
                source_refs=_source_references_from_snapshot(item.get("source_refs")),
                content_hash=_required_str(item, "content_hash"),
            )
        )
    return tuple(evidence)


def _gap_cards_from_snapshot(value: object) -> tuple[GapCard, ...] | None:
    if value is None:
        return None
    cards: list[GapCard] = []
    for raw in _sequence(value):
        item = _mapping(raw)
        revision = item.get("revision")
        selected = item.get("selected")
        source_note_id = item.get("source_note_id")
        if (
            type(revision) is not int
            or type(selected) is not bool
            or (source_note_id is not None and type(source_note_id) is not int)
        ):
            raise ValueError("orphan gap card is invalid")
        cards.append(
            GapCard(
                concept_id=_required_str(item, "concept_id"),
                text=_required_str(item, "text"),
                extra=_required_str(item, "extra"),
                revision=revision,
                selected=selected,
                image_state=_required_str(item, "image_state"),
                media_filename=_optional_str(item.get("media_filename")),
                source_note_id=source_note_id,
                generated_image=_required_mapping(item, "generated_image"),
                validation_state=_required_str(item, "validation_state"),
                source_refs=_source_references_from_snapshot(item.get("source_refs")),
                evidence_ids=tuple(
                    _string_value(entry) for entry in _sequence(item.get("evidence_ids"))
                ),
                provenance=_required_mapping(item, "provenance"),
                initial_tags=tuple(
                    _string_value(entry) for entry in _sequence(item.get("initial_tags"))
                ),
                content_hash=_required_str(item, "content_hash"),
                card_id=_required_str(item, "card_id"),
            )
        )
    return tuple(cards)


def _source_references_from_snapshot(value: object) -> tuple[SourceReference, ...]:
    return tuple(
        SourceReference(
            source_kind=SourceKind(_required_str(item, "source_kind")),
            revision_id=_required_int(item, "revision_id"),
            locator=_required_str(item, "locator"),
            content_hash=_required_str(item, "content_hash"),
        )
        for item in (_mapping(raw) for raw in _sequence(value))
    )


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("orphan field is not a JSON object")
    return dict(value)


def _required_mapping(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    return _mapping(value.get(key))


def _sequence(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("orphan field is not a JSON array")
    return list(value)


def _required_str(value: Mapping[str, Any], key: str) -> str:
    return _string_value(value.get(key))


def _optional_str(value: object) -> str | None:
    return None if value is None else _string_value(value)


def _string_value(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("orphan field is not a string")
    return value


def _required_int(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if type(item) is not int:
        raise ValueError("orphan field is not an integer")
    return item


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _safe_error(error: Exception) -> str:
    return (" ".join(str(error).split()) or type(error).__name__)[:1_000]


def _lease_now(clock: Callable[[], datetime] | None) -> datetime:
    return clock() if clock is not None else datetime.now(UTC)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
