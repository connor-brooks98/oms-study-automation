import asyncio
import hashlib
import json
import math
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from oms_hub.anki.audit import AuditRunResult, CardAuditService
from oms_hub.anki.calibration import (
    calibrated_score,
    canonical_sha256,
    cluster_note_ids,
    deck_and_tag_eligible,
    effective_tag_mode,
    frozen_config_payload,
    pollution_diagnostic,
)
from oms_hub.anki.card_centric import (
    CARD_CENTRIC_UNCONDITIONAL_RESIDUAL_RATE,
    CardCentricClassifier,
    CardCentricLedgerService,
    build_snapshot_census,
    build_source_index,
    evidence_quality_v2,
    fast_selection_eligible_v2,
    scope_cards,
    select_high_yield,
    select_high_yield_v2,
    selection_eligible,
    selection_eligible_v2,
)
from oms_hub.anki.card_centric_contracts import (
    CardCentricSourceIndex,
    CardClassification,
    CardConcept,
    CardConceptLedger,
    CardEvidenceAudit,
    CardGapBatch,
    CardRecord,
    ClassifierResult,
    FastCardClassification,
    FastClassificationResult,
    GeneratedCardResolution,
    QualitySelectionResult,
    SemanticDedupeReview,
    SemanticPreFilterResult,
    SnapshotCensus,
    TagScopeResult,
    serialize_card_centric_ledger,
)
from oms_hub.anki.card_centric_hybrid import CardCentricHybridRetriever, query_variants
from oms_hub.anki.card_centric_review import V3_PHASE_G_SAFETY, V3ReviewSnapshot, reconcile_v3
from oms_hub.anki.classification_v3 import (
    CLASSIFICATION_CANDIDATES_PER_FACT,
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_TOKENS,
    ClassificationInputError,
    R7ClassificationService,
    classify_set_coverage,
    r7_audit_envelope,
    r7_pin_document,
)
from oms_hub.anki.classification_v3 import (
    route_document as r7_route_document,
)
from oms_hub.anki.contracts import canonical_payload_sha256
from oms_hub.anki.convergence import (
    ConvergenceState,
    ExpansionResult,
    ParaphraseExpansionService,
    update_growth,
)
from oms_hub.anki.correction_contracts import (
    WARNING_FLOOR,
    A11HistorySnapshot,
    DuplicateIdentity,
    FactForbiddenClozeMap,
    FactForbiddenClozeTargets,
    GeneratedFactResolution,
    GeneratedResolutionKind,
    PinnedLectureMetadata,
)
from oms_hub.anki.cost_estimator import (
    RESERVED_INPUT_SAFETY_MULTIPLIER,
    CostAuthorizationError,
    FrozenRateTable,
    StageCostSession,
    TokenUsage,
)
from oms_hub.anki.course_policy import CourseCurationPolicy
from oms_hub.anki.dedupe import DeduplicationService, V3DedupeProposal
from oms_hub.anki.domain import (
    Candidate,
    CurationStage,
    EvidenceSupport,
    GapCard,
    PipelineContractVersion,
    ResolvedClassifierExecution,
    ResolvedStageModel,
    RetrievalPass,
    SourceEvidence,
    SourceKind,
    SourceReference,
    StageUsage,
)
from oms_hub.anki.evidence_bundle import (
    CandidateCardFields,
    CandidateEvidenceBundle,
    RetrievalScore,
    SelectedPassage,
)
from oms_hub.anki.fidelity_audit import R2FidelityDiagnostic, audit_fidelity
from oms_hub.anki.gap_generation_v3 import (
    R9GenerationService,
    V3Evidence,
    V3GeneratedCard,
    V3GenerationFact,
    V3GenerationRequest,
    V3UnresolvedFact,
)
from oms_hub.anki.gaps import (
    ExistingGapSupport,
    GapCardProposal,
    GapCardService,
    GapValidationError,
    SupportedGap,
    V2GapGenerationRequest,
    V2GapGenerationResult,
    V2GapGenerationService,
    source_evidence_id,
)
from oms_hub.anki.index import AnkiIndex, CompanionFilters
from oms_hub.anki.judgment import (
    CoverageJudgment,
    JudgmentResult,
    JudgmentService,
    runtime_judgment_from_v2,
)
from oms_hub.anki.lcl import (
    LCLService,
    LectureConcept,
    LectureConceptLedger,
    runtime_ledger_from_v2,
)
from oms_hub.anki.normalize import NormalizedNote
from oms_hub.anki.pipeline import (
    PinnedInputChanged,
    StageContext,
    StageProduct,
)
from oms_hub.anki.prompt_catalog import AnkiPromptCatalogService
from oms_hub.anki.prompts import (
    AnkiPromptLibrary,
    PromptMetadata,
    PromptSynchronizer,
    StaticPromptSynchronizer,
)
from oms_hub.anki.provider_attempts import (
    emit_provider_event,
    finalize_provider_call,
    provider_call_scope,
    provider_cost_reservation,
)
from oms_hub.anki.reconciliation import (
    AuditResolution,
    CardCentricReconciliationInput,
    ConceptResolution,
    GeneratedResolution,
    ReconciliationInput,
    ReconciliationReport,
    _forbidden_cloze_rows,
    reconcile,
    reconcile_card_centric,
    selected_card_centric_coverage,
)
from oms_hub.anki.repository import AnkiCurationRepository
from oms_hub.anki.rescue import (
    RescueLocalization,
    RescueQuery,
    RescueService,
    RescueSupport,
)
from oms_hub.anki.retrieval import (
    RetrievalScope,
    RetrievalService,
    hybrid_rank_fusion,
)
from oms_hub.anki.runtime import AnkiRuntime
from oms_hub.anki.scope_contracts import LectureScope, ScopedConcept, ScopedFact
from oms_hub.anki.scope_service import (
    PinnedScopePrompt,
    ScopeInputError,
    ScopeReuseArtifact,
    ScopeService,
)
from oms_hub.anki.semantic.domain import EmbeddingClient
from oms_hub.anki.semantic.service import SemanticIndexService, normalize_semantic_text
from oms_hub.anki.semantic.store import SemanticSnapshotStore
from oms_hub.anki.source_index import (
    LectureSourceIndex,
    SourceScope,
)
from oms_hub.anki.sources import (
    LectureSourceExtractor,
    OutlineRepository,
    SourceEmphasisEvidence,
    SourcePassage,
    project_source_emphasis_evidence,
)
from oms_hub.anki.v2_contracts import (
    AuditVerdictV2,
    CoverageJudgmentV2,
    LectureConceptLedgerV2,
    MissingFactV2,
)
from oms_hub.document_processing.run_styles import extract_styled_text_run_sidecar
from oms_hub.ingestion.domain import StudyRevision, UploadKind
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.llm.domain import GenerationOptions, ProviderCapabilities, ProviderName
from oms_hub.llm.repository import LLMSettingsRepository
from oms_hub.llm.structured import StructuredOutputError, StructuredTextService

SourceIndexFactory = Callable[[UUID], LectureSourceIndex]


# S6's v2 residual search is intentionally narrower than the census/deck
# universe.  The domain/version are part of the hashed document so an audit
# from another stage or future representation can never be mistaken for this
# eligibility decision.
_CARD_CENTRIC_V2_S6_SEMANTIC_ELIGIBILITY_AUDIT_VERSION = "v1"
_CARD_CENTRIC_V2_S6_SEMANTIC_ELIGIBILITY_AUDIT_DOMAIN = (
    "oms-study-automation:anki:card_centric_v2:s6:semantic_eligibility"
)


def _evidence_audit_tokens(value: str) -> tuple[str, ...]:
    """Normalize visible text into Unicode-aware evidence-audit word tokens."""
    tokens: list[str] = []
    token: list[str] = []
    for character in unicodedata.normalize("NFKC", value).casefold():
        if character.isalnum() or unicodedata.category(character).startswith("M"):
            token.append(character)
        elif token:
            tokens.append("".join(token))
            token = []
    if token:
        tokens.append("".join(token))
    return tuple(tokens)


def _evidence_audit_terms(concept: CardConcept) -> tuple[tuple[str, ...], ...]:
    terms = {
        tokens
        for value in (concept.primary_entity, *concept.aliases)
        if (tokens := _evidence_audit_tokens(value))
    }
    return tuple(sorted(terms, key=lambda value: (-len(value), value)))


def _has_evidence_audit_term(
    passage_text: str,
    terms: Sequence[tuple[str, ...]],
) -> bool:
    tokens = _evidence_audit_tokens(passage_text)
    return any(
        tokens[start : start + len(term)] == term
        for term in terms
        for start in range(len(tokens) - len(term) + 1)
    )


def _normalized_trimmed_character_count(value: str) -> int:
    """Count an NFKC-normalized passage after trimming only its outer whitespace."""
    return len(unicodedata.normalize("NFKC", value).strip())


class PinnedCurationInputValidator:
    def __init__(
        self,
        repository: AnkiCurationRepository,
        revisions: IngestionRepository,
        companion: AnkiIndex,
        semantic_store: SemanticSnapshotStore,
        source_indexes: SourceIndexFactory,
        *,
        outlines: OutlineRepository | None = None,
        semantic_model: str,
        semantic_dimensions: int,
    ) -> None:
        self.repository = repository
        self.revisions = revisions
        self.companion = companion
        self.semantic_store = semantic_store
        self.source_indexes = source_indexes
        self.outlines = outlines
        self.semantic_model = semantic_model
        self.semantic_dimensions = semantic_dimensions

    def validate(self, job_id: UUID) -> None:
        job = self.repository.require_job(job_id)
        if set(job.source_revision_hashes) != set(job.source_revision_ids):
            raise PinnedInputChanged(
                "Selected source revisions are missing immutable hashes; start a new curation job"
            )
        pinned_revisions: dict[int, StudyRevision] = {}
        for revision_id in job.source_revision_ids:
            try:
                revision = self.revisions.get_study_revision(revision_id)
            except KeyError as exc:
                raise PinnedInputChanged(
                    f"Selected source revision {revision_id} is unavailable"
                ) from exc
            if revision.lecture_id != job.lecture_id:
                raise PinnedInputChanged(
                    f"Selected source revision {revision_id} belongs to another lecture"
                )
            if revision.provenance_kind == "imported_cleaned" and not revision.current:
                raise PinnedInputChanged(
                    f"Selected source revision {revision_id} is no longer current"
                )
            if revision_fingerprint(revision) != job.source_revision_hashes[revision_id]:
                raise PinnedInputChanged(
                    f"Selected source revision {revision_id} changed after the job was queued"
                )
            if not revision.immutable_source_path.is_file():
                raise PinnedInputChanged(
                    f"Selected source revision {revision_id} file is unavailable"
                )
            if revision.provenance_kind == "imported_cleaned":
                self._validate_imported_transcript(revision)
            if (
                revision.provenance_kind == "imported_derived"
                or self.revisions.has_imported_derived_audit(revision.id)
            ) and not self.revisions.imported_derived_audit_matches(revision):
                raise PinnedInputChanged("Pinned imported-derived slide provenance changed")
            pinned_revisions[revision_id] = revision

        if job.summary_outline_id is not None:
            if job.summary_outline_sha256 is None or self.outlines is None:
                raise PinnedInputChanged(
                    "The job has an incomplete summary pin; start a new curation job"
                )
            outline = self.outlines.outline(job.summary_outline_id)
            if outline is None:
                raise PinnedInputChanged("Pinned NotebookLM summary is unavailable")
            if outline.lecture_id != job.lecture_id:
                raise PinnedInputChanged("Pinned NotebookLM summary belongs to another lecture")
            if not outline.current:
                raise PinnedInputChanged("Pinned NotebookLM summary is no longer current")
            if (
                outline.sha256 != job.summary_outline_sha256
                or not outline.path.is_file()
                or hashlib.sha256(outline.path.read_bytes()).hexdigest()
                != job.summary_outline_sha256
            ):
                raise PinnedInputChanged(
                    "Pinned NotebookLM summary changed after the job was queued"
                )
            if outline.provenance_kind not in {
                "imported_notebooklm",
                "notebooklm_generated",
            } or (
                outline.provenance_kind == "notebooklm_generated"
                and any(
                    value is not None
                    for value in (
                        outline.import_id,
                        outline.immutable_path,
                        outline.slide_revision_id,
                        outline.slide_sha256,
                        outline.slide_source_sha256,
                        outline.transcript_revision_id,
                        outline.transcript_sha256,
                    )
                )
            ):
                raise PinnedInputChanged("Pinned imported NotebookLM summary provenance changed")
            if outline.provenance_kind == "imported_notebooklm":
                if (
                    outline.import_id is None
                    or outline.immutable_path is None
                    or not outline.immutable_path.is_file()
                    or hashlib.sha256(outline.immutable_path.read_bytes()).hexdigest()
                    != outline.sha256
                    or outline.slide_revision_id is None
                    or outline.transcript_revision_id is None
                    or outline.slide_sha256 is None
                    or outline.slide_source_sha256 is None
                    or outline.transcript_sha256 is None
                ):
                    raise PinnedInputChanged(
                        "Pinned imported NotebookLM summary provenance changed"
                    )
                try:
                    slide = self.revisions.get_study_revision(outline.slide_revision_id)
                    transcript = self.revisions.get_study_revision(outline.transcript_revision_id)
                except KeyError as exc:
                    raise PinnedInputChanged(
                        "Pinned imported NotebookLM summary links changed"
                    ) from exc
                if (
                    slide.lecture_id != job.lecture_id
                    or transcript.lecture_id != job.lecture_id
                    or not slide.current
                    or not transcript.current
                    or slide.kind is not UploadKind.SLIDES
                    or transcript.kind is not UploadKind.TRANSCRIPTS
                    or slide.source_sha256 != outline.slide_source_sha256
                    or slide.derived_sha256 != outline.slide_sha256
                    or transcript.derived_sha256 != outline.transcript_sha256
                    or transcript.provenance_kind != "imported_cleaned"
                    or transcript.import_id != outline.import_id
                    or outline.slide_revision_id not in job.source_revision_ids
                    or outline.transcript_revision_id not in job.source_revision_ids
                ):
                    raise PinnedInputChanged("Pinned imported NotebookLM summary links changed")

        companion_generation = self.companion.snapshot_id()
        if job.companion_generation is None:
            raise PinnedInputChanged(
                "The job has no pinned companion-index generation; start a new curation job"
            )
        if companion_generation != job.companion_generation:
            raise PinnedInputChanged(
                f"Pinned companion generation {job.companion_generation} is no longer active"
            )
        if job.pipeline_contract_version.value in {"retrieval_v4", "card_centric_v2"}:
            semantic = self.semantic_store.load(
                expected_model=self.semantic_model,
                expected_dimensions=self.semantic_dimensions,
            )
            if job.semantic_generation is None:
                raise PinnedInputChanged(
                    "The job has no pinned semantic generation; start a new curation job"
                )
            if str(semantic.manifest.generation) != job.semantic_generation:
                raise PinnedInputChanged(
                    f"Pinned semantic generation {job.semantic_generation} is no longer active"
                )
        if job.source_index_generation is not None:
            try:
                generation = self.source_indexes(job.id).current_generation()
            except (FileNotFoundError, ValueError) as exc:
                raise PinnedInputChanged("The job's lecture source index is unavailable") from exc
            if str(generation) != job.source_index_generation:
                raise PinnedInputChanged(
                    f"Pinned source index generation "
                    f"{job.source_index_generation} is no longer active"
                )

    @staticmethod
    def _validate_imported_transcript(revision: StudyRevision) -> None:
        required_paths = (
            revision.immutable_source_path,
            revision.immutable_derived_path,
            revision.canonical_source_path,
            revision.canonical_derived_path,
        )
        if (
            revision.import_id is None
            or revision.derived_sha256 != revision.source_sha256
            or any(path is None or not path.is_file() for path in required_paths)
        ):
            raise PinnedInputChanged("Pinned imported transcript provenance changed")
        try:
            if any(
                hashlib.sha256(path.read_bytes()).hexdigest() != revision.source_sha256
                for path in required_paths
                if path is not None
            ):
                raise PinnedInputChanged("Pinned imported transcript files changed")
        except OSError as exc:
            raise PinnedInputChanged("Pinned imported transcript files changed") from exc


class CurationServicesRunner:
    def __init__(
        self,
        *,
        runtime: AnkiRuntime,
        repository: AnkiCurationRepository,
        source_extractor: LectureSourceExtractor,
        source_indexes: SourceIndexFactory,
        companion: AnkiIndex,
        semantic: SemanticIndexService,
        structured: StructuredTextService,
        embedder: EmbeddingClient,
        focused_retrieval_limit: int,
        global_retrieval_limit: int,
        llm_settings: LLMSettingsRepository,
        prompts: AnkiPromptCatalogService | None = None,
        prompt_sync: PromptSynchronizer | None = None,
    ) -> None:
        self.runtime = runtime
        self.repository = repository
        self.source_extractor = source_extractor
        self.source_indexes = source_indexes
        self.companion = companion
        self.semantic = semantic
        self.structured = structured
        self.llm_settings = llm_settings
        self.retrieval = RetrievalService(
            companion,
            semantic,
            per_concept_limit=focused_retrieval_limit,
            global_limit=global_retrieval_limit,
        )
        self.embedder = embedder
        self.prompts = prompts or AnkiPromptCatalogService()
        self.prompt_sync = prompt_sync or StaticPromptSynchronizer()

    def _model(self, context: StageContext) -> str:
        pinned = context.job.model.strip()
        if pinned:
            return pinned
        return self.llm_settings.get(_provider(context)).model

    async def run(self, context: StageContext) -> StageProduct:
        if context.stage.value.startswith("v3_"):
            self._require_v3_offline_execution(context)
        handlers = {
            CurationStage.PREFLIGHT: self._preflight,
            CurationStage.SOURCE_INDEX: self._source_index,
            CurationStage.LCL: self._lcl,
            CurationStage.RETRIEVAL_PASS_1: self._retrieval_pass_1,
            CurationStage.JUDGMENT_PASS_1: self._judgment_pass_1,
            CurationStage.RESCUE: self._rescue,
            CurationStage.RETRIEVAL_PASS_2: self._retrieval_pass_2,
            CurationStage.JUDGMENT_PASS_2: self._judgment_pass_2,
            CurationStage.CONVERGENCE_PASS_3: self._convergence_pass_3,
            CurationStage.CONVERGENCE_PASS_4: self._convergence_pass_4,
            CurationStage.CONVERGENCE_PASS_5: self._convergence_pass_5,
            CurationStage.CARD_AUDIT: self._card_audit,
            CurationStage.COVERAGE_RECOMPUTE: self._coverage_recompute,
            CurationStage.DEDUPE: self._dedupe_stage,
            CurationStage.GAPS: self._generate_gaps,
            CurationStage.RECONCILIATION: self._reconciliation_stage,
            CurationStage.CARD_LEDGER: self._card_ledger,
            CurationStage.CARD_EVIDENCE_AUDIT: self._card_evidence_audit,
            CurationStage.CARD_TAG_SCOPE: self._card_tag_scope,
            CurationStage.CARD_PREFILTER: self._card_prefilter,
            CurationStage.CARD_FAST_CLASSIFY: self._card_fast_classify,
            CurationStage.CARD_CLASSIFY: self._card_classify,
            CurationStage.CARD_COVERAGE: self._card_coverage,
            CurationStage.CARD_RESIDUAL: self._card_residual,
            CurationStage.CARD_GAP_FILL: self._card_gap_fill,
            CurationStage.CARD_SELECTION: self._card_selection,
            CurationStage.V3_R0_PREFLIGHT: self._v3_r0_preflight,
            CurationStage.V3_R1_SOURCE_INDEX: self._v3_r1_source_index,
            CurationStage.V3_R2_FIDELITY: self._v3_r2_fidelity,
            CurationStage.V3_R4_INDEX_VERIFICATION: self._v3_r4_index_verification,
            CurationStage.V3_R3_SCOPE: self._v3_r3_scope,
            CurationStage.V3_R5_RETRIEVAL: self._v3_r5_retrieval,
            CurationStage.V3_R6_CALIBRATION: self._v3_r6_calibration,
            CurationStage.V3_R7_CLASSIFICATION: self._v3_r7_classification,
            CurationStage.V3_R8_GAP_CONFIRMATION: self._v3_r8_gap_confirmation,
            CurationStage.V3_R9_GENERATION: self._v3_r9_generation,
            CurationStage.V3_R10_DEDUPE: self._v3_r10_dedupe,
            CurationStage.V3_R11_REVIEW: self._v3_r11_review,
            CurationStage.V3_R12_APPLY: self._v3_r12_apply,
        }
        return await handlers[context.stage](context)

    def _require_v3_offline_execution(self, context: StageContext) -> None:
        clients = (
            getattr(self.structured, "generator", None),
            self.embedder,
            getattr(self.semantic, "embedder", None),
        )
        if getattr(context.job, "offline_replay_only", False):
            allowed = all(
                getattr(client, "offline_replay_only", False) is True for client in clients
            )
        else:
            allows_capture = getattr(self.repository, "allows_v3_live_capture", lambda: False)
            allowed = allows_capture() and all(
                getattr(client, "capture_only", False) is True for client in clients
            )
        if not allowed:
            raise PinnedInputChanged(
                "v3 requires offline-only replay clients or the capture-only live boundary"
            )

    async def _v3_r0_preflight(self, context: StageContext) -> StageProduct:
        if context.job.policy_sha256 is None:
            raise PinnedInputChanged("R0 requires the persisted policy pin")
        policy = self.repository.get_policy_by_sha256(context.job.policy_sha256)
        if context.job.rate_table_document is None:
            raise PinnedInputChanged("R0 requires the persisted frozen rate table")
        table = FrozenRateTable.from_document(context.job.rate_table_document)
        if table.rate_table_sha256 != context.job.rate_table_sha256:
            raise PinnedInputChanged("R0 frozen rate table changed")
        routes = context.job.resolved_model_config
        if not all(
            (
                routes.scope_r3,
                routes.cheap_classify_r7,
                routes.thorough_classify_r7,
                routes.generation_r9,
            )
        ):
            raise PinnedInputChanged("R0 requires complete v3 model routes")
        assert routes.scope_r3 is not None
        assert routes.cheap_classify_r7 is not None
        assert routes.thorough_classify_r7 is not None
        assert routes.generation_r9 is not None
        prompt = (
            AnkiPromptLibrary(self.prompts.bundled_directory)
            .load_many(("card-centric-scope-v3",))
            .prompts[0]
        )
        payload: dict[str, object] = {
            "policy": policy.model_dump(mode="json"),
            "policy_sha256": policy.policy_sha256,
            "policy_revision": policy.revision,
            "ordinary_cost_limit_microusd": policy.ordinary_cost_limit_microusd,
            "hard_stop_cost_limit_microusd": policy.hard_stop_cost_limit_microusd,
            "rate_table": table.document(),
            "rate_table_sha256": table.rate_table_sha256,
            "model_config_sha256": context.job.model_config_sha256,
            "scope_r3": r7_route_document(routes.scope_r3),
            "cheap_classify_r7": r7_route_document(routes.cheap_classify_r7),
            "thorough_classify_r7": r7_route_document(routes.thorough_classify_r7),
            "generation_r9": r7_route_document(routes.generation_r9),
            "r7_classification": r7_pin_document(
                routes.cheap_classify_r7, routes.thorough_classify_r7, table.rate_table_sha256
            ),
            "prompt_snapshot": [
                {
                    "id": prompt.metadata.id,
                    "version": prompt.metadata.version,
                    "content": prompt.content,
                    "content_sha256": prompt.content_sha256,
                    "metadata": prompt.metadata.model_dump(mode="json", by_alias=True),
                }
            ],
            "cost_ledger": [],
            "cost_ledger_sha256": hashlib.sha256(b"[]").hexdigest(),
        }
        payload["artifact_sha256"] = canonical_sha256(payload)
        return StageProduct(kind="card_centric_v3_preflight", payload=payload)

    async def _v3_r1_source_index(self, context: StageContext) -> StageProduct:
        r0 = _payload(context, CurationStage.V3_R0_PREFLIGHT)
        policy = _r3_policy(context, r0)
        if set(context.job.source_revision_hashes) != set(context.job.source_revision_ids):
            raise PinnedInputChanged("R1 source revision pins are incomplete")
        passages = await asyncio.to_thread(
            self.source_extractor.extract,
            context.job.source_revision_ids,
            summary_outline_id=context.job.summary_outline_id,
        )
        slide = [item for item in passages if item.source_kind is SourceKind.SLIDE]
        revisions = [
            self.source_extractor.revisions.get_study_revision(item)
            for item in context.job.source_revision_ids
        ]
        if any(
            context.job.source_revision_hashes[revision.id] != revision_fingerprint(revision)
            for revision in revisions
        ):
            raise PinnedInputChanged("R1 pinned source revision changed")
        slide_revisions = [item for item in revisions if item.kind is UploadKind.SLIDES]
        if len(slide_revisions) != 1 or not slide:
            raise PinnedInputChanged("R1 requires exactly one pinned slide source")
        revision = slide_revisions[0]
        source_id = f"lecture:{context.job.lecture_id}:revision:{revision.id}:slides"
        sidecar = await asyncio.to_thread(
            extract_styled_text_run_sidecar,
            revision.immutable_source_path,
            source_id=source_id,
            source_sha256=revision.source_sha256,
        )
        run_passages = [
            SourcePassage.create(
                revision_id=revision.id,
                lecture_id=context.job.lecture_id,
                artifact_id=revision.upload_item_id,
                source_kind=SourceKind.SLIDE,
                locator=run.locator,
                text=run.text,
                source_id=source_id,
                slide_number=run.slide_number,
            )
            for run in sidecar.runs
            if run.text.strip()
        ]
        emphasis = project_source_emphasis_evidence(sidecar, policy, revision_id=revision.id)
        payload: dict[str, object] = {
            "passages": [
                _passage_payload(item)
                for item in (
                    *run_passages,
                    *(item for item in passages if item.source_kind is not SourceKind.SLIDE),
                )
            ],
            "style_sidecar": sidecar.model_dump(mode="json"),
            "style_source_sha256": sidecar.source_sha256,
            "style_sidecar_sha256": sidecar.sidecar_sha256,
            "emphasis_evidence": [item.model_dump(mode="json") for item in emphasis],
            "cost_ledger": [],
            "cost_ledger_sha256": hashlib.sha256(b"[]").hexdigest(),
        }
        payload["artifact_sha256"] = canonical_sha256(payload)
        return StageProduct(kind="card_centric_v3_source_index", payload=payload)

    async def _v3_r2_fidelity(self, context: StageContext) -> StageProduct:
        from oms_hub.document_processing.run_styles import StyledTextRunSidecar

        r0 = _payload(context, CurationStage.V3_R0_PREFLIGHT)
        r1 = _payload(context, CurationStage.V3_R1_SOURCE_INDEX)
        policy = _r3_policy(context, r0)
        sidecar = StyledTextRunSidecar.model_validate(r1["style_sidecar"])
        diagnostic = audit_fidelity(sidecar, policy, source_passages=_r3_passages(r1))
        payload: dict[str, object] = {
            "fidelity_diagnostic": diagnostic.model_dump(mode="json"),
            "cost_ledger": [],
            "cost_ledger_sha256": hashlib.sha256(b"[]").hexdigest(),
        }
        payload["artifact_sha256"] = canonical_sha256(payload)
        return StageProduct(
            kind="card_centric_v3_fidelity",
            payload=payload,
            blocking_error=None if diagnostic.may_advance else f"R2 fidelity {diagnostic.status}",
        )

    async def _v3_r4_index_verification(self, context: StageContext) -> StageProduct:
        r0 = _payload(context, CurationStage.V3_R0_PREFLIGHT)
        policy = _r3_policy(context, r0)
        eligible = set(
            self.companion.eligible_note_ids(
                CompanionFilters(deck_allowlist=context.job.deck_allowlist)
            )
        )
        notes = tuple(note for note in self.companion.list_notes() if note.note_id in eligible)
        snapshot = self.semantic.store.load(
            expected_model=self.semantic.model,
            expected_dimensions=self.semantic.dimensions,
            expected_generation=context.job.semantic_generation,
        )
        cards = [{"note_id": note.note_id, "content_sha256": note.content_sha256} for note in notes]
        census = build_snapshot_census(
            tuple(_card_record(note) for note in notes),
            deck_allowlist=context.job.deck_allowlist,
            scope_tokens=context.job.tag_allowlist,
            snapshot_id=context.job.companion_generation or context.job.index_snapshot_id,
        )
        semantic = [
            {"note_id": note_id, "semantic_content_sha256": content_hash}
            for note_id, content_hash in zip(
                snapshot.manifest.note_ids, snapshot.manifest.content_hashes, strict=True
            )
            if note_id in eligible
        ]
        manifest = {
            "generation": str(snapshot.manifest.generation),
            "model": snapshot.manifest.model,
            "dimensions": snapshot.manifest.dimensions,
            "matrix_sha256": snapshot.manifest.matrix_sha256,
        }
        payload: dict[str, object] = {
            "kind": "card_centric_v3_index_verification",
            "policy_sha256": policy.policy_sha256,
            "companion_generation": context.job.companion_generation,
            "lexical_generation": context.job.companion_generation,
            "semantic_generation": context.job.semantic_generation,
            "deck_allowlist": list(context.job.deck_allowlist),
            "tag_allowlist": list(context.job.tag_allowlist),
            "card_identities": cards,
            "cards_sha256": canonical_sha256(cards),
            "semantic_identities": semantic,
            "semantic_manifest": manifest,
            "semantic_manifest_sha256": canonical_sha256(manifest),
            "census": census.model_dump(mode="json"),
            "census_sha256": canonical_sha256(census.model_dump(mode="json")),
        }
        payload["verification_sha256"] = canonical_sha256(payload)
        payload["artifact_sha256"] = canonical_sha256(payload)
        return StageProduct(kind="card_centric_v3_index_verification", payload=payload)

    async def _v3_r3_scope(self, context: StageContext) -> StageProduct:
        """Synthetic-only R3 seam; pipeline.py still rejects v3 execution."""
        if context.job.pipeline_contract_version is not PipelineContractVersion.CARD_CENTRIC_V3:
            raise PinnedInputChanged("R3 scope requires the card_centric_v3 contract")
        r0 = _payload(context, CurationStage.V3_R0_PREFLIGHT)
        costs = _v3_cost_session(context, r0)
        r1 = _payload(context, CurationStage.V3_R1_SOURCE_INDEX)
        r2 = _payload(context, CurationStage.V3_R2_FIDELITY)
        policy = _r3_policy(context, r0)
        route = _r3_route(context, r0)
        prompt = _r3_prompt(r0)
        passages = _r3_passages(r1)
        emphasis = _r3_emphasis(r1)
        fidelity = _r3_fidelity(policy, r1, r2, emphasis)
        result = await asyncio.to_thread(
            ScopeService(
                cast(StructuredTextService, _GuardedStructuredService(self.structured, costs, "R3"))
            ).generate_scope,
            policy=policy,
            fidelity=fidelity,
            source_passages=passages,
            emphasis_evidence=emphasis,
            prompt=prompt,
            route=route,
            model_config_sha256=context.job.model_config_sha256,
            existing=_r3_reuse(r0, context.replay_inputs),
            require_v3_provenance=True,
        )
        payload: dict[str, Any] = {
            "scope": result.scope.model_dump(mode="json"),
            "provider_input": result.provider_input,
            "source_bundle": result.source_bundle,
            "source_bundle_sha256": result.source_bundle_sha256,
            "scope_request_sha256": result.scope_request_sha256,
            "prompt_id": result.prompt_id,
            "prompt_version": result.prompt_version,
            "prompt_content_sha256": result.prompt_content_sha256,
            "route": result.route,
            "output_schema_sha256": result.output_schema_sha256,
            "reused": result.reused,
        }
        _seal_v3_costs(costs, payload)
        payload["artifact_sha256"] = canonical_sha256(payload)
        return StageProduct(
            kind="card_centric_v3_scope",
            payload=payload,
            usage=result.usage,
        )

    async def _v3_r5_retrieval(self, context: StageContext) -> StageProduct:
        """Offline-only R5 over the exact R3/R4 pins; R4 stays handler-free."""
        if context.job.pipeline_contract_version is not PipelineContractVersion.CARD_CENTRIC_V3:
            raise PinnedInputChanged("R5 retrieval requires the card_centric_v3 contract")
        r0 = _payload(context, CurationStage.V3_R0_PREFLIGHT)
        costs = _v3_cost_session(context, r0)
        r3 = _payload(context, CurationStage.V3_R3_SCOPE)
        r4 = _v3_r4_verification(
            context, _payload(context, CurationStage.V3_R4_INDEX_VERIFICATION), r0
        )
        try:
            policy = _r3_policy(context, r0)
            scope = LectureScope.model_validate(r3["scope"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PinnedInputChanged("Pinned R5 scope is malformed") from exc
        if scope.policy_sha256 != policy.policy_sha256 or r3.get(
            "scope_sha256", scope.scope_sha256
        ) not in {None, scope.scope_sha256}:
            raise PinnedInputChanged("Pinned R5 scope identity changed")
        if self.companion.snapshot_id() != r4["companion_generation"]:
            raise PinnedInputChanged("current companion generation changed")
        live_ids = self.companion.eligible_note_ids(
            CompanionFilters(deck_allowlist=context.job.deck_allowlist)
        )
        if live_ids != {int(identity["note_id"]) for identity in r4["card_identities"]}:
            raise PinnedInputChanged("current companion card closure changed")
        for identity in r4["card_identities"]:
            note = self.companion.get_note(int(identity["note_id"]))
            if note is None or note.content_sha256 != identity["content_sha256"]:
                raise PinnedInputChanged("current companion card identity changed")
        snapshot = self.semantic.store.load(
            expected_model=self.semantic.model,
            expected_dimensions=self.semantic.dimensions,
            expected_generation=r4["semantic_generation"],
        )
        manifest = r4["semantic_manifest"]
        if (
            str(snapshot.manifest.generation) != manifest["generation"]
            or snapshot.manifest.model != manifest["model"]
            or snapshot.manifest.dimensions != manifest["dimensions"]
            or snapshot.manifest.matrix_sha256 != manifest["matrix_sha256"]
        ):
            raise PinnedInputChanged("current semantic manifest changed")
        live_semantic = [
            {
                "note_id": note_id,
                "semantic_content_sha256": snapshot.manifest.content_hashes[index],
            }
            for index, note_id in enumerate(snapshot.manifest.note_ids)
            if note_id in live_ids
        ]
        if live_semantic != r4["semantic_identities"]:
            raise PinnedInputChanged("current semantic identity closure changed")
        requested = policy.tag_scope_mode
        census = SnapshotCensus.model_validate(r4["census"])
        mode = effective_tag_mode(requested, census_trusted=census.trust.decision == "trusted")
        filters = CompanionFilters(
            deck_allowlist=context.job.deck_allowlist,
            # R5 must retain deck-eligible off-scope rows for R6 pollution audit.
            tag_allowlist=(),
        )
        embedding_client = getattr(self.semantic, "embedder", None)
        semantic = (
            _CostedSemanticService(
                self.semantic,
                _GuardedEmbeddingClient(embedding_client, costs, "R5", self.semantic.model),
            )
            if embedding_client is not None
            else self.semantic
        )
        retriever = CardCentricHybridRetriever(self.companion, semantic)
        config: dict[str, Any] = frozen_config_payload()
        config_sha256 = canonical_sha256(config)
        facts: list[dict[str, Any]] = []
        for concept in scope.concepts:
            for fact in concept.facts:
                variants, trace = query_variants(
                    fact_statement=fact.statement,
                    canonical_statement=concept.canonical_statement,
                    primary_entity=concept.primary_entity,
                    aliases=concept.aliases,
                    exact_terms=concept.exact_terms,
                    professor_policy_basis=concept.professor_policy_basis,
                    retrieval_queries=concept.retrieval_queries,
                    max_variants=int(config["query_variant_limit"]),
                    max_characters=int(config["query_character_limit"]),
                )
                with provider_call_scope(batch_index=len(facts), batch_note_ids=()):
                    cards = await retriever.retrieve(
                        variants=variants,
                        exact_terms=concept.exact_terms,
                        filters=filters,
                        expected_generation=r4["semantic_generation"],
                        variant_weights=cast(Sequence[float], config["semantic_variant_weights"]),
                        semantic_eligible_note_ids={
                            int(identity["note_id"]) for identity in r4["semantic_identities"]
                        },
                        raw_limit=int(config["raw_limit"]),
                        rrf_k=int(config["rrf_k"]),
                        boost_weights=cast(Mapping[str, float], config["boost_parameters"]),
                        lecture_tag_prefix=(
                            context.job.tag_allowlist[0] if context.job.tag_allowlist else None
                        )
                        if mode == "prior_boost"
                        else None,
                    )
                candidates: list[dict[str, Any]] = []
                for card in cards:
                    category = census.mapping.get(card.note_id)
                    if category is None:
                        raise PinnedInputChanged("R5 raw candidate escapes pinned census")
                    candidates.append(
                        {
                            "note_id": card.note_id,
                            "content_sha256": card.content_sha256,
                            "text": card.text,
                            "extra": card.extra,
                            "tags": list(card.tags),
                            "decks": list(card.decks),
                            "semantic_score": card.semantic_score,
                            "semantic_variant_scores_raw": card.semantic_variant_scores,
                            "variant_ranks": card.fusion.semantic_variant_ranks,
                            "semantic_variant_scores": card.fusion.semantic_variant_scores,
                            "semantic_rank": card.fusion.aggregate_semantic_rank,
                            "lexical_rank": card.fusion.lexical_rank,
                            "base_rrf": card.fusion.base_rrf,
                            "boost_total": card.boost_total,
                            "exact_match_reasons": list(card.exact_match_reasons),
                        }
                    )
                fact_payload: dict[str, Any] = {
                    "concept_id": concept.concept_id,
                    "fact_id": fact.fact_id,
                    "variants": list(variants),
                    "query_trace": list(trace),
                    "raw_semantic": [list(items) for items in retriever.last_semantic_trace],
                    "raw_lexical": list(retriever.last_lexical_trace),
                    "candidates": candidates,
                }
                fact_payload["query_sha256"] = canonical_sha256(fact_payload)
                fact_payload["fact_sha256"] = canonical_sha256(fact_payload)
                facts.append(fact_payload)
        payload = {
            "policy_sha256": policy.policy_sha256,
            "scope_sha256": scope.scope_sha256,
            "r4_verification_sha256": r4["verification_sha256"],
            "semantic_generation": r4["semantic_generation"],
            "requested_tag_mode": requested,
            "effective_tag_mode": mode,
            "config": config,
            "config_sha256": config_sha256,
            "facts": facts,
        }
        _seal_v3_costs(costs, payload)
        payload["artifact_sha256"] = canonical_sha256(payload)
        return StageProduct(kind="card_centric_v3_retrieval", payload=payload)

    async def _v3_r6_calibration(self, context: StageContext) -> StageProduct:
        if context.job.pipeline_contract_version is not PipelineContractVersion.CARD_CENTRIC_V3:
            raise PinnedInputChanged("R6 calibration requires the card_centric_v3 contract")
        r5 = _payload(context, CurationStage.V3_R5_RETRIEVAL)
        costs = _v3_cost_session(context, _payload(context, CurationStage.V3_R0_PREFLIGHT))
        r4 = _v3_r4_verification(
            context,
            _payload(context, CurationStage.V3_R4_INDEX_VERIFICATION),
            _payload(context, CurationStage.V3_R0_PREFLIGHT),
        )
        if r5.get("semantic_generation") != r4["semantic_generation"] or r5.get(
            "artifact_sha256"
        ) != canonical_sha256(
            {key: value for key, value in r5.items() if key != "artifact_sha256"}
        ):
            raise PinnedInputChanged("Pinned R5 retrieval identity changed")
        if r5.get("config_sha256") != canonical_sha256(r5.get("config")):
            raise PinnedInputChanged("Pinned R5 calibration configuration changed")
        if r5.get("config") != frozen_config_payload():
            raise PinnedInputChanged("Pinned R5 calibration configuration is not frozen")
        config = cast(Mapping[str, Any], r5["config"])
        if float(config["rrf_floor"]) != 1 / (int(config["rrf_k"]) + int(config["raw_limit"])):
            raise PinnedInputChanged("Pinned R5 RRF floor is inconsistent")
        if config.get("tag_mode_version") != 1:
            raise PinnedInputChanged("Pinned R5 tag semantics version is unsupported")
        identities = {
            int(item["note_id"]): str(item["semantic_content_sha256"])
            for item in r4["semantic_identities"]
        }
        rows = [row for fact in r5.get("facts", []) for row in fact.get("candidates", [])]
        card_identities = {
            int(item["note_id"]): str(item["content_sha256"]) for item in r4["card_identities"]
        }
        if any(card_identities.get(int(row["note_id"])) != row["content_sha256"] for row in rows):
            raise PinnedInputChanged("Pinned R5 candidate card identity changed")
        census = SnapshotCensus.model_validate(r4["census"])
        records: list[dict[str, Any]] = []
        for fact_payload in r5.get("facts", []):
            candidates = list(fact_payload.get("candidates", []))
            by_id = {int(row["note_id"]): row for row in candidates}
            diagnostics: list[dict[str, Any]] = []
            weights: dict[str, float] = {}
            for index, hits in enumerate(fact_payload.get("raw_semantic", []), start=1):
                variant = f"variant_{index}"
                diagnostic = pollution_diagnostic(
                    [
                        {
                            "semantic_score": hit["score"],
                            "in_scope": census.mapping.get(int(hit["note_id"])) == "target_tagged",
                            "deck": sorted(by_id.get(int(hit["note_id"]), {}).get("decks", []))[0]
                            if by_id.get(int(hit["note_id"]), {}).get("decks", [])
                            else "",
                            "tag_root": (
                                sorted(by_id.get(int(hit["note_id"]), {}).get("tags", []))[0].split(
                                    "::"
                                )[0]
                                if by_id.get(int(hit["note_id"]), {}).get("tags", [])
                                else "<untagged>"
                            ),
                        }
                        for hit in hits
                    ],
                    threshold=float(config["semantic_threshold"]),
                    ceiling=int(config["pollution_ceiling"]),
                    ratio_limit=float(config["pollution_ratio"]),
                )
                weights[variant] = (
                    0.0
                    if diagnostic.polluted
                    else float(
                        config["semantic_variant_weights"][
                            min(index - 1, len(config["semantic_variant_weights"]) - 1)
                        ]
                    )
                )
                diagnostics.append(
                    {
                        "variant": variant,
                        "raw_semantic_hit_count": len(hits),
                        "raw_lexical_hit_count": len(fact_payload.get("raw_lexical", [])),
                        "above_threshold_count": diagnostic.above_threshold_count,
                        "off_scope_count": diagnostic.off_scope_count,
                        "ratio": diagnostic.ratio,
                        "polluted": diagnostic.polluted,
                        "dominant_pattern": diagnostic.dominant_pattern,
                        "semantic_lane_weight": weights[variant],
                    }
                )
            rankings = {
                variant: tuple(ranks.get(rank) for rank in range(1, max(ranks, default=0) + 1))
                for variant in weights
                for ranks in (
                    {
                        int(row["variant_ranks"][variant]): int(row["note_id"])
                        for row in candidates
                        if (
                            variant in row["variant_ranks"]
                            and weights[variant] > 0
                            and float(row["semantic_variant_scores_raw"].get(variant, -1))
                            >= float(config["semantic_threshold"])
                        )
                    },
                )
            }
            lexical = tuple(
                note_id
                for _rank, note_id in sorted(
                    (int(row["lexical_rank"]), int(row["note_id"]))
                    for row in candidates
                    if row["lexical_rank"] is not None
                )
            )
            fused = {
                row.note_id: row
                for row in hybrid_rank_fusion(
                    rankings,
                    lexical,
                    variant_weights=weights,
                    rrf_k=int(config["rrf_k"]),
                )
            }
            for query_diagnostic in diagnostics:
                semantic_note_ids = {
                    note_id
                    for note_id, row in fused.items()
                    if query_diagnostic["variant"] in row.semantic_variant_ranks
                }
                exact_lexical_note_ids = {
                    int(row["note_id"])
                    for row in candidates
                    if row["lexical_rank"] is not None and row["exact_match_reasons"]
                }
                query_diagnostic["fused_candidate_count"] = len(
                    semantic_note_ids | exact_lexical_note_ids
                )
                query_diagnostic["exact_only"] = (
                    bool(exact_lexical_note_ids) and not semantic_note_ids
                )
                query_diagnostic["semantic_only"] = (
                    bool(semantic_note_ids) and not exact_lexical_note_ids
                )
            clean_semantic_note_ids = {
                note_id
                for query_diagnostic in diagnostics
                if not query_diagnostic["polluted"]
                for note_id, fused_row in fused.items()
                if query_diagnostic["variant"] in fused_row.semantic_variant_ranks
            }
            admitted: list[dict[str, Any]] = []
            for row in candidates:
                if not deck_and_tag_eligible(
                    census.mapping.get(int(row["note_id"]), "deck_excluded"),
                    mode=str(r5["effective_tag_mode"]),
                ):
                    continue
                fusion = fused.get(int(row["note_id"]))
                if fusion is None:
                    continue
                exact = bool(row["exact_match_reasons"])
                clean_semantic = int(row["note_id"]) in clean_semantic_note_ids
                score, disposition = calibrated_score(
                    base_rrf=fusion.base_rrf,
                    boost=float(row["boost_total"]),
                    semantic_score=max(
                        (
                            float(score)
                            for variant, score in row["semantic_variant_scores_raw"].items()
                            if weights.get(variant, 0) > 0
                            and float(score) >= float(config["semantic_threshold"])
                        ),
                        default=None,
                    ),
                    exact_match=exact,
                    polluted=False,
                    threshold=float(config["semantic_threshold"]),
                )
                if (not exact and not clean_semantic and fusion.lexical_rank is None) or (
                    not exact and score < float(config["rrf_floor"])
                ):
                    continue
                admitted.append(
                    {
                        **row,
                        "semantic_rank": fusion.aggregate_semantic_rank,
                        "lexical_rank": fusion.lexical_rank,
                        "base_rrf": fusion.base_rrf,
                        "calibrated_score": score,
                        "disposition": disposition,
                        "clean_semantic_lane": clean_semantic,
                    }
                )
            admitted.sort(key=lambda row: (-float(row["calibrated_score"]), int(row["note_id"])))
            retained = admitted[: int(config["per_fact_limit"])]
            records.append(
                {
                    "concept_id": fact_payload["concept_id"],
                    "fact_id": fact_payload["fact_id"],
                    "query_diagnostics": diagnostics,
                    "exact_only": bool(retained)
                    and all(row["semantic_rank"] is None for row in retained),
                    "semantic_only": bool(retained)
                    and all(row["lexical_rank"] is None for row in retained),
                    "all_candidates": retained,
                    "per_fact_cap_excluded_note_ids": [
                        int(row["note_id"]) for row in admitted[int(config["per_fact_limit"]) :]
                    ],
                    "per_fact_cap_exclusions": [
                        {"note_id": int(row["note_id"]), "reason": "per_fact_cap"}
                        for row in admitted[int(config["per_fact_limit"]) :]
                    ],
                    "global_cap_excluded_note_ids": [],
                    "global_cap_exclusions": [],
                }
            )
        globally_ranked = sorted(
            (row for record in records for row in record["all_candidates"]),
            key=lambda row: (-float(row["calibrated_score"]), int(row["note_id"])),
        )
        allowed: set[int] = set()
        for row in globally_ranked:
            if len(allowed) == int(config["global_unique_limit"]):
                break
            allowed.add(int(row["note_id"]))
        for record in records:
            record["global_cap_excluded_note_ids"] = [
                int(row["note_id"])
                for row in record["all_candidates"]
                if int(row["note_id"]) not in allowed
            ]
            record["global_cap_exclusions"] = [
                {"note_id": note_id, "reason": "global_unique_cap"}
                for note_id in record["global_cap_excluded_note_ids"]
            ]
            record["all_candidates"] = [
                row for row in record["all_candidates"] if int(row["note_id"]) in allowed
            ]
        covered = sorted(
            {
                int(row["note_id"])
                for record in records
                for row in record["all_candidates"]
                if int(row["note_id"]) in identities
            }
        )
        vectors = (
            await self.semantic.pinned_document_vectors(
                note_ids=covered, expected_generation=r4["semantic_generation"]
            )
            if covered
            else {}
        )
        for record in records:
            candidates = record["all_candidates"]
            groups = cluster_note_ids(
                candidates,
                vectors=cast(Mapping[int, Sequence[float]], vectors),
                cosine_threshold=float(config["cosine_cluster_threshold"]),
            )
            candidate_by_id = {int(row["note_id"]): row for row in candidates}
            record["clusters"] = [
                {
                    "representative_note_id": min(
                        siblings,
                        key=lambda note_id: (
                            -float(candidate_by_id[note_id]["calibrated_score"]),
                            note_id,
                        ),
                    ),
                    "sibling_note_ids": list(siblings),
                    "missing_vector_note_ids": [
                        note_id for note_id in siblings if note_id not in vectors
                    ],
                }
                for siblings in groups
            ]
            record["fact_sha256"] = canonical_sha256(record)
        payload = {
            "policy_sha256": r5.get("policy_sha256"),
            "scope_sha256": r5.get("scope_sha256"),
            "r5_artifact_sha256": r5["artifact_sha256"],
            "config_sha256": r5["config_sha256"],
            "semantic_generation": r4["semantic_generation"],
            "records": records,
        }
        _seal_v3_costs(costs, payload)
        payload["artifact_sha256"] = canonical_sha256(payload)
        return StageProduct(kind="card_centric_v3_calibration", payload=payload)

    async def _v3_r7_classification(self, context: StageContext) -> StageProduct:
        """Classify R6 representatives only; v3 pipeline creation stays fail-closed."""
        if context.job.pipeline_contract_version is not PipelineContractVersion.CARD_CENTRIC_V3:
            raise PinnedInputChanged("R7 classification requires the card_centric_v3 contract")
        r0 = _payload(context, CurationStage.V3_R0_PREFLIGHT)
        costs = _v3_cost_session(context, r0)
        r3 = _payload(context, CurationStage.V3_R3_SCOPE)
        r5 = _payload(context, CurationStage.V3_R5_RETRIEVAL)
        r6 = _payload(context, CurationStage.V3_R6_CALIBRATION)
        policy = _r3_policy(context, r0)
        try:
            scope = LectureScope.model_validate(r3["scope"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PinnedInputChanged("Pinned R7 scope is malformed") from exc
        if (
            scope.policy_sha256 != policy.policy_sha256
            or r6.get("policy_sha256") != policy.policy_sha256
            or r6.get("scope_sha256") != scope.scope_sha256
        ):
            raise PinnedInputChanged("Pinned R7 policy/scope identity changed")
        if r6.get("artifact_sha256") != canonical_sha256(
            {key: value for key, value in r6.items() if key != "artifact_sha256"}
        ):
            raise PinnedInputChanged("Pinned R6 calibration identity changed")
        if (
            r6.get("r5_artifact_sha256") != r5.get("artifact_sha256")
            or r5.get("policy_sha256") != policy.policy_sha256
            or r5.get("scope_sha256") != scope.scope_sha256
            or r5.get("artifact_sha256")
            != canonical_sha256(
                {key: value for key, value in r5.items() if key != "artifact_sha256"}
            )
        ):
            raise PinnedInputChanged("Pinned R5/R6 provenance changed")
        cheap, thorough, rate_table_sha256 = _r7_routes(context, r0)
        try:
            bundles = _v3_r7_bundles(scope, r3, r6, policy.policy_sha256)
        except PinnedInputChanged as exc:
            payload = r7_audit_envelope(
                cheap,
                thorough,
                rate_table_sha256,
                blocking=True,
                partial_diagnostics=(str(exc),),
            )
            payload = {
                "policy_sha256": policy.policy_sha256,
                "scope_sha256": scope.scope_sha256,
                "r6_artifact_sha256": r6["artifact_sha256"],
                **payload,
            }
            _seal_v3_costs(costs, payload)
            payload["artifact_sha256"] = canonical_sha256(payload)
            return StageProduct(
                kind="card_centric_v3_classification",
                payload=payload,
                blocking_error=str(exc),
            )
        result = await asyncio.to_thread(
            R7ClassificationService(
                cast(StructuredTextService, _GuardedStructuredService(self.structured, costs, "R7"))
            ).classify,
            bundles=bundles,
            strictness=policy.classification_strictness,
            cheap_route=cheap,
            thorough_route=thorough,
            repair_authorization=r0.get("r7_repair_authorization"),
            rate_table_sha256=rate_table_sha256,
            ordinary_limit_microusd=policy.ordinary_cost_limit_microusd,
            hard_limit_microusd=policy.hard_stop_cost_limit_microusd,
            defer_partial=True,
        )
        payload = {
            "policy_sha256": policy.policy_sha256,
            "scope_sha256": scope.scope_sha256,
            "r6_artifact_sha256": r6["artifact_sha256"],
            **result.payload,
        }
        payload.pop("artifact_sha256", None)
        _seal_v3_costs(costs, payload)
        payload["artifact_sha256"] = canonical_sha256(payload)
        return StageProduct(
            kind="card_centric_v3_classification",
            payload=payload,
            usage=result.usage,
            blocking_error=result.blocking_error,
        )

    async def _v3_r8_gap_confirmation(self, context: StageContext) -> StageProduct:
        """Confirm gaps from retained R5 traces only; R8 never searches again."""
        # Preserve the prior manual-seam behavior when Phase D's required R4
        # artifact is absent; a real R8 dispatch always has this pin.
        if CurationStage.V3_R4_INDEX_VERIFICATION not in context.prior_payloads:
            raise KeyError(context.stage)
        r0, scope, r4, r5, r6, r7 = _v3_phase_f_inputs(context)
        costs = _v3_cost_session(context, r0)
        policy = _r3_policy(context, r0)
        if r7.get("blocking") or r7.get("blocking_error"):
            return _v3_blocked_product(
                "card_centric_v3_gap_confirmation", r7, "R7 is blocking", costs=costs
            )
        initial_rows = _v3_r7_rows(r7)
        initial_by_fact: dict[str, list[Mapping[str, Any]]] = {}
        initial_ids_by_fact: dict[str, set[int]] = {}
        for row in initial_rows:
            bundle_id = row.get("bundle_id")
            if not isinstance(bundle_id, str):
                raise PinnedInputChanged("Pinned R7 final partition is malformed")
            fact_id = _v3_bundle_fact_id(r7, bundle_id)
            initial_by_fact.setdefault(fact_id, []).append(row)
            initial_ids_by_fact.setdefault(fact_id, set()).add(
                _v3_bundle_candidate_note_id(r7, bundle_id)
            )
        r5_by_fact = _v3_records_by_fact(r5, "facts")
        r6_by_fact = _v3_records_by_fact(r6, "records")
        source_evidence = _v3_scope_evidence(scope, _payload(context, CurationStage.V3_R3_SCOPE))
        expected_ids = {
            int(item["note_id"]): str(item["content_sha256"]) for item in r4["card_identities"]
        }
        semantic_ids = {
            int(item["note_id"]): str(item["semantic_content_sha256"])
            for item in r4["semantic_identities"]
        }
        semantic_coverage_incomplete = bool(set(expected_ids) - set(semantic_ids))
        threshold = float(r5["config"]["semantic_threshold"])
        raw_limit = int(r5["config"]["raw_limit"])
        records: list[dict[str, Any]] = []
        residual_bundles: list[CandidateEvidenceBundle] = []
        diagnostics_by_fact: dict[str, list[str]] = {}
        for concept in scope.concepts:
            for fact in concept.facts:
                r5_fact, r6_fact = r5_by_fact[fact.fact_id], r6_by_fact[fact.fact_id]
                diagnostics = _v3_r8_raw_safety(
                    r5_fact, r6_fact, expected_ids, semantic_ids, threshold, raw_limit
                )
                diagnostics_by_fact[fact.fact_id] = diagnostics
                initial = initial_by_fact.get(fact.fact_id, [])
                initial_ids = initial_ids_by_fact.get(fact.fact_id, set())
                if _v3_positive_initial(initial):
                    state, reason = "covered_initial", "terminal initial support"
                else:
                    candidates_by_id = {
                        int(candidate["note_id"]): candidate
                        for candidate in r6_fact["all_candidates"]
                    }
                    remaining_candidates: list[Mapping[str, Any]] = []
                    seen_content: set[str] = set()
                    for cluster in r6_fact["clusters"]:
                        representative = cluster.get("representative_note_id")
                        if (
                            type(representative) is not int
                            or representative not in candidates_by_id
                        ):
                            raise PinnedInputChanged("Pinned R6 residual cluster is malformed")
                        for sibling in (representative, *cluster["sibling_note_ids"]):
                            if type(sibling) is not int or sibling not in candidates_by_id:
                                raise PinnedInputChanged("Pinned R6 residual sibling is malformed")
                            content_sha256 = candidates_by_id[sibling].get("content_sha256")
                            if (
                                not isinstance(content_sha256, str)
                                or expected_ids.get(sibling) != content_sha256
                            ):
                                raise PinnedInputChanged("Pinned R6 residual identity is malformed")
                            if content_sha256 in seen_content:
                                continue
                            seen_content.add(content_sha256)
                            remaining_candidates.append(candidates_by_id[sibling])
                    candidates = remaining_candidates
                    if not candidates and not diagnostics:
                        candidates = [
                            candidate
                            for candidate in r5_fact["candidates"]
                            if int(candidate["note_id"]) not in candidates_by_id
                            and _v3_residual_qualifies(candidate, threshold)
                        ]
                    for candidate in candidates:
                        residual_bundles.append(
                            _v3_residual_bundle_from_fact(
                                concept,
                                fact,
                                source_evidence,
                                policy.policy_sha256,
                                scope.scope_sha256,
                                candidate,
                            )
                        )
                    state, reason = "pending_residual", "no terminal initial coverage"
                records.append(
                    {
                        "concept_id": concept.concept_id,
                        "fact_id": fact.fact_id,
                        "generation_allowed": fact.generation_allowed,
                        "state": state,
                        "reason": reason,
                        "initial_note_ids": sorted(initial_ids),
                        "residual_candidate_note_ids": [],
                    }
                )
        residual_rows: list[dict[str, object]] = []
        set_coverage: dict[str, object] = {}
        residual_error: str | None = None
        residual_usage: StageUsage | None = None
        if residual_bundles:
            _cheap, thorough, _rate_table_sha256 = _r7_routes(context, r0)
            result = await asyncio.to_thread(
                classify_set_coverage,
                cast(
                    StructuredTextService,
                    _GuardedStructuredService(self.structured, costs, "R8"),
                ),
                bundles=tuple(
                    sorted(
                        residual_bundles,
                        key=lambda item: (
                            item.concept.concept_id,
                            item.fact_id,
                            item.candidate.note_id,
                        ),
                    )
                ),
                strictness=policy.classification_strictness,
                route=thorough,
            )
            residual_rows = list(cast(list[dict[str, object]], result.payload["final_partition"]))
            set_coverage = dict(result.payload)
            residual_error = result.blocking_error or (
                "R8 set coverage is blocking" if result.payload["blocking"] else None
            )
            residual_usage = result.usage
        by_fact_residual: dict[str, list[dict[str, object]]] = {}
        by_bundle = {bundle.bundle_id: bundle for bundle in residual_bundles}
        for row in residual_rows:
            bundle = by_bundle.get(cast(str, row.get("bundle_id")))
            if bundle is None:
                raise PinnedInputChanged("R8 residual classifier escapes requested bundles")
            by_fact_residual.setdefault(bundle.fact_id, []).append(row)
        for record in records:
            if record["state"] != "pending_residual":
                continue
            rows = by_fact_residual.get(cast(str, record["fact_id"]), [])
            record["residual_candidate_note_ids"] = sorted(
                bundle.candidate.note_id
                for bundle in residual_bundles
                if bundle.fact_id == record["fact_id"]
            )
            if residual_error:
                record["state"], record["reason"] = "unresolved", residual_error
            elif _v3_positive_initial(rows):
                record["state"], record["reason"] = "covered_residual", "terminal residual support"
            elif any(row.get("disposition") == "unresolved" for row in rows):
                record["state"], record["reason"] = (
                    "unresolved",
                    "residual classification unresolved",
                )
            elif diagnostics := diagnostics_by_fact[cast(str, record["fact_id"])]:
                record["state"], record["reason"] = "unresolved", "; ".join(diagnostics)
            elif semantic_coverage_incomplete:
                record["state"], record["reason"] = (
                    "unresolved",
                    "R4 semantic coverage is incomplete",
                )
            else:
                record["state"], record["reason"] = (
                    "confirmed_missing",
                    "no qualifying existing card",
                )
        payload = {
            "policy_sha256": policy.policy_sha256,
            "scope_sha256": scope.scope_sha256,
            "r4_verification_sha256": r4["verification_sha256"],
            "r5_artifact_sha256": r5["artifact_sha256"],
            "r6_artifact_sha256": r6["artifact_sha256"],
            "r7_artifact_sha256": r7["artifact_sha256"],
            "records": records,
            "residual_r7": {
                "bundles": [item.model_dump(mode="json") for item in residual_bundles],
                "final_partition": residual_rows,
                "set_coverage": set_coverage,
            },
        }
        _seal_v3_costs(costs, payload)
        payload["artifact_sha256"] = canonical_sha256(payload)
        return StageProduct(
            kind="card_centric_v3_gap_confirmation",
            payload=payload,
            usage=residual_usage,
            blocking_error=residual_error,
        )

    async def _v3_r9_generation(self, context: StageContext) -> StageProduct:
        r0, scope, r4, r5, r6, r7 = _v3_phase_f_inputs(context)
        costs = _v3_cost_session(context, r0)
        r8 = _payload(context, CurationStage.V3_R8_GAP_CONFIRMATION)
        _v3_artifact_valid(r8, "Pinned R8 gap confirmation identity changed")
        policy = _r3_policy(context, r0)
        if (
            r8.get("policy_sha256") != policy.policy_sha256
            or r8.get("scope_sha256") != scope.scope_sha256
            or r8.get("r4_verification_sha256") != r4["verification_sha256"]
            or r8.get("r5_artifact_sha256") != r5["artifact_sha256"]
            or r8.get("r6_artifact_sha256") != r6["artifact_sha256"]
            or r8.get("r7_artifact_sha256") != r7["artifact_sha256"]
        ):
            raise PinnedInputChanged("Pinned R8 closure changed")
        route = context.job.resolved_model_config.generation_r9
        if route is None or r0.get("generation_r9") != r7_route_document(route):
            raise PinnedInputChanged("Pinned R9 generation route is unavailable")
        evidence = _v3_scope_evidence(scope, _payload(context, CurationStage.V3_R3_SCOPE))
        facts = {fact.fact_id: fact for concept in scope.concepts for fact in concept.facts}
        requested = [
            record
            for record in _v3_r8_records(scope, r8)
            if record.get("state") == "confirmed_missing"
        ]
        requests: list[V3GenerationRequest] = []
        disabled: list[V3UnresolvedFact] = []
        for record in requested:
            fact = facts.get(cast(str, record.get("fact_id")))
            if fact is None or record.get("generation_allowed") is not fact.generation_allowed:
                raise PinnedInputChanged("Pinned R8 fact closure changed")
            projection = V3GenerationFact(
                fact_id=fact.fact_id,
                statement=fact.statement,
                evidence=tuple(
                    V3Evidence(evidence_id=item, text=evidence[item]) for item in fact.evidence_ids
                ),
                forbidden_cloze_targets=fact.forbidden_cloze_targets,
                generation_allowed=fact.generation_allowed,
            )
            if not fact.generation_allowed:
                disabled.append(
                    V3UnresolvedFact(
                        fact_id=fact.fact_id, reason="generation disabled by scoped fact"
                    )
                )
                continue
            trial = V3GenerationRequest(
                policy_sha256=policy.policy_sha256,
                scope_sha256=scope.scope_sha256,
                style_profile=policy.generation_style_profile,
                facts=(projection,),
            )
            if requests:
                try:
                    combined = requests[-1].model_copy(
                        update={"facts": (*requests[-1].facts, projection)}
                    )
                    requests[-1] = V3GenerationRequest.model_validate(
                        combined.model_dump(mode="json")
                    )
                    continue
                except ValueError:
                    pass
            requests.append(trial)
        resolutions: list[dict[str, object]] = [item.model_dump(mode="json") for item in disabled]
        calls: list[dict[str, object]] = []
        usages: list[StageUsage] = []
        blocking_errors: list[str] = []
        service = R9GenerationService(
            cast(StructuredTextService, _GuardedStructuredService(self.structured, costs, "R9"))
        )
        for batch_index, request in enumerate(requests):
            result, usage = await asyncio.to_thread(
                service.generate,
                request,
                route=route,
                repair_authorization=r0.get("r9_repair_authorization"),
                rate_table_sha256=r0.get("rate_table_sha256"),
                ordinary_limit_microusd=policy.ordinary_cost_limit_microusd,
                hard_limit_microusd=policy.hard_stop_cost_limit_microusd,
                batch_index=batch_index,
            )
            for item in result.resolutions:
                value = item.model_dump(mode="json")
                if isinstance(item, V3GeneratedCard):
                    value["card_id"] = f"card:{item.fact_id}:{item.split_index or 1}"
                resolutions.append(value)
            calls.extend(result.calls)
            if usage is not None:
                usages.append(usage)
            if result.blocking_error is not None:
                blocking_errors.append(result.blocking_error)
        _v3_r9_partition(resolutions, {cast(str, item["fact_id"]) for item in requested})
        payload = {
            "policy_sha256": policy.policy_sha256,
            "scope_sha256": scope.scope_sha256,
            "r8_artifact_sha256": r8["artifact_sha256"],
            "requests": [item.model_dump(mode="json") for item in requests],
            "resolutions": sorted(
                resolutions,
                key=lambda item: (
                    str(item["fact_id"]),
                    int(cast(int | str | None, item.get("split_index")) or 0),
                    str(item.get("card_id", "")),
                ),
            ),
            "calls": calls,
        }
        _seal_v3_costs(costs, payload)
        payload["artifact_sha256"] = canonical_sha256(payload)
        return StageProduct(
            kind="card_centric_v3_generation",
            payload=payload,
            usage=_v3_usage(usages),
            blocking_error="; ".join(blocking_errors) or None,
        )

    async def _v3_r10_dedupe(self, context: StageContext) -> StageProduct:
        r0, scope, r4, _r5, _r6, _r7 = _v3_phase_f_inputs(context)
        costs = _v3_cost_session(context, r0)
        r9 = _payload(context, CurationStage.V3_R9_GENERATION)
        _v3_artifact_valid(r9, "Pinned R9 generation identity changed")
        policy = _r3_policy(context, r0)
        if (
            r9.get("policy_sha256") != policy.policy_sha256
            or r9.get("scope_sha256") != scope.scope_sha256
            or r9.get("r8_artifact_sha256")
            != _payload(context, CurationStage.V3_R8_GAP_CONFIRMATION).get("artifact_sha256")
        ):
            raise PinnedInputChanged("Pinned R9 closure changed")
        cards = [item for item in r9.get("resolutions", []) if item.get("status") == "generated"]
        proposals = tuple(
            V3DedupeProposal(
                str(item["card_id"]), str(item["fact_id"]), str(item["text"]), str(item["extra"])
            )
            for item in sorted(
                cards,
                key=lambda item: (
                    str(item["fact_id"]),
                    int(item.get("split_index") or 0),
                    str(item["card_id"]),
                ),
            )
        )
        notes: list[NormalizedNote] = []
        for identity in r4["card_identities"]:
            note = self.companion.get_note(int(identity["note_id"]))
            if note is None or note.content_sha256 != identity["content_sha256"]:
                raise PinnedInputChanged("R10 existing-card closure changed")
            notes.append(note)
        semantic_ids = {int(item["note_id"]) for item in r4["semantic_identities"]}
        vectors = (
            await self.semantic.pinned_document_vectors(
                note_ids=sorted({note.note_id for note in notes} & semantic_ids),
                expected_generation=r4["semantic_generation"],
            )
            if proposals
            else {}
        )
        if not set(vectors) <= semantic_ids:
            raise PinnedInputChanged("R10 pinned vectors escape R4 semantic closure")
        results = await DeduplicationService(
            _GuardedEmbeddingClient(
                self.embedder, costs, "R10", getattr(self.semantic, "model", "embedding")
            )
        ).classify_v3_batch(proposals, notes, existing_document_vectors=vectors)
        by_id = {item.card_id: item for item in results}
        rows: list[dict[str, object]] = []
        for source in r9["resolutions"]:
            value = dict(source)
            if source.get("status") == "generated":
                result = by_id[str(source["card_id"])]
                value["dedupe"] = {
                    "disposition": result.disposition,
                    "duplicate_of": result.duplicate_of,
                    "nearest_matches": [
                        {
                            "identifier": match.identifier,
                            "score": match.score,
                            "exact": match.exact,
                        }
                        for match in result.nearest_matches
                    ],
                    "missing_existing_vector_note_ids": list(
                        result.missing_existing_vector_note_ids
                    ),
                }
                value["status"] = (
                    "generated"
                    if result.disposition == "generated"
                    else "unresolved"
                    if result.disposition == "overlap"
                    else (
                        "duplicate_of_existing"
                        if cast(str, result.duplicate_of).startswith("note:")
                        else "duplicate_of_generated"
                    )
                )
            rows.append(value)
        payload = {
            "policy_sha256": policy.policy_sha256,
            "scope_sha256": scope.scope_sha256,
            "r9_artifact_sha256": r9["artifact_sha256"],
            "resolutions": rows,
        }
        _seal_v3_costs(costs, payload)
        payload["artifact_sha256"] = canonical_sha256(payload)
        return StageProduct(kind="card_centric_v3_dedupe", payload=payload)

    async def _v3_r11_review(self, context: StageContext) -> StageProduct:
        """Pure frozen review projection; it never calls a provider or Anki."""
        if context.job.pipeline_contract_version is not PipelineContractVersion.CARD_CENTRIC_V3:
            raise PinnedInputChanged("R11 review requires the card_centric_v3 contract")
        r0_costs = _payload(context, CurationStage.V3_R0_PREFLIGHT)
        costs = _v3_cost_session(context, r0_costs)
        stages = (
            CurationStage.V3_R0_PREFLIGHT,
            CurationStage.V3_R1_SOURCE_INDEX,
            CurationStage.V3_R2_FIDELITY,
            CurationStage.V3_R3_SCOPE,
            CurationStage.V3_R4_INDEX_VERIFICATION,
            CurationStage.V3_R5_RETRIEVAL,
            CurationStage.V3_R6_CALIBRATION,
            CurationStage.V3_R7_CLASSIFICATION,
            CurationStage.V3_R8_GAP_CONFIRMATION,
            CurationStage.V3_R9_GENERATION,
            CurationStage.V3_R10_DEDUPE,
        )
        # A missing prerequisite is deliberately a raw lookup failure here:
        # the stage runner must never manufacture a partial review artifact.
        products = {
            f"R{index}": context.prior_payloads[stage] for index, stage in enumerate(stages)
        }
        hashes: dict[str, str] = {}
        for name, payload in products.items():
            value = payload.get("artifact_sha256")
            if not isinstance(value, str) or len(value) != 64:
                raise PinnedInputChanged(f"R11 requires a complete {name} artifact hash")
            unsigned = {key: item for key, item in payload.items() if key != "artifact_sha256"}
            if value != canonical_sha256(unsigned):
                raise PinnedInputChanged(f"R11 rejected a tampered {name} artifact")
            hashes[name] = value
        r0, r3, r5, r6, r7, r8, r9, r10 = (
            products["R0"],
            products["R3"],
            products["R5"],
            products["R6"],
            products["R7"],
            products["R8"],
            products["R9"],
            products["R10"],
        )
        r4 = products["R4"]
        scope = LectureScope.model_validate(r3.get("scope"))
        for evidence in scope.evidence:
            if evidence.revision_id is None or evidence.source_kind is None:
                raise PinnedInputChanged("R11 scope evidence lacks pinned provenance")
            try:
                SourceKind(evidence.source_kind)
            except (TypeError, ValueError) as exc:
                raise PinnedInputChanged("R11 scope evidence lacks pinned provenance") from exc
        policy = _r3_policy(context, r0)
        if (
            scope.policy_sha256 != policy.policy_sha256
            or r10.get("scope_sha256") != scope.scope_sha256
        ):
            raise PinnedInputChanged("R11 policy/scope closure changed")
        rate_table_sha256 = r0.get("rate_table_sha256")
        if not isinstance(rate_table_sha256, str) or len(rate_table_sha256) != 64:
            raise PinnedInputChanged("R11 requires the frozen R0 rate table")
        bundle_candidates = {
            str(bundle["bundle_id"]): {
                **dict(cast(Mapping[str, Any], bundle["candidate"])),
                "fact_id": bundle.get("fact_id"),
            }
            for bundle in cast(list[Mapping[str, Any]], r7.get("bundles", []))
            if isinstance(bundle.get("bundle_id"), str)
            and isinstance(bundle.get("candidate"), Mapping)
        }
        residual = cast(Mapping[str, Any], r8.get("residual_r7", {}))
        bundle_candidates.update(
            {
                str(bundle["bundle_id"]): {
                    **dict(cast(Mapping[str, Any], bundle["candidate"])),
                    "fact_id": bundle.get("fact_id"),
                }
                for bundle in cast(list[Mapping[str, Any]], residual.get("bundles", []))
                if isinstance(bundle.get("bundle_id"), str)
                and isinstance(bundle.get("candidate"), Mapping)
            }
        )
        final_rows = [
            *_v3_r7_rows(r7),
            *cast(list[dict[str, Any]], residual.get("final_partition", [])),
        ]
        existing_by_key: dict[tuple[int, str], dict[str, Any]] = {}
        for row in final_rows:
            bundle_id = row.get("bundle_id")
            candidate = bundle_candidates.get(cast(str, bundle_id))
            if candidate is None or not isinstance(candidate.get("note_id"), int):
                raise PinnedInputChanged("R11 R7 selection lacks pinned candidate identity")
            fact_id = candidate["fact_id"]
            if not isinstance(fact_id, str):
                raise PinnedInputChanged("R11 R7 selection lacks pinned fact identity")
            existing_by_key[(candidate["note_id"], fact_id)] = dict(
                row,
                note_id=candidate["note_id"],
                fact_id=fact_id,
                content_sha256=candidate.get("content_sha256"),
                selected=(row.get("disposition") == "keep"),
            )
        existing_rows = list(existing_by_key.values())
        existing_keys = set(existing_by_key)
        for record in cast(list[Mapping[str, Any]], r6.get("records", [])):
            fact_id = record.get("fact_id")
            if not isinstance(fact_id, str):
                raise PinnedInputChanged("R11 R6 record lacks fact identity")
            candidates = {
                int(item["note_id"]): item
                for item in cast(list[Mapping[str, Any]], record.get("all_candidates", []))
                if isinstance(item.get("note_id"), int)
            }
            for cluster in cast(list[Mapping[str, Any]], record.get("clusters", [])):
                representative = cluster.get("representative_note_id")
                if not isinstance(representative, int) or representative not in candidates:
                    raise PinnedInputChanged("R11 R6 cluster lacks a visible representative")
                for note_id in cluster.get("sibling_note_ids", []):
                    if not isinstance(note_id, int) or note_id not in candidates:
                        raise PinnedInputChanged("R11 R6 cluster lacks a visible sibling")
                    if (note_id, fact_id) in existing_keys:
                        continue
                    cluster_candidate = candidates[note_id]
                    existing_keys.add((note_id, fact_id))
                    existing_rows.append(
                        {
                            "note_id": note_id,
                            "fact_id": fact_id,
                            "content_sha256": cluster_candidate.get("content_sha256"),
                            "disposition": "redundant" if note_id != representative else "exclude",
                            "redundant_with_candidate_id": (
                                f"note:{representative}" if note_id != representative else None
                            ),
                            "selected": False,
                        }
                    )
        for row in tuple(existing_rows):
            target = row.get("redundant_with_candidate_id")
            fact_id = row.get("fact_id")
            if row.get("disposition") != "redundant" or not isinstance(target, str):
                continue
            if not target.startswith("note:") or not isinstance(fact_id, str):
                raise PinnedInputChanged("R11 redundant target is malformed")
            try:
                note_id = int(target.removeprefix("note:"))
            except ValueError as exc:
                raise PinnedInputChanged("R11 redundant target is malformed") from exc
            matching = [
                item
                for item in existing_rows
                if item.get("note_id") == note_id and item.get("fact_id") == fact_id
            ]
            if matching:
                matching[0]["disposition"] = "keep"
                matching[0]["selected"] = True
            else:
                existing_keys.add((note_id, fact_id))
                existing_rows.append(
                    {
                        "note_id": note_id,
                        "fact_id": fact_id,
                        "disposition": "keep",
                        "selected": True,
                        "canonical_for_redundant_note_id": row.get("note_id"),
                    }
                )
        for row in cast(list[Mapping[str, Any]], r10.get("resolutions", [])):
            dedupe = row.get("dedupe")
            target = dedupe.get("duplicate_of") if isinstance(dedupe, Mapping) else None
            fact_id = row.get("fact_id")
            if row.get("status") != "duplicate_of_existing" or not isinstance(target, str):
                continue
            if not target.startswith("note:") or not isinstance(fact_id, str):
                raise PinnedInputChanged("R11 R10 duplicate target is malformed")
            try:
                note_id = int(target.removeprefix("note:"))
            except ValueError as exc:
                raise PinnedInputChanged("R11 R10 duplicate target is malformed") from exc
            matching = [
                item
                for item in existing_rows
                if item.get("note_id") == note_id and item.get("fact_id") == fact_id
            ]
            if matching:
                matching[0]["disposition"] = "keep"
                matching[0]["selected"] = True
            else:
                existing_keys.add((note_id, fact_id))
                existing_rows.append(
                    {
                        "note_id": note_id,
                        "fact_id": fact_id,
                        "disposition": "keep",
                        "selected": True,
                        "duplicate_target": True,
                    }
                )
        existing = tuple(existing_rows)
        generated = tuple(
            dict(row, selected=(row.get("status") == "generated"))
            for row in cast(list[dict[str, object]], r10.get("resolutions", []))
            if isinstance(row.get("card_id"), str) and str(row["card_id"]).strip()
        )
        snapshot = V3ReviewSnapshot(
            policy_sha256=policy.policy_sha256,
            scope_sha256=scope.scope_sha256,
            rate_table_sha256=rate_table_sha256,
            r0_to_r10_sha256=hashes,
            evidence={
                "r0": {"policy": r0.get("policy"), "rate_table": r0.get("rate_table")},
                "r1_source": products["R1"],
                "r2_fidelity": products["R2"],
                "r4_index_verification": products["R4"],
                "scope": scope.model_dump(mode="json"),
                "retrieval": r5,
                "calibration": r6,
                "classification": r7,
                "gap_confirmation": r8,
                "generation": r9,
                "dedupe": r10,
                "cost_ledger": r10.get("cost_ledger", []),
                "policy_enforcement": products["R2"],
                "phase_g_safety": V3_PHASE_G_SAFETY,
            },
            existing_candidates=existing,
            generated_cards=generated,
            selected_existing_note_ids=tuple(
                sorted(
                    {
                        int(row["note_id"])
                        for row in existing
                        if row.get("selected") and isinstance(row.get("note_id"), int)
                    }
                )
            ),
            selected_generated_card_ids=tuple(
                sorted(
                    str(row["card_id"])
                    for row in generated
                    if row.get("selected") and row.get("card_id") is not None
                )
            ),
        )
        reconciliation = reconcile_v3(snapshot)
        payload = {
            "policy_sha256": policy.policy_sha256,
            "scope_sha256": scope.scope_sha256,
            "rate_table_sha256": rate_table_sha256,
            "cost_ledger": r10.get("cost_ledger", []),
            "cost_ledger_sha256": r10.get("cost_ledger_sha256"),
            "snapshot": snapshot.model_dump(mode="json"),
            "reconciliation": reconciliation.model_dump(mode="json"),
        }
        _seal_v3_costs(costs, payload)
        payload["artifact_sha256"] = canonical_sha256(payload)
        projected_candidates, projected_gap_cards = _v3_r11_persisted_rows(
            scope=scope,
            r4=r4,
            existing=existing,
            generated=generated,
        )
        return StageProduct(
            kind="card_centric_v3_review",
            payload=payload,
            candidates=projected_candidates,
            gap_cards=projected_gap_cards,
        )

    async def _v3_r12_apply(self, context: StageContext) -> StageProduct:
        """Phase G seam only: approval is required and Anki is never contacted."""
        r11 = _payload(context, CurationStage.V3_R11_REVIEW)
        costs = _v3_cost_session(context, _payload(context, CurationStage.V3_R0_PREFLIGHT))
        _v3_artifact_valid(r11, "R12 rejected a tampered R11 artifact")
        payload = {
            "r11_artifact_sha256": r11["artifact_sha256"],
            "review_snapshot_sha256": r11.get("snapshot", {}).get("snapshot_sha256"),
            "status": "approval_required",
            "anki_mutated": False,
            "phase_g_safety": V3_PHASE_G_SAFETY,
            "cost_ledger": r11.get("cost_ledger", []),
            "cost_ledger_sha256": r11.get("cost_ledger_sha256"),
        }
        _seal_v3_costs(costs, payload)
        payload["artifact_sha256"] = canonical_sha256(payload)
        return StageProduct(kind="card_centric_v3_apply_seam", payload=payload)

    async def _preflight(self, context: StageContext) -> StageProduct:
        result = await self.runtime.ensure_running()
        if not result.reachable or not result.collection_accessible or not result.sync_available:
            raise RuntimeError(result.blocking_reason or "Local Anki preflight failed")
        sync_result = await asyncio.to_thread(self.prompt_sync.sync)
        prompt_snapshot = await asyncio.to_thread(
            self.prompts.load_job_snapshot,
            lcl_id=context.job.lcl_prompt_version,
            coverage_id=context.job.judgment_rubric_version,
            gap_id=context.job.gap_prompt_version,
        )
        snapshot_prompts = prompt_snapshot.prompts
        if (
            getattr(context.job, "pipeline_contract_version", None)
            is PipelineContractVersion.CARD_CENTRIC_V2
        ):
            snapshot_prompts = (
                *snapshot_prompts,
                *AnkiPromptLibrary(self.prompts.bundled_directory)
                .load_many(
                    (
                        "card-centric-ledger-v2",
                        "card-centric-fast-classifier",
                        "card-centric-classifier",
                        "card-centric-gap-v2",
                    )
                )
                .prompts,
            )
        product = StageProduct(
            kind="anki_preflight",
            payload={
                "reachable": result.reachable,
                "ankiconnect_version": result.ankiconnect_version,
                "active_profile": result.active_profile,
                "collection_accessible": result.collection_accessible,
                "sync_available": result.sync_available,
                "prompt_snapshot": [
                    {
                        "id": prompt.metadata.id,
                        "version": prompt.metadata.version,
                        "prompt_hash": prompt.prompt_hash,
                        "content": prompt.content,
                        "path": str(prompt.path),
                        "source_paths": [str(path) for path in prompt.source_paths],
                        "metadata": prompt.metadata.model_dump(
                            mode="json",
                            by_alias=True,
                        ),
                    }
                    for prompt in snapshot_prompts
                ],
                "prompt_sync_stale": sync_result.stale,
                "prompt_sync_detail": sync_result.detail,
            },
        )
        if getattr(
            context.job, "pipeline_contract_version", None
        ) is None or context.job.pipeline_contract_version.value not in {
            "card_centric_v1",
            "card_centric_v2",
        }:
            return product
        try:
            passages = await asyncio.to_thread(
                self.source_extractor.extract,
                context.job.source_revision_ids,
                summary_outline_id=context.job.summary_outline_id,
            )
        except Exception as exc:  # source errors become an actionable S0 artifact
            contract = context.job.pipeline_contract_version.value
            return StageProduct(
                kind="card_centric_preflight_failure",
                payload={
                    "failure_code": "source_preflight_failed",
                    "detail": str(exc),
                },
                blocking_error=f"{contract} preflight failed: source_preflight_failed",
            )
        kinds = {passage.source_kind for passage in passages}
        required = {SourceKind.SLIDE, SourceKind.TRANSCRIPT, SourceKind.SUMMARY}
        if not required <= kinds:
            missing = sorted(kind.value for kind in required - kinds)
            contract = context.job.pipeline_contract_version.value
            return StageProduct(
                kind="card_centric_preflight_failure",
                payload={
                    "failure_code": "required_sources_missing",
                    "missing_source_kinds": missing,
                },
                blocking_error=(
                    f"{contract} preflight failed: required_sources_missing " + ", ".join(missing)
                ),
            )
        return StageProduct(
            kind="card_centric_preflight",
            payload={
                **product.payload,
                "required_source_kinds": ["slide", "transcript", "summary"],
                "source_passage_count": len(passages),
            },
        )

    async def _source_index(
        self,
        context: StageContext,
    ) -> StageProduct:
        passages = await asyncio.to_thread(
            self.source_extractor.extract,
            context.job.source_revision_ids,
            summary_outline_id=context.job.summary_outline_id,
        )
        if any(passage.lecture_id != context.job.lecture_id for passage in passages):
            raise ValueError("selected source revisions contain another lecture")
        if context.job.pipeline_contract_version.value in {"card_centric_v1", "card_centric_v2"}:
            return await asyncio.to_thread(
                self._build_card_centric_source_index,
                context,
                passages,
            )
        generation = await self.source_indexes(context.job.id).refresh(passages)
        return StageProduct(
            kind="lecture_source_index",
            payload={
                "generation": str(generation.generation),
                "passage_count": generation.passage_count,
                "indexed_count": generation.indexed_count,
                "passages": [_passage_payload(passage) for passage in passages],
            },
            job_pins={"source_index_generation": str(generation.generation)},
        )

    def _build_card_centric_source_index(
        self,
        context: StageContext,
        passages: Sequence[SourcePassage],
    ) -> StageProduct:
        usable = [
            passage
            for passage in passages
            if passage.source_kind
            in {
                SourceKind.SUMMARY,
                SourceKind.TRANSCRIPT,
                SourceKind.SLIDE,
                SourceKind.SPEAKER_NOTES,
            }
        ]
        source = build_source_index(
            usable,
            snapshot_id=context.job.index_snapshot_id,
            source_revision_hashes=context.job.source_revision_hashes,
            summary_outline_sha256=context.job.summary_outline_sha256,
        )
        cards = tuple(_card_record(note) for note in self.companion.list_notes())
        census = build_snapshot_census(
            cards,
            deck_allowlist=context.job.deck_allowlist,
            scope_tokens=context.job.tag_allowlist,
            snapshot_id=context.job.companion_generation or context.job.index_snapshot_id,
        )
        return StageProduct(
            kind="card_centric_source_index",
            payload={
                "source_index": source.model_dump(mode="json"),
                "census": census.model_dump(mode="json"),
                "cards": [card.model_dump(mode="json") for card in cards],
            },
        )

    async def _card_ledger(self, context: StageContext) -> StageProduct:
        source = _card_source_index(context)
        stage_model = context.job.resolved_model_config.ledger_s2
        version = context.job.pipeline_contract_version
        instruction = (
            _pinned_card_v2_prompt(context, "card-centric-ledger-v2")
            if version is PipelineContractVersion.CARD_CENTRIC_V2
            else _card_ledger_prompt(self.prompts, version)
        )
        result = await asyncio.to_thread(
            CardCentricLedgerService(
                self.structured,
                instruction,
            ).generate,
            source_index=source,
            provider=ProviderName(stage_model.provider),
            model=stage_model.model,
            record_attempt=context.record_card_ledger_attempt,
        )
        return StageProduct(
            kind="card_centric_ledger",
            payload={
                "ledger": serialize_card_centric_ledger(
                    result.ledger,
                    pipeline_contract_version=version.value,
                ),
                "source_sha256": source.source_sha256,
                "provenance": {
                    "provider": stage_model.provider,
                    "model": stage_model.model,
                    "request_id": result.request_id,
                    "request_ids": list(result.request_ids),
                    "cache_prefix_sha256": result.cache_prefix_sha256,
                },
                "generation_parameters": result.generation_parameters,
                "generation_parameters_sha256": result.generation_parameters_sha256,
                "request_ids": list(result.request_ids),
            },
            usage=StageUsage(
                result.request_id, result.input_tokens, result.output_tokens, result.cost_microusd
            ),
        )

    async def _card_evidence_audit(self, context: StageContext) -> StageProduct:
        """S2b is deterministic and carries concept-specific slide evidence."""
        source = _card_source_index(context)
        ledger = _card_ledger(context)
        matched: dict[str, list[str]] = {}
        counts: dict[str, int] = {}
        for concept in ledger.concepts:
            terms = _evidence_audit_terms(concept)
            seen_passage_ids: set[str] = set()
            passages = []
            for passage in source.passages:
                if (
                    passage.authority != "slide"
                    or passage.passage_id in seen_passage_ids
                    or not _has_evidence_audit_term(passage.text, terms)
                ):
                    continue
                seen_passage_ids.add(passage.passage_id)
                passages.append(passage)
            matched[concept.concept_id] = [passage.passage_id for passage in passages]
            counts[concept.concept_id] = sum(
                _normalized_trimmed_character_count(passage.text) for passage in passages
            )
        audit = CardEvidenceAudit(
            evidence_poor_concept_ids=tuple(
                concept_id for concept_id, count in counts.items() if count < 50
            ),
            matched_slide_passage_ids={
                concept_id: tuple(passage_ids) for concept_id, passage_ids in matched.items()
            },
            matched_slide_char_counts=counts,
            threshold_chars=50,
            total_concepts=len(ledger.concepts),
        )
        return StageProduct(
            kind="card_centric_evidence_audit",
            payload=audit.model_dump(mode="json"),
        )

    async def _card_tag_scope(self, context: StageContext) -> StageProduct:
        source_payload = _payload(context, CurationStage.SOURCE_INDEX)
        cards = _card_records(source_payload)
        census = _card_census(source_payload)
        if census.denominator_count == 0:
            contract = context.job.pipeline_contract_version.value
            return StageProduct(
                kind="card_centric_tag_scope_failure",
                payload={
                    "failure_code": "tag_scope_untrusted",
                    "census": census.model_dump(mode="json"),
                    "detail": census.trust.reason,
                },
                blocking_error=(f"{contract} tag scope blocked: " + census.trust.reason),
            )
        residual_mode = (
            "all_concepts"
            if census.trust.untagged_rate >= CARD_CENTRIC_UNCONDITIONAL_RESIDUAL_RATE
            else "gaps_only"
        )
        scope = scope_cards(
            cards,
            census=census,
            scope_tokens=context.job.tag_allowlist,
        )
        return StageProduct(
            kind="card_centric_tag_scope",
            payload={
                "scope": scope.model_dump(mode="json"),
                "census": census.model_dump(mode="json"),
                "residual_mode": residual_mode,
            },
        )

    async def _card_prefilter(self, context: StageContext) -> StageProduct:
        """S4a: score scoped notes using pinned card vectors and concept queries."""
        source_payload = _payload(context, CurationStage.SOURCE_INDEX)
        cards_by_id = {card.note_id: card for card in _card_records(source_payload)}
        scope = TagScopeResult.model_validate(
            _payload(context, CurationStage.CARD_TAG_SCOPE)["scope"]
        )
        scoped_note_ids = tuple(sorted(scope.scoped_note_ids))
        if not scoped_note_ids:
            result = SemanticPreFilterResult(
                pre_filtered_note_ids=(),
                pre_excluded_note_ids=(),
                threshold=0.55,
                similarity_stats={"min": 0.0, "max": 0.0, "mean": 0.0, "median": 0.0},
            )
            return StageProduct(
                kind="card_centric_prefilter",
                payload=result.model_dump(mode="json"),
            )
        if set(scoped_note_ids) - set(cards_by_id):
            raise PinnedInputChanged("card-centric semantic scope contains unknown notes")
        ledger = _card_ledger(context)
        is_v2 = context.job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V2
        if context.job.semantic_generation is None:
            raise PinnedInputChanged("card-centric v2 job has no pinned semantic generation")
        # S4a's pinned vectors are local, but its concept-query embeddings are
        # still provider work.  Bind that one semantic subcall to the complete
        # stable scope so deterministic rehearsal captures its request,
        # response, and terminal evidence rather than falling through without
        # a provider-call detail.
        with provider_call_scope(
            batch_index=0,
            batch_note_ids=scoped_note_ids,
            kind="query_embedding",
            subcall_ordinal=0,
        ):
            if is_v2:
                similarity = await self.semantic.pinned_centroid_similarity(
                    tuple(_card_concept_centroid_terms(concept) for concept in ledger.concepts),
                    note_ids=scoped_note_ids,
                    expected_generation=context.job.semantic_generation,
                )
                scores = dict(similarity.scores)
                unavailable = tuple(sorted(set(similarity.unavailable_note_ids)))
            else:
                concept_queries = tuple(
                    " ".join((concept.primary_entity, *concept.aliases)).strip()
                    for concept in ledger.concepts
                )
                scores = await self.semantic.pinned_similarity(
                    concept_queries,
                    note_ids=scoped_note_ids,
                    expected_generation=context.job.semantic_generation,
                )
                unavailable = ()
        if (
            set(scores) & set(unavailable)
            or set(scores) | set(unavailable) != set(scoped_note_ids)
            or any(note_id <= 0 for note_id in unavailable)
        ):
            raise PinnedInputChanged(
                "pinned semantic scores and unavailable notes do not cover scoped notes"
            )
        if any(
            not isinstance(score, (int, float)) or not math.isfinite(score)
            for score in scores.values()
        ):
            raise PinnedInputChanged("pinned semantic scores are invalid")
        threshold = 0.55
        pre_filtered = tuple(
            sorted(
                set(unavailable)
                | {note_id for note_id, score in scores.items() if score >= threshold}
            )
        )
        pre_excluded = tuple(sorted(set(scoped_note_ids) - set(pre_filtered)))
        ordered_scores = sorted(scores.values())
        midpoint = len(ordered_scores) // 2
        median = (
            0.0
            if not ordered_scores
            else (
                ordered_scores[midpoint]
                if len(ordered_scores) % 2
                else (ordered_scores[midpoint - 1] + ordered_scores[midpoint]) / 2
            )
        )
        result = SemanticPreFilterResult(
            pre_filtered_note_ids=pre_filtered,
            pre_excluded_note_ids=pre_excluded,
            threshold=threshold,
            similarity_stats={
                "min": min(ordered_scores, default=0.0),
                "max": max(ordered_scores, default=0.0),
                "mean": sum(ordered_scores) / len(ordered_scores) if ordered_scores else 0.0,
                "median": median,
            },
        )
        payload = result.model_dump(mode="json")
        if is_v2:
            payload["embedding_unavailable_note_ids"] = list(unavailable)
        return StageProduct(
            kind="card_centric_prefilter",
            payload=payload,
        )

    async def _card_fast_classify(self, context: StageContext) -> StageProduct:
        """S4b: authorized fast triage of semantically relevant scoped cards."""
        source = _card_source_index(context)
        cards_by_id = {
            card.note_id: card
            for card in _card_records(_payload(context, CurationStage.SOURCE_INDEX))
        }
        scope = TagScopeResult.model_validate(
            _payload(context, CurationStage.CARD_TAG_SCOPE)["scope"]
        )
        prefilter, _ = _read_card_prefilter(context, scope)
        selected = tuple(
            cards_by_id[note_id]
            for note_id in sorted(prefilter.pre_filtered_note_ids)
            if note_id in cards_by_id
        )
        if {card.note_id for card in selected} != set(prefilter.pre_filtered_note_ids):
            raise PinnedInputChanged("card-centric prefilter contains unknown notes")
        stage_model = context.job.resolved_model_config.fast_classify_s4b
        if stage_model is None:
            raise PinnedInputChanged("card-centric v2 job has no fast-classifier model")
        ledger = _card_ledger(context)
        instruction = _pinned_card_v2_prompt(context, "card-centric-fast-classifier")
        concept_definitions = [
            {
                "concept_id": concept.concept_id,
                "canonical_statement": concept.canonical_statement,
                "primary_entity": concept.primary_entity,
                "aliases": list(concept.aliases),
            }
            for concept in sorted(ledger.concepts, key=lambda item: item.concept_id)
        ]
        allowed_concepts = {concept.concept_id for concept in ledger.concepts}
        allowed_passages = {passage.passage_id for passage in source.passages}
        execution = _classifier_execution(context)
        batches = tuple(
            selected[start : start + execution.fast_batch_size]
            for start in range(0, len(selected), execution.fast_batch_size)
        )
        semaphore = asyncio.Semaphore(execution.fast_concurrency)

        async def classify_batch(
            batch_index: int,
            batch: tuple[CardRecord, ...],
        ) -> tuple[
            tuple[FastCardClassification, ...],
            StageUsage | None,
            dict[str, Any] | None,
        ]:
            expected_note_ids = tuple(card.note_id for card in batch)
            reason_code: str | None = None
            try:
                async with semaphore:
                    with provider_call_scope(
                        batch_index=batch_index,
                        batch_note_ids=expected_note_ids,
                    ):
                        generated = await asyncio.to_thread(
                            self.structured.generate_json,
                            instruction,
                            json.dumps(
                                {
                                    "cards": [card.model_dump(mode="json") for card in batch],
                                    "concept_definitions": concept_definitions,
                                    "allowed_concept_ids": sorted(allowed_concepts),
                                    "allowed_supporting_passage_ids": sorted(allowed_passages),
                                },
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            output_model=FastClassificationResult,
                            provider=ProviderName(stage_model.provider),
                            model=stage_model.model,
                            options=GenerationOptions(
                                cacheable_source_prefix=source.prefix,
                                thinking_budget_tokens=execution.thinking_budget_tokens,
                            ),
                        )
            except StructuredOutputError as exc:
                reason_code = "structured_output_invalid"
                usage = StageUsage(
                    exc.generation.request_id,
                    exc.generation.input_tokens,
                    exc.generation.output_tokens,
                    exc.generation.cost_microusd,
                )
            else:
                usage = StageUsage(
                    generated.request_id,
                    generated.input_tokens,
                    generated.output_tokens,
                    generated.cost_microusd,
                )
                reason_code = _fast_batch_degradation_reason(
                    generated.value.results,
                    expected_note_ids=expected_note_ids,
                    allowed_concepts=allowed_concepts,
                    allowed_passages=allowed_passages,
                )
                if reason_code is not None:
                    observed = [item.note_id for item in generated.value.results]
                    expected = set(expected_note_ids)
                    emit_provider_event(
                        getattr(generated, "attempt_handle", None),
                        "contract_failed",
                        error=f"S4b batch degraded: {reason_code}",
                        missing_note_ids=tuple(expected - set(observed)),
                        extra_note_ids=tuple(set(observed) - expected),
                        duplicate_note_ids=tuple(
                            sorted({note_id for note_id in observed if observed.count(note_id) > 1})
                        ),
                    )
                if reason_code is None:
                    return tuple(generated.value.results), usage, None
            if reason_code is not None:
                return (
                    tuple(
                        FastCardClassification(
                            note_id=note_id,
                            verdict="NEEDS_REVIEW",
                            reason=f"S4b degraded batch: {reason_code}",
                        )
                        for note_id in expected_note_ids
                    ),
                    usage,
                    {
                        "batch_index": batch_index,
                        "note_ids": list(expected_note_ids),
                        "reason_code": reason_code,
                    },
                )
            raise AssertionError("S4b batch did not produce a terminal outcome")

        completed = await asyncio.gather(
            *(classify_batch(index, batch) for index, batch in enumerate(batches))
        )
        results = [item for batch, _, _ in completed for item in batch]
        usages = [usage for _, usage, _ in completed if usage is not None]
        degraded_batches = [degraded for _, _, degraded in completed if degraded is not None]
        fast = FastClassificationResult(
            results=tuple(sorted(results, key=lambda item: item.note_id))
        )
        return StageProduct(
            kind="card_centric_fast_classification",
            payload={
                "fast_classifier": fast.model_dump(mode="json"),
                "source_sha256": source.source_sha256,
                "model_config": context.job.resolved_model_config.canonical_document(),
                # This is P2-C's frozen artifact/config seam. P1/I0 owns
                # persistence in preflight identity and canonical job documents.
                "classifier_execution": _classifier_generation_parameters(
                    stage_model.provider,
                    stage_model.model,
                    execution,
                    prompt_id="card-centric-fast-classifier",
                ),
                "fast_count": len(fast.results),
                "degraded_batches": degraded_batches,
                "degraded_note_count": sum(len(batch["note_ids"]) for batch in degraded_batches),
                # These notes were deliberately not sent to S4b.  Keep their
                # identity in the S4b artifact so S4c/selection can prove the
                # scoped universe was conserved without treating them as
                # coverage evidence.
                "fallback_note_ids": list(prefilter.pre_excluded_note_ids),
            },
            usage=_combined_usage("card_fast_classify", usages),
        )

    async def _card_classify(self, context: StageContext) -> StageProduct:
        source_payload = _payload(context, CurationStage.SOURCE_INDEX)
        source = _card_source_index(context)
        scope_payload = _payload(context, CurationStage.CARD_TAG_SCOPE)
        try:
            scope = TagScopeResult.model_validate(scope_payload["scope"])
            concept_ids = tuple(concept.concept_id for concept in _card_ledger(context).concepts)
        except (KeyError, TypeError, ValueError) as exc:
            raise PinnedInputChanged("card-centric scope or ledger artifact is malformed") from exc
        cards_by_id = {card.note_id: card for card in _card_records(source_payload)}
        if set(scope.scoped_note_ids) | set(scope.unscoped_note_ids) != set(cards_by_id):
            raise PinnedInputChanged("card-centric scope does not partition census cards")
        stage_model = context.job.resolved_model_config.classify_s4
        is_v2 = context.job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V2
        if is_v2:
            fast, fallback_ids = _card_fast_classifier(context)
            fast_ids = {item.note_id for item in fast.results}
            if fast_ids & set(fallback_ids) or fast_ids | set(fallback_ids) != set(
                scope.scoped_note_ids
            ):
                raise PinnedInputChanged(
                    "v2 fast and fallback artifacts do not conserve scoped notes"
                )
            selected = tuple(
                cards_by_id[note_id]
                for note_id in sorted(
                    item.note_id for item in fast.results if item.verdict == "NEEDS_REVIEW"
                )
            )
        else:
            selected = tuple(cards_by_id[note_id] for note_id in scope.scoped_note_ids)
        capabilities = _structured_capabilities(
            self.structured,
            ProviderName(stage_model.provider),
            stage_model.model,
        )
        classifier, execution = _card_classifier_for_version(
            context,
            structured=self.structured,
            prompt_catalog=self.prompts,
            capabilities=capabilities,
        )
        classified = await classifier.classify(
            selected,
            source_index=source,
            concept_ids=concept_ids,
            provider=ProviderName(stage_model.provider),
            model=stage_model.model,
        )
        return StageProduct(
            kind="card_centric_classification",
            payload={
                "classifier": classified.model_dump(mode="json"),
                "model_config": context.job.resolved_model_config.canonical_document(),
                "source_sha256": source.source_sha256,
                "scoped_note_count": len(selected),
                "thorough_count": len(selected),
                **(
                    {
                        # P1/I0 will pair this frozen payload with the corresponding
                        # persisted prompt snapshot in replay identity.
                        "classifier_execution": _classifier_generation_parameters(
                            stage_model.provider,
                            stage_model.model,
                            execution,
                            prompt_id="card-centric-classifier" if is_v2 else None,
                        ),
                    }
                    if execution is not None
                    else {}
                ),
            },
            usage=_card_classifier_usage(classified),
            cache_hits=sum(
                audit.cache_read_input_tokens > 0 for audit in classified.telemetry.batches
            ),
        )

    async def _card_coverage(self, context: StageContext) -> StageProduct:
        source = _card_source_index(context)
        ledger = _card_ledger(context)
        classified = _card_classifier(context, CurationStage.CARD_CLASSIFY)
        is_v2 = context.job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V2
        fast = _card_fast_classifier(context)[0] if is_v2 else None
        coverage: dict[str, list[dict[str, Any]]] = {
            concept.concept_id: [] for concept in ledger.concepts
        }
        for item in classified.results:
            evidence_quality = evidence_quality_v2(item, source) if is_v2 else None
            eligible = evidence_quality is not None if is_v2 else selection_eligible(item, source)
            if not eligible:
                continue
            for concept_id in item.covered_concept_ids:
                if concept_id in coverage:
                    evidence = {
                        "note_id": item.note_id,
                        "supporting_passage_ids": list(item.supporting_passage_ids),
                    }
                    if evidence_quality is not None:
                        evidence["evidence_quality"] = evidence_quality.value
                    coverage[concept_id].append(evidence)
        if fast is not None:
            for fast_item in fast.results:
                if not fast_selection_eligible_v2(fast_item, source):
                    continue
                for concept_id in fast_item.grounded_concept_ids:
                    if concept_id in coverage:
                        coverage[concept_id].append(
                            {
                                "note_id": fast_item.note_id,
                                "supporting_passage_ids": list(fast_item.supporting_passage_ids),
                                "evidence_quality": "fast_pass",
                            }
                        )
        return StageProduct(
            kind="card_centric_coverage",
            payload={
                "coverage": {
                    concept_id: {
                        "status": "covered" if evidence else "uncovered",
                        "evidence": sorted(evidence, key=lambda value: value["note_id"]),
                    }
                    for concept_id, evidence in coverage.items()
                },
                "source_sha256": source.source_sha256,
            },
        )

    async def _card_residual(self, context: StageContext) -> StageProduct:
        """S6: whole-deck recall for gaps, or every concept when census requires it."""
        ledger = _card_ledger(context)
        coverage = _card_coverage_payload(context)
        scope_payload = _payload(context, CurationStage.CARD_TAG_SCOPE)
        residual_mode = scope_payload.get("residual_mode", "gaps_only")
        targets = _card_residual_targets(
            ledger,
            coverage,
            residual_mode,
        )
        source_payload = _payload(context, CurationStage.SOURCE_INDEX)
        cards = {card.note_id: card for card in _card_records(source_payload)}
        scoped = TagScopeResult.model_validate(scope_payload["scope"])
        is_v2 = context.job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V2
        searchable_note_ids = set(cards)
        semantic_eligibility_audit: dict[str, Any] = {}
        if is_v2:
            searchable_note_ids, semantic_eligibility_audit = _card_residual_v2_semantic_audit(
                cards
            )
        if not targets:
            return StageProduct(
                kind="card_centric_residual",
                payload={
                    "audits": [],
                    "classifier": None,
                    "uncovered_concept_ids": [],
                    "residual_mode": residual_mode,
                    **semantic_eligibility_audit,
                },
            )
        query_specs = tuple(
            (concept.concept_id, f"{concept.primary_entity} {alias}")
            for concept in targets
            for alias in (concept.aliases or (concept.primary_entity,))
        )
        queries = tuple(query for _, query in query_specs)
        search_kwargs: dict[str, Any] = {
            "eligible_note_ids": searchable_note_ids,
            "limit": 12,
        }
        if is_v2:
            semantic_generation = getattr(context.job, "semantic_generation", None)
            if hasattr(context.job, "semantic_generation") and semantic_generation is None:
                raise PinnedInputChanged("card-centric v2 job has no pinned semantic generation")
            if semantic_generation is not None:
                search_kwargs["expected_generation"] = semantic_generation
        # S6 has one batched semantic-query embedding call before its
        # structured residual classifier.  The ordered searchable note IDs
        # and canonical query payload captured by ReplayEmbeddingClient make
        # this a stable replay identity independent of scheduler timing.
        with provider_call_scope(
            batch_index=0,
            batch_note_ids=tuple(sorted(searchable_note_ids)),
            kind="query_embedding",
            subcall_ordinal=0,
        ):
            hits = await self.semantic.search(queries, **search_kwargs)
        audit: list[dict[str, Any]] = []
        hit_ids: set[int] = set()
        # Terminal v2 fast rows are classification decisions, as are actual
        # S4c rows.  NEEDS_REVIEW is deliberately absent here: it is replaced
        # by S4c.  S4a fallback notes remain eligible for residual recall.
        classified_ids = (
            {
                item.note_id
                for item in _card_classifier(context, CurationStage.CARD_CLASSIFY).results
            }
            | {
                item.note_id
                for item in _terminal_fast_classifications(
                    _card_fast_classifier(context)[0].results
                )
            }
            if is_v2
            else set(scoped.scoped_note_ids)
        )
        for (concept_id, query), found in zip(query_specs, hits, strict=True):
            usable = [item for item in found if item.note_id not in classified_ids]
            ids = tuple(sorted({item.note_id for item in usable}))
            row: dict[str, Any] = {
                "concept_id": concept_id,
                "query": query,
                "hit_note_ids": list(ids),
            }
            if is_v2:
                top = max((item.score for item in usable), default=None)
                row["semantic_scores"] = {
                    str(item.note_id): item.score
                    for item in sorted(usable, key=lambda item: item.note_id)
                }
                below_threshold = tuple(
                    sorted(item.note_id for item in usable if 0.40 <= item.score < 0.50)
                )
                row["below_classification_threshold_note_ids"] = list(below_threshold)
                if top is None or top < 0.40:
                    row.update(
                        {
                            "classified_note_ids": [],
                            "semantic_skip": True,
                            "disposition": "semantic_skip",
                        }
                    )
                    audit.append(row)
                    continue
                gated = tuple(sorted(item.note_id for item in usable if item.score >= 0.50))
                row["classified_note_ids"] = list(gated)
                row["semantic_skip"] = False
                row["disposition"] = "classified" if gated else "below_classification_threshold"
                hit_ids.update(gated)
                audit.append(row)
                continue
            hit_ids.update(ids)
            row["classified_note_ids"] = list(ids)
            audit.append(row)
        selected = tuple(cards[note_id] for note_id in sorted(hit_ids) if note_id in cards)
        stage_model = context.job.resolved_model_config.residual_s6
        classifier, execution = _card_classifier_for_version(
            context,
            structured=self.structured,
            prompt_catalog=self.prompts,
            capabilities=_structured_capabilities(
                self.structured, ProviderName(stage_model.provider), stage_model.model
            ),
        )
        classified = await classifier.classify(
            selected,
            source_index=_card_source_index(context),
            concept_ids=tuple(concept.concept_id for concept in ledger.concepts),
            provider=ProviderName(stage_model.provider),
            model=stage_model.model,
        )
        return StageProduct(
            kind="card_centric_residual",
            payload={
                "audits": audit,
                "classifier": classified.model_dump(mode="json"),
                "uncovered_concept_ids": [concept.concept_id for concept in targets],
                "residual_mode": residual_mode,
                **semantic_eligibility_audit,
                **(
                    {
                        "classifier_execution": _classifier_generation_parameters(
                            stage_model.provider,
                            stage_model.model,
                            execution,
                            prompt_id="card-centric-classifier" if is_v2 else None,
                        ),
                    }
                    if execution is not None
                    else {}
                ),
            },
            usage=_card_classifier_usage(classified),
        )

    async def _card_gap_fill(self, context: StageContext) -> StageProduct:
        ledger = _card_ledger(context)
        coverage = _merged_card_coverage(context)
        source = _card_source_index(context)
        version = context.job.pipeline_contract_version
        is_v2 = version is PipelineContractVersion.CARD_CENTRIC_V2
        stage_model = context.job.resolved_model_config.gap_fill_s7
        instruction = (
            _pinned_card_v2_prompt(context, "card-centric-gap-v2")
            if is_v2
            else _card_gap_prompt(self.prompts, version)
        )
        # P1 will persist this S0 contract in the preflight artifact.  V2 must
        # fail closed until that integration lands instead of reading a mutable
        # live lecture title during an S7 request.
        pinned_lecture = _pinned_card_v2_lecture_metadata(context) if is_v2 else None
        lecture_title = (
            pinned_lecture.title
            if pinned_lecture is not None
            else self.repository.lecture_title(context.job.lecture_id)
        )
        output: list[GeneratedCardResolution] = []
        evidence_records: list[SourceEvidence] = []
        passages_by_id = {passage.passage_id: passage for passage in source.passages}
        usages: list[StageUsage] = []
        for concept_index, concept in enumerate(ledger.concepts):
            if _coverage_suppresses_recovery(coverage[concept.concept_id]):
                continue
            fact_count = concept.suggested_fact_count if is_v2 else 1
            missing_facts = [
                {
                    "fact_id": f"{concept.concept_id}-M{index + 1}",
                    "statement": (
                        concept.fact_descriptions[index] if is_v2 else concept.canonical_statement
                    ),
                }
                for index in range(fact_count)
            ]
            forbidden_by_fact = FactForbiddenClozeMap(
                facts=tuple(
                    FactForbiddenClozeTargets(
                        fact_id=fact["fact_id"],
                        targets=(
                            concept.forbidden_cloze_targets_by_fact[index]
                            if index < len(concept.forbidden_cloze_targets_by_fact)
                            else ()
                        ),
                    )
                    for index, fact in enumerate(missing_facts)
                )
            )
            generation_input: dict[str, object] = {
                "concept": concept.model_dump(mode="json"),
                "missing_facts": missing_facts,
                "evidence_passages": [
                    {
                        "passage_id": passage.passage_id,
                        "source_kind": passage.source_kind,
                        "text": passage.text,
                    }
                    for passage in source.passages
                    if is_v2 or passage.authority != "summary"
                ],
                "lecture_title": lecture_title,
                "lecture_entity_count": ledger.lecture_entity_count,
                "is_mechanism": concept.is_mechanism if is_v2 else False,
                "existing_supports": [],
            }
            if is_v2:
                generation_input["forbidden_cloze_targets_by_fact"] = [
                    {
                        "fact_id": fact["fact_id"],
                        "targets": list(
                            forbidden_by_fact.targets_by_fact_id.get(fact["fact_id"], ())
                        ),
                    }
                    for fact in missing_facts
                ]
            else:
                generation_input["forbidden_cloze_targets"] = list(ledger.forbidden_cloze_targets)
            with provider_call_scope(batch_index=concept_index, defer_acceptance=True):
                result = await asyncio.to_thread(
                    self.structured.generate_json,
                    instruction,
                    json.dumps(generation_input, sort_keys=True, separators=(",", ":")),
                    output_model=CardGapBatch,
                    provider=ProviderName(stage_model.provider),
                    model=stage_model.model,
                    options=GenerationOptions(cacheable_source_prefix=source.prefix),
                )
            attempts = [result]
            expected = {fact["fact_id"] for fact in missing_facts}
            try:
                if is_v2:
                    _validate_card_gap_batch_v2(
                        result.value,
                        expected,
                        forbidden_by_fact.targets_by_fact_id,
                    )
                else:
                    returned = {item.fact_id for item in result.value.resolutions}
                    if returned != expected:
                        raise PinnedInputChanged(
                            "card-centric gap output must resolve every requested fact"
                        )
            except PinnedInputChanged as exc:
                emit_provider_event(result.attempt_handle, "contract_failed", error=str(exc))
                if not is_v2:
                    raise
                repair_input = json.dumps(
                    {
                        "generation_input": generation_input,
                        "invalid_response": result.raw_text,
                        "validation_error": str(exc),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                with provider_call_scope(
                    batch_index=concept_index,
                    kind="repair",
                    defer_acceptance=True,
                ):
                    result = await asyncio.to_thread(
                        self.structured.generate_json,
                        f"{instruction}\n\nRepair the invalid gap-card batch. "
                        "Correct only the reported defect and return the complete batch. "
                        "Do not cloze any reported forbidden target; cloze a different "
                        "element or return that fact as unresolved if no valid alternative exists.",
                        repair_input,
                        output_model=CardGapBatch,
                        provider=ProviderName(stage_model.provider),
                        model=stage_model.model,
                        options=GenerationOptions(cacheable_source_prefix=source.prefix),
                    )
                attempts.append(result)
                try:
                    _validate_card_gap_batch_v2(
                        result.value,
                        expected,
                        forbidden_by_fact.targets_by_fact_id,
                    )
                except PinnedInputChanged as repair_error:
                    emit_provider_event(
                        result.attempt_handle,
                        "contract_failed",
                        error=str(repair_error),
                    )
                    raise
            try:
                for item in result.value.resolutions:
                    if item.status == "generated" and (
                        not set(item.source_passage_ids)
                        <= {passage.passage_id for passage in source.passages}
                        or (
                            not is_v2
                            and all(value.startswith("SUM:") for value in item.source_passage_ids)
                        )
                    ):
                        raise PinnedInputChanged(
                            "generated card must cite admissible lecture evidence"
                        )
                    if item.status == "generated" and not all(
                        passages_by_id[passage_id].text.strip()
                        for passage_id in item.source_passage_ids
                    ):
                        raise PinnedInputChanged(
                            "generated card must cite nonempty grounded lecture evidence"
                        )
            except PinnedInputChanged as exc:
                emit_provider_event(result.attempt_handle, "contract_failed", error=str(exc))
                raise
            for item in result.value.resolutions:
                card_id = hashlib.sha256(
                    f"{concept.concept_id}\0{item.fact_id}\0{item.text}\0{item.extra}".encode()
                ).hexdigest()[:32]
                evidence_ids: tuple[str, ...] = ()
                if item.status == "generated":
                    cited = tuple(passages_by_id[value] for value in item.source_passage_ids)
                    evidence_ids = tuple(
                        source_evidence_id(concept.concept_id, passage.passage_id)
                        for passage in cited
                    )
                    evidence_records.extend(
                        SourceEvidence(
                            evidence_id=evidence_id,
                            concept_id=concept.concept_id,
                            support=EvidenceSupport.SUPPORTED,
                            # Evidence is the cited immutable lecture passage,
                            # never the model-authored card that cites it.
                            statement=passage.text,
                            source_refs=(
                                SourceReference(
                                    source_kind=SourceKind(passage.source_kind),
                                    revision_id=passage.revision_id,
                                    locator=passage.passage_id,
                                    content_hash=passage.content_sha256,
                                ),
                            ),
                            content_hash=passage.content_sha256,
                        )
                        for evidence_id, passage in zip(evidence_ids, cited, strict=True)
                    )
                output.append(
                    GeneratedCardResolution(
                        card_id=f"CC-{card_id}",
                        concept_id=concept.concept_id,
                        fact_id=item.fact_id,
                        text=item.text,
                        extra=item.extra,
                        source_passage_ids=item.source_passage_ids
                        if item.status == "generated"
                        else ("UNRESOLVED",),
                        evidence_ids=evidence_ids,
                        split=item.split,
                        split_index=item.split_index,
                        status=item.status,
                        reason=item.reason,
                    )
                )
            usages.extend(
                StageUsage(
                    attempt.request_id,
                    attempt.input_tokens,
                    attempt.output_tokens,
                    attempt.cost_microusd,
                )
                for attempt in attempts
            )
            finalize_provider_call(result.attempt_handle)
        return StageProduct(
            kind="card_centric_gap_fill",
            payload={"resolutions": [item.model_dump(mode="json") for item in output]},
            usage=_combined_usage("card_gap_fill", usages),
            # Two split cards may cite one passage. They intentionally share
            # its evidence identity; persist one durable record referenced by
            # both cards rather than inserting duplicate primary keys.
            source_evidence=tuple({item.evidence_id: item for item in evidence_records}.values()),
        )

    async def _card_selection(self, context: StageContext) -> StageProduct:
        source = _card_source_index(context)
        ledger = _card_ledger(context)
        classifications = _all_card_classifications(context)
        generated = _card_deduped(context)
        semantic_dedupe_reviews = _card_dedupe_reviews(context)
        _validate_semantic_dedupe_reviews(semantic_dedupe_reviews, generated)
        is_v2 = context.job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V2
        fast, fallback_ids = _card_fast_classifier(context) if is_v2 else (None, ())
        selection_metadata_by_identity: dict[str, dict[str, object]] = {}
        if is_v2:
            assert fast is not None
            # P2 prevents these IDs from being a selection fallback.  P3 still
            # needs their terminal identities for the immutable candidate and
            # exclusion audit, and its selector excludes them explicitly.
            fallback_ids = _unrecovered_s4a_exclusion_note_ids(fallback_ids, classifications)
            selection_result = select_high_yield_v2(
                classifications,
                fast_classifications=fast.results,
                fast_fallback_note_ids=fallback_ids,
                ledger=ledger,
                source_index=source,
                generated_cards=generated,
                semantic_review_required_card_ids=tuple(
                    review.card_id for review in semantic_dedupe_reviews
                ),
                target=65,
                cap=70,
                minimum=60,
            )
            selected = selection_result.selected_existing_note_ids
            excluded = selection_result.excluded_existing_note_ids
            generated_ids = selection_result.selected_generated_card_ids
            selection_metadata_by_identity = {
                item.identity: item.model_dump(mode="json")
                for item in selection_result.selection_metadata
            }
        else:
            selected, excluded, generated_ids = select_high_yield(
                classifications,
                ledger=ledger,
                source_index=source,
                generated_card_ids=[
                    item.card_id for item in generated if item.status == "generated"
                ],
            )
        if is_v2:
            mandatory_note_ids = selection_result.mandatory_note_ids
            mandatory_generated_card_ids = selection_result.mandatory_generated_card_ids
        else:
            mandatory_note_ids = _mandatory_card_note_ids(
                classifications,
                fast.results if fast is not None else (),
                ledger,
                source,
                v2=False,
            )
            mandatory_generated_card_ids = ()
        selected_set = set(selected)
        source_by_id = {passage.passage_id: passage for passage in source.passages}
        candidate_rows = (
            _v2_card_candidates(
                context,
                classifications,
                fast.results,
                fallback_ids,
                selected_set,
                source,
                selection_metadata_by_identity,
            )
            if fast is not None
            else tuple(
                Candidate(
                    note_id=item.note_id,
                    content_hash=next(
                        card.content_sha256
                        for card in _card_records(_payload(context, CurationStage.SOURCE_INDEX))
                        if card.note_id == item.note_id
                    ),
                    best_concept_id=(
                        item.covered_concept_ids[0] if item.covered_concept_ids else "unmapped"
                    ),
                    provenance={
                        "card_centric": {
                            "verdict": item.verdict,
                            "primary_subject": item.primary_subject,
                            "covered_concept_ids": list(item.covered_concept_ids),
                            "supporting_passage_ids": list(item.supporting_passage_ids),
                            "flags": list(item.flags),
                            "selection_eligible": selection_eligible(item, source),
                        }
                    },
                    scores={},
                    predicted_band=item.verdict,
                    verdict=item.verdict.lower(),
                    confidence=1.0 if item.verdict == "YES" else 0.5,
                    reason=item.reason,
                    context_trap="context_trap" in item.flags,
                    recall_direction="card_centric",
                    mnemonic_classification="none",
                    dedupe_disposition="eligible" if item.note_id in selected_set else "excluded",
                    selected=item.note_id in selected_set and selection_eligible(item, source),
                )
                for item in classifications
            )
        )
        gap_cards = tuple(
            GapCard(
                concept_id=item.concept_id,
                text=item.text,
                extra=item.extra,
                selected=item.card_id in set(generated_ids),
                validation_state=item.status,
                source_refs=tuple(
                    SourceReference(
                        source_kind=SourceKind(source_by_id[value].source_kind),
                        revision_id=source_by_id[value].revision_id,
                        locator=value,
                        content_hash=source_by_id[value].content_sha256,
                    )
                    for value in item.source_passage_ids
                    if value != "UNRESOLVED"
                ),
                evidence_ids=item.evidence_ids,
                provenance={
                    "card_centric": {
                        "fact_id": item.fact_id,
                        "source_passage_ids": list(item.source_passage_ids),
                        "reason": item.reason,
                    },
                    "selection": selection_metadata_by_identity.get(
                        f"generated:{item.card_id}",
                        {"selected": False},
                    ),
                },
                content_hash=hashlib.sha256(
                    f"{item.concept_id}\0{item.text}\0{item.extra}".encode()
                ).hexdigest(),
                card_id=item.card_id,
            )
            for item in generated
            if item.status == "generated"
        )
        if is_v2:
            selection_payload = selection_result.model_dump(mode="json")
            selection_payload["selected_count"] = len(selected) + len(generated_ids)
            selection_payload["selection_order"] = [
                item.identity for item in selection_result.selection_metadata
            ]
        else:
            selection_payload = {
                "selected_existing_note_ids": list(selected),
                "excluded_existing_note_ids": list(excluded),
                "selected_generated_card_ids": list(generated_ids),
                "target": 65,
                "cap": 70,
                "minimum_target": 60,
                "mandatory_note_ids": list(mandatory_note_ids),
                "mandatory_generated_card_ids": list(mandatory_generated_card_ids),
                "overflow_acknowledgement": None,
            }
        selection_payload["semantic_dedupe_reviews"] = [
            review.model_dump(mode="json") for review in semantic_dedupe_reviews
        ]
        selection_payload["terminal_resolutions"] = _dedupe_terminal_resolutions(
            generated,
            semantic_dedupe_reviews,
        )
        return StageProduct(
            kind="card_centric_selection",
            payload=selection_payload,
            candidates=candidate_rows,
            gap_cards=gap_cards,
        )

    async def _lcl(self, context: StageContext) -> StageProduct:
        passages = _source_passages(context)
        prompt_text, prompt_hash = _resolved_prompt(
            context,
            context.job.lcl_prompt_version,
        )
        schema_name = _resolved_prompt_schema(
            context,
            context.job.lcl_prompt_version,
        )
        if schema_name not in {"lcl_v1", "lcl_v2"}:
            raise PinnedInputChanged("Pinned LCL prompt schema is unsupported")
        lcl_schema = cast(Literal["lcl_v1", "lcl_v2"], schema_name)
        service = LCLService(
            self.structured,
            provider=_provider(context),
            model=self._model(context),
            prompt_version=context.job.lcl_prompt_version,
            prompt_text=prompt_text,
            prompt_hash=prompt_hash,
            schema_name=lcl_schema,
        )
        generated = await asyncio.to_thread(service.generate, passages)
        return StageProduct(
            kind="lecture_concept_ledger",
            payload={
                "ledger": generated.ledger.model_dump(mode="json"),
                "raw_response": generated.raw_response,
                "prompt_version": generated.prompt_version,
                "prompt_hash": generated.prompt_hash,
                "schema_name": schema_name,
                "provider": generated.provider.value,
                "model": generated.model,
                "request_id": generated.request_id,
                "repair_attempted": generated.repair_attempted,
            },
            usage=StageUsage(
                request_id=generated.request_id,
                input_tokens=generated.input_tokens,
                output_tokens=generated.output_tokens,
                cost_microusd=generated.cost_microusd,
            ),
        )

    async def _retrieval_pass_1(
        self,
        context: StageContext,
    ) -> StageProduct:
        groups: dict[str, list[dict[str, Any]]] = {}
        for concept in _ledger(context).concepts:
            candidates = await self.retrieval.retrieve_pass_1(
                concept,
                _retrieval_scope(context),
            )
            groups[concept.concept_id] = [_candidate_payload(candidate) for candidate in candidates]
        return StageProduct(
            kind="pass_1_candidates",
            payload={"groups": groups},
        )

    async def _judgment_pass_1(
        self,
        context: StageContext,
    ) -> StageProduct:
        return await self._judge_groups(
            context,
            source_stage=CurationStage.RETRIEVAL_PASS_1,
            kind="pass_1_judgments",
        )

    async def _rescue(self, context: StageContext) -> StageProduct:
        ledger = _ledger(context)
        service = RescueService(
            self.source_indexes(context.job.id),
            self.structured,
            provider=_provider(context),
            model=self._model(context),
            prompt_version=context.job.judgment_rubric_version,
        )
        localizations: dict[str, dict[str, Any]] = {}
        evidence_records: list[SourceEvidence] = []
        for concept in ledger.concepts:
            judgment = _coverage_judgment(
                context,
                CurationStage.JUDGMENT_PASS_1,
                concept.concept_id,
            )
            if judgment.status == "covered":
                continue
            localization = await service.localize(
                concept,
                SourceScope(
                    revision_ids=context.job.source_revision_ids,
                    source_kinds=tuple(SourceKind),
                ),
            )
            queries = (
                service.build_queries(localization)
                if localization.support != "unsupported" and localization.evidence
                else ()
            )
            localizations[concept.concept_id] = {
                "support": localization.support,
                "rationale": localization.rationale,
                "evidence": [_passage_payload(passage) for passage in localization.evidence],
                "queries": [query.model_dump(mode="json") for query in queries],
            }
            evidence_records.extend(_evidence_records(localization))
        return StageProduct(
            kind="source_rescue",
            payload={"localizations": localizations},
            source_evidence=tuple(evidence_records),
        )

    async def _retrieval_pass_2(
        self,
        context: StageContext,
    ) -> StageProduct:
        ledger_by_id = {concept.concept_id: concept for concept in _ledger(context).concepts}
        rescue = _payload(context, CurationStage.RESCUE)
        localizations = cast(
            dict[str, dict[str, Any]],
            rescue.get("localizations", {}),
        )
        groups: dict[str, list[dict[str, Any]]] = {}
        for concept_id, localization in localizations.items():
            raw_queries = localization.get("queries", [])
            queries = [RescueQuery.model_validate(query) for query in raw_queries]
            if not queries:
                groups[concept_id] = []
                continue
            candidates = await self.retrieval.retrieve_pass_2(
                ledger_by_id[concept_id],
                queries,
                _retrieval_scope(context),
            )
            groups[concept_id] = [_candidate_payload(candidate) for candidate in candidates]
        return StageProduct(
            kind="pass_2_candidates",
            payload={"groups": groups},
        )

    async def _judgment_pass_2(
        self,
        context: StageContext,
    ) -> StageProduct:
        product = await self._judge_groups(
            context,
            source_stage=CurationStage.RETRIEVAL_PASS_2,
            kind="pass_2_judgments",
        )
        pass_1 = _projected_candidates(
            _payload(
                context,
                CurationStage.JUDGMENT_PASS_1,
            )
        )
        merged = _merge_candidates((*pass_1, *(product.candidates or ())))
        return replace(product, candidates=merged)

    async def _convergence_pass_3(
        self,
        context: StageContext,
    ) -> StageProduct:
        return await self._convergence_pass(context, pass_number=3)

    async def _convergence_pass_4(
        self,
        context: StageContext,
    ) -> StageProduct:
        return await self._convergence_pass(context, pass_number=4)

    async def _convergence_pass_5(
        self,
        context: StageContext,
    ) -> StageProduct:
        return await self._convergence_pass(context, pass_number=5)

    async def _convergence_pass(
        self,
        context: StageContext,
        *,
        pass_number: int,
    ) -> StageProduct:
        if not 3 <= pass_number <= 5:
            raise ValueError("convergence pass number must be between 3 and 5")
        ledger = _ledger(context)
        states, expanded_by_id = _prior_convergence(
            context,
            ledger,
            pass_number=pass_number,
        )
        concepts = {concept.concept_id: concept for concept in ledger.concepts}
        next_states: dict[str, ConvergenceState] = dict(states)
        compatibility_skipped: list[str] = []
        active: list[tuple[str, ConvergenceState, CoverageJudgment]] = []
        for concept_id, state in states.items():
            if state.converged:
                continue
            judgment = _coverage_judgment(
                context,
                _final_judgment_stage(context, concept_id),
                concept_id,
            )
            if not judgment.missing_facts:
                next_states[concept_id] = state.model_copy(update={"converged": True})
            elif not concepts[concept_id].primary_entity:
                compatibility_skipped.append(concept_id)
                next_states[concept_id] = state.model_copy(update={"converged": True})
            else:
                active.append((concept_id, state, judgment))
        existing = tuple(self.repository.list_candidates(context.job.id))
        if not active:
            ordered_states = tuple(next_states[concept.concept_id] for concept in ledger.concepts)
            schema_name = _payload(
                context,
                _final_judgment_stage(
                    context,
                    ledger.concepts[0].concept_id,
                ),
            ).get("schema_name", "coverage_v1")
            return StageProduct(
                kind=f"convergence_pass_{pass_number}",
                payload={
                    "pass_number": pass_number,
                    "schema_name": schema_name,
                    "concepts": [state.model_dump(mode="json") for state in ordered_states],
                    "active_concept_ids": [],
                    "groups": {},
                    "expanded_paraphrases": expanded_by_id,
                    "expansions": {},
                    "judgments": {},
                    "needs_manual_review": False,
                    "manual_review_concept_ids": [],
                    "compatibility_skipped_concept_ids": (compatibility_skipped),
                },
                candidates=existing,
            )
        prompt_text, prompt_hash = _resolved_prompt(
            context,
            "paraphrase-expansion",
        )
        if (
            _resolved_prompt_schema(
                context,
                "paraphrase-expansion",
            )
            != "paraphrase_v2"
        ):
            raise PinnedInputChanged("Pinned paraphrase-expansion schema is unsupported")
        expansion_service = ParaphraseExpansionService(
            self.structured,
            provider=_provider(context),
            model=self._model(context),
            prompt_text=prompt_text,
            prompt_hash=prompt_hash,
        )
        coverage_text, coverage_hash = _resolved_prompt(
            context,
            context.job.judgment_rubric_version,
        )
        coverage_schema_name = _resolved_prompt_schema(
            context,
            context.job.judgment_rubric_version,
        )
        if coverage_schema_name not in {"coverage_v1", "coverage_v2"}:
            raise PinnedInputChanged("Pinned coverage prompt schema is unsupported")
        coverage_service = JudgmentService(
            self.structured,
            self.repository,
            self.companion,
            provider=_provider(context),
            model=self._model(context),
            prompt_version=context.job.judgment_rubric_version,
            prompt_text=coverage_text,
            prompt_hash=coverage_hash,
            schema_name=cast(
                Literal["coverage_v1", "coverage_v2"],
                coverage_schema_name,
            ),
        )
        candidates_by_id = {candidate.note_id: candidate for candidate in existing}
        passages = _source_passages(context)
        scope = _retrieval_scope(context)
        groups: dict[str, list[dict[str, Any]]] = {}
        judgments: dict[str, dict[str, Any]] = {}
        expansions: dict[str, dict[str, Any]] = {}
        active_ids: list[str] = []
        expansion_results: list[ExpansionResult] = []
        judgment_results: list[JudgmentResult] = []
        projected: list[Candidate] = []
        for concept_id, prior_state, prior_judgment in active:
            concept = concepts[concept_id]
            active_ids.append(concept_id)
            used = (*concept.queries, *expanded_by_id.get(concept_id, ()))
            found_notes = tuple(
                note
                for nid in prior_state.seen_note_ids
                if (note := self.companion.get_note(nid)) is not None
            )
            expansion = await asyncio.to_thread(
                expansion_service.expand,
                concept,
                used_paraphrases=used,
                found_notes=found_notes,
                missing_facts=prior_judgment.missing_facts,
            )
            expansion_results.append(expansion)
            queries = expansion.expansion.paraphrases
            expanded_by_id.setdefault(concept_id, []).extend(queries)
            expansions[concept_id] = {
                **expansion.expansion.model_dump(mode="json"),
                "request_ids": list(expansion.request_ids),
            }
            retrieved = await self.retrieval.retrieve_convergence(
                concept,
                queries,
                scope,
                pass_number=pass_number,
            )
            growth = update_growth(
                seen_note_ids=prior_state.seen_note_ids,
                retrieved_note_ids=tuple(candidate.note_id for candidate in retrieved),
            )
            new_ids = set(growth.new_note_ids)
            new_candidates = [candidate for candidate in retrieved if candidate.note_id in new_ids]
            groups[concept_id] = [_candidate_payload(candidate) for candidate in new_candidates]
            for candidate in new_candidates:
                candidates_by_id[candidate.note_id] = candidate
            coverage_complete = not prior_judgment.missing_facts
            if new_candidates:
                support_ids = _combined_support_ids(context, concept_id)
                unknown = support_ids - set(candidates_by_id)
                if unknown:
                    raise PinnedInputChanged("Coverage support is absent from current candidates")
                judge_ids = support_ids | new_ids
                result = await asyncio.to_thread(
                    coverage_service.judge,
                    concept,
                    [candidates_by_id[nid] for nid in sorted(judge_ids)],
                    passages=passages,
                )
                judgment_results.append(result)
                judgments[concept_id] = {
                    "judgment": result.judgment.model_dump(mode="json"),
                    "cache_key": result.cache_key,
                    "cache_hit": result.cache_hit,
                    "provider": result.provider.value,
                    "model": result.model,
                    "request_id": result.request_id,
                }
                runtime = (
                    runtime_judgment_from_v2(result.judgment)
                    if isinstance(result.judgment, CoverageJudgmentV2)
                    else result.judgment
                )
                supporting = set(runtime.supporting_note_ids)
                projected.extend(
                    _judged_candidate(
                        candidates_by_id[nid],
                        runtime,
                        selected=nid in supporting,
                    )
                    for nid in sorted(judge_ids)
                )
                coverage_complete = not runtime.missing_facts
            next_states[concept_id] = ConvergenceState(
                concept_id=concept_id,
                passes_run=pass_number,
                seen_note_ids=growth.seen_note_ids,
                growth=(*prior_state.growth, growth.growth),
                converged=growth.converged or coverage_complete,
            )
        ordered_states = tuple(next_states[concept.concept_id] for concept in ledger.concepts)
        manual_review = tuple(
            state.concept_id for state in ordered_states if pass_number == 5 and not state.converged
        )
        candidates = _merge_candidates((*existing, *projected))
        return StageProduct(
            kind=f"convergence_pass_{pass_number}",
            payload={
                "pass_number": pass_number,
                "schema_name": coverage_schema_name,
                "concepts": [state.model_dump(mode="json") for state in ordered_states],
                "active_concept_ids": active_ids,
                "groups": groups,
                "expanded_paraphrases": expanded_by_id,
                "expansions": expansions,
                "judgments": judgments,
                "needs_manual_review": bool(manual_review),
                "manual_review_concept_ids": list(manual_review),
                "compatibility_skipped_concept_ids": compatibility_skipped,
            },
            usage=_convergence_usage(
                pass_number,
                expansion_results,
                judgment_results,
            ),
            cache_hits=sum(result.cache_hit for result in judgment_results),
            candidates=candidates,
        )

    async def _card_audit(self, context: StageContext) -> StageProduct:
        candidates = tuple(self.repository.list_candidates(context.job.id))
        prompt_text, prompt_hash = _resolved_prompt(
            context,
            "card-relevance-audit",
        )
        metadata = _resolved_prompt_metadata(
            context,
            "card-relevance-audit",
        )
        if metadata.get("schema") != "audit_verdict_v2":
            raise PinnedInputChanged("Pinned card-audit prompt schema is unsupported")
        batch_size = metadata.get("batch_size", 30)
        if not isinstance(batch_size, int) or batch_size < 1:
            raise PinnedInputChanged("Pinned card-audit batch size is malformed")
        service = CardAuditService(
            self.structured,
            self.repository,
            self.companion,
            provider=_provider(context),
            model=self._model(context),
            prompt_text=prompt_text,
            prompt_hash=prompt_hash,
            batch_size=batch_size,
        )
        result = await asyncio.to_thread(
            service.audit,
            lecture_id=context.job.lecture_id,
            lecture_title=self.repository.lecture_title(context.job.lecture_id),
            lecture_entity_count=_ledger(context).lecture_entity_count,
            candidates=candidates,
            passages=_source_passages(context),
        )
        verdict_by_id = {verdict.nid: verdict for verdict in result.verdicts}
        audited = tuple(
            _audited_candidate(candidate, verdict_by_id[candidate.note_id])
            for candidate in candidates
        )
        counts = {
            verdict: sum(item.verdict == verdict for item in result.verdicts)
            for verdict in ("keep", "drop", "uncertain")
        }
        return StageProduct(
            kind="card_relevance_audit",
            payload={
                "schema_name": "audit_verdict_v2",
                "prompt_hash": prompt_hash,
                "verdicts": [verdict.model_dump(mode="json") for verdict in result.verdicts],
                "counts": counts,
            },
            candidates=audited,
            usage=_audit_usage(result),
            cache_hits=result.cache_hits,
        )

    async def _coverage_recompute(
        self,
        context: StageContext,
    ) -> StageProduct:
        ledger = _ledger(context)
        prompt_text, prompt_hash = _resolved_prompt(
            context,
            context.job.judgment_rubric_version,
        )
        schema_name = _resolved_prompt_schema(
            context,
            context.job.judgment_rubric_version,
        )
        if schema_name not in {"coverage_v1", "coverage_v2"}:
            raise PinnedInputChanged("Pinned coverage prompt schema is unsupported")
        coverage_schema = cast(
            Literal["coverage_v1", "coverage_v2"],
            schema_name,
        )
        service = JudgmentService(
            self.structured,
            self.repository,
            self.companion,
            provider=_provider(context),
            model=self._model(context),
            prompt_version=context.job.judgment_rubric_version,
            prompt_text=prompt_text,
            prompt_hash=prompt_hash,
            schema_name=coverage_schema,
        )
        audit_payload = _payload(context, CurationStage.CARD_AUDIT)
        raw_verdicts = audit_payload.get("verdicts")
        if not isinstance(raw_verdicts, list):
            raise PinnedInputChanged("Card-audit artifact is malformed")
        verdicts = tuple(AuditVerdictV2.model_validate(value) for value in raw_verdicts)
        keep_ids = {verdict.nid for verdict in verdicts if verdict.verdict == "keep"}
        candidates_by_id = {
            candidate.note_id: candidate
            for candidate in self.repository.list_candidates(context.job.id)
        }
        if set(candidates_by_id) != {verdict.nid for verdict in verdicts}:
            raise PinnedInputChanged("Card-audit artifact does not partition current candidates")
        results: dict[str, dict[str, Any]] = {}
        usages: list[JudgmentResult] = []
        for concept in ledger.concepts:
            prior_stage = _final_judgment_stage(context, concept.concept_id)
            prior = _coverage_judgment(
                context,
                prior_stage,
                concept.concept_id,
            )
            prior_support_ids = _combined_support_ids(
                context,
                concept.concept_id,
            )
            unknown_support_ids = prior_support_ids - set(candidates_by_id)
            if unknown_support_ids:
                raise PinnedInputChanged("Coverage support is absent from current candidates")
            surviving_ids = tuple(sorted(prior_support_ids & keep_ids))
            if surviving_ids == tuple(sorted(prior_support_ids)) and prior_support_ids == set(
                prior.supporting_note_ids
            ):
                results[concept.concept_id] = {
                    **_judgment_record(
                        context,
                        prior_stage,
                        concept.concept_id,
                    ),
                    "recomputed": False,
                }
                continue
            recomputed = await asyncio.to_thread(
                service.judge,
                concept,
                [candidates_by_id[nid] for nid in surviving_ids],
                passages=_source_passages(context),
            )
            usages.append(recomputed)
            results[concept.concept_id] = {
                "judgment": recomputed.judgment.model_dump(mode="json"),
                "cache_key": recomputed.cache_key,
                "cache_hit": recomputed.cache_hit,
                "provider": recomputed.provider.value,
                "model": recomputed.model,
                "request_id": recomputed.request_id,
                "recomputed": True,
            }
        return StageProduct(
            kind="audited_coverage_judgments",
            payload={
                "schema_name": schema_name,
                "judgments": results,
            },
            usage=_judgment_usage("coverage_recompute", usages),
            cache_hits=sum(result.cache_hit for result in usages),
        )

    async def _dedupe_stage(
        self,
        context: StageContext,
    ) -> StageProduct:
        if context.job.pipeline_contract_version.value in {"card_centric_v1", "card_centric_v2"}:
            return await self._card_dedupe(context)
        return await self._finalize_outcomes(context)

    async def _card_dedupe(self, context: StageContext) -> StageProduct:
        """S8 uses v2 semantic duplicate policy while retaining the v1 path."""
        cards = {
            card.note_id: card
            for card in _card_records(_payload(context, CurationStage.SOURCE_INDEX))
        }
        existing = tuple(
            item
            for item in _all_card_classifications(context)
            if selection_eligible(item, _card_source_index(context))
        )
        generated = _card_generated(context)
        if context.job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V2:
            return await self._card_dedupe_v2(context, cards, generated)
        resolved: list[GeneratedCardResolution] = []
        accepted_generated: list[GeneratedCardResolution] = []
        for item in generated:
            if item.status != "generated":
                resolved.append(item)
                continue
            tokens = _card_tokens(item.text)
            duplicate = next(
                (
                    classification.note_id
                    for classification in existing
                    if item.concept_id in classification.covered_concept_ids
                    and _token_overlap(tokens, _card_tokens(cards[classification.note_id].text))
                    >= 0.80
                ),
                None,
            )
            generated_duplicate = next(
                (
                    other.card_id
                    for other in accepted_generated
                    if other.concept_id == item.concept_id
                    and _token_overlap(tokens, _card_tokens(other.text)) >= 0.80
                ),
                None,
            )
            if duplicate is None and generated_duplicate is None:
                resolved.append(item)
                accepted_generated.append(item)
            else:
                resolved.append(
                    item.model_copy(
                        update={
                            "status": "duplicate_of_existing",
                            "duplicate_of_existing_note_id": duplicate,
                            "duplicate_of_generated_card_id": generated_duplicate,
                            "reason": (
                                "token-overlap duplicate of eligible existing card"
                                if duplicate is not None
                                else (
                                    "token-overlap duplicate of generated card "
                                    f"{generated_duplicate}"
                                )
                            ),
                        }
                    )
                )
        return StageProduct(
            kind="card_centric_dedupe",
            payload={"resolutions": [item.model_dump(mode="json") for item in resolved]},
        )

    async def _card_dedupe_v2(
        self,
        context: StageContext,
        cards: Mapping[int, CardRecord],
        generated: Sequence[GeneratedCardResolution],
    ) -> StageProduct:
        """Adapt immutable card artifacts to the existing semantic deduper."""
        source = _card_source_index(context)
        existing_ids = {
            item.note_id
            for item in _all_card_classifications(context)
            if selection_eligible_v2(item, source)
        }
        existing_concepts = {
            item.note_id: set(item.covered_concept_ids)
            for item in _all_card_classifications(context)
            if item.note_id in existing_ids
        }
        # Only independently eligible thorough classifications can terminate a
        # recovery card as an existing-note duplicate.  Fast-pass LIKELY_YES
        # rows remain a below-floor T6 selection fallback; treating one as an
        # S8 terminal target would let it bypass that quality boundary.
        existing_notes = tuple(
            _normalized_card_note(cards[note_id])
            for note_id in sorted(existing_ids)
            if note_id in cards
        )
        existing_document_vectors = None
        if existing_notes:
            if context.job.semantic_generation is None:
                raise PinnedInputChanged("card-centric v2 job has no pinned semantic generation")
            existing_document_vectors = await self.semantic.pinned_document_vectors(
                note_ids=tuple(note.note_id for note in existing_notes),
                expected_generation=context.job.semantic_generation,
            )
        deduper = DeduplicationService(
            self.embedder,
            duplicate_threshold=0.88,
            overlap_threshold=0.80,
            nearest_limit=5,
        )
        resolved: list[GeneratedCardResolution] = []
        accepted: list[GapCardProposal] = []
        accepted_ids: set[str] = set()
        for batch_index, item in enumerate(generated):
            if item.status != "generated":
                resolved.append(item)
                continue
            proposal = _dedupe_gap_proposal(item, context)
            concept_notes = tuple(
                note
                for note in existing_notes
                if item.concept_id in existing_concepts.get(note.note_id, set())
            )
            concept_vectors = (
                None
                if existing_document_vectors is None
                else {
                    note.note_id: existing_document_vectors[note.note_id]
                    for note in concept_notes
                }
            )
            concept_accepted = tuple(
                other for other in accepted if other.concept_id == item.concept_id
            )
            with provider_call_scope(
                batch_index=batch_index,
                batch_note_ids=tuple(sorted(existing_ids)),
                kind="embedding",
            ):
                outcome = await deduper.classify(
                    proposal,
                    concept_notes,
                    concept_accepted,
                    existing_document_vectors=concept_vectors,
                )
            if outcome.disposition == "unique":
                resolved.append(item)
                accepted.append(proposal)
                accepted_ids.add(f"proposal:{item.card_id}")
                continue
            nearest = outcome.nearest_matches[0].identifier if outcome.nearest_matches else None
            update: dict[str, Any] = {
                "status": "duplicate_of_existing",
                "reason": f"semantic dedup {outcome.disposition}: nearest={nearest or 'none'}",
                "duplicate_of_existing_note_id": None,
                "duplicate_of_generated_card_id": None,
            }
            if nearest is not None and nearest.startswith("note:"):
                try:
                    update["duplicate_of_existing_note_id"] = int(nearest.removeprefix("note:"))
                except ValueError as exc:
                    raise PinnedInputChanged(
                        "semantic dedupe returned an invalid note identity"
                    ) from exc
            elif nearest is not None and nearest in accepted_ids:
                update["duplicate_of_generated_card_id"] = nearest.removeprefix("proposal:")
            else:
                raise PinnedInputChanged("semantic dedupe returned an unknown identity")
            resolved.append(item.model_copy(update=update))
        return StageProduct(
            kind="card_centric_dedupe",
            payload={
                "resolutions": [item.model_dump(mode="json") for item in resolved],
                "semantic_dedupe_reviews": [],
                "terminal_resolutions": _dedupe_terminal_resolutions(resolved),
            },
        )

    async def _finalize_outcomes(
        self,
        context: StageContext,
    ) -> StageProduct:
        rescue_payload = _payload(context, CurationStage.RESCUE)
        localizations = cast(
            dict[str, dict[str, Any]],
            rescue_payload.get("localizations", {}),
        )
        outcomes: dict[str, str] = {}
        for concept in _ledger(context).concepts:
            judgment = _coverage_judgment(
                context,
                CurationStage.COVERAGE_RECOMPUTE,
                concept.concept_id,
            )
            if judgment.status == "covered":
                outcomes[concept.concept_id] = "covered_audited"
                continue
            raw_localization = localizations.get(concept.concept_id)
            if raw_localization is None:
                outcomes[concept.concept_id] = "gap_supported"
                continue
            localization = _localization(
                concept,
                raw_localization,
            )
            outcomes[concept.concept_id] = RescueService.finalize(
                localization,
                judgment,
            )
        return StageProduct(
            kind="final_coverage_outcomes",
            payload={"outcomes": outcomes},
            candidates=tuple(self.repository.list_candidates(context.job.id)),
        )

    async def _generate_gaps(
        self,
        context: StageContext,
    ) -> StageProduct:
        schema_name = _resolved_prompt_schema(
            context,
            context.job.gap_prompt_version,
        )
        if schema_name == "gap_cards_v2":
            return await self._generate_gaps_v2(context)
        if schema_name != "gap_cards_v1":
            raise PinnedInputChanged("Pinned gap-generation prompt schema is unsupported")
        return await self._generate_gaps_v1(context)

    async def _generate_gaps_v1(
        self,
        context: StageContext,
    ) -> StageProduct:
        ledger_by_id = {concept.concept_id: concept for concept in _ledger(context).concepts}
        outcomes = cast(
            dict[str, str],
            _payload(context, CurationStage.DEDUPE).get(
                "outcomes",
                {},
            ),
        )
        rescue_payload = _payload(context, CurationStage.RESCUE)
        localizations = cast(
            dict[str, dict[str, Any]],
            rescue_payload.get("localizations", {}),
        )
        prompt_text, prompt_hash = _resolved_prompt(
            context,
            context.job.gap_prompt_version,
        )
        service = GapCardService(
            self.structured,
            provider=_provider(context),
            model=self._model(context),
            prompt_version=context.job.gap_prompt_version,
            prompt_text=prompt_text,
            prompt_hash=prompt_hash,
        )
        proposed: list[GapCardProposal] = []
        unresolved: list[dict[str, str]] = []
        for concept_id, outcome in outcomes.items():
            if outcome != "gap_supported":
                continue
            localization = (
                _localization(
                    ledger_by_id[concept_id],
                    localizations[concept_id],
                )
                if concept_id in localizations
                else _localization_from_concept(
                    ledger_by_id[concept_id],
                    _source_passages(context),
                )
            )
            generated = await asyncio.to_thread(
                service.generate,
                SupportedGap(
                    concept=localization.concept,
                    evidence=localization.evidence,
                    initial_tags=("OMS::Generated",),
                ),
            )
            if generated.proposal is None:
                unresolved.append(
                    {
                        "concept_id": concept_id,
                        "status": generated.status,
                        "reason": generated.reason,
                    }
                )
            else:
                proposed.append(generated.proposal)

        existing_notes = [
            note
            for candidate in self.repository.list_candidates(context.job.id)
            if (note := self.companion.get_note(candidate.note_id)) is not None
        ]
        dedupe = DeduplicationService(self.embedder)
        cards: list[GapCard] = []
        proposal_payloads: list[dict[str, Any]] = []
        for proposal in proposed:
            classification = await dedupe.classify(
                proposal,
                existing_notes,
                proposed,
            )
            proposal_payloads.append(
                {
                    **_proposal_payload(proposal),
                    "dedupe": {
                        "disposition": classification.disposition,
                        "nearest_matches": [
                            {
                                "identifier": match.identifier,
                                "score": match.score,
                                "exact": match.exact,
                            }
                            for match in classification.nearest_matches
                        ],
                    },
                }
            )
            if classification.disposition == "duplicate":
                continue
            cards.append(
                GapCard(
                    concept_id=proposal.concept_id,
                    text=proposal.fields["Text"],
                    extra=proposal.fields.get("Extra", ""),
                    selected=classification.disposition == "unique",
                    validation_state=(
                        "valid" if classification.disposition == "unique" else "overlap"
                    ),
                    source_refs=proposal.source_refs,
                    evidence_ids=proposal.evidence_ids,
                    provenance={
                        **proposal.provenance,
                        "provider": proposal.provider.value,
                        "model": proposal.model,
                        "prompt_version": proposal.prompt_version,
                        "confidence": proposal.confidence,
                        "dedupe_disposition": (classification.disposition),
                        "nearest_matches": [
                            {
                                "identifier": match.identifier,
                                "score": match.score,
                                "exact": match.exact,
                            }
                            for match in classification.nearest_matches
                        ],
                    },
                    initial_tags=proposal.initial_tags,
                    content_hash=proposal.content_hash,
                )
            )
        return StageProduct(
            kind="grounded_gap_cards",
            payload={
                "proposals": proposal_payloads,
                "unresolved": unresolved,
            },
            gap_cards=tuple(cards),
            usage=_proposal_usage(proposed),
        )

    async def _generate_gaps_v2(
        self,
        context: StageContext,
    ) -> StageProduct:
        ledger = _ledger(context)
        passages = _source_passages(context)
        pinned_lecture = _pinned_card_v2_lecture_metadata(context)
        prompt_text, prompt_hash = _resolved_prompt(
            context,
            context.job.gap_prompt_version,
        )
        service = V2GapGenerationService(
            self.structured,
            provider=_provider(context),
            model=self._model(context),
            prompt_version=context.job.gap_prompt_version,
            prompt_text=prompt_text,
            prompt_hash=prompt_hash,
        )
        all_candidates = tuple(self.repository.list_candidates(context.job.id))
        candidate_by_id = {candidate.note_id: candidate for candidate in all_candidates}
        proposals: list[GapCardProposal] = []
        unresolved: list[dict[str, Any]] = []
        results: list[V2GapGenerationResult] = []
        expected_fact_ids: set[str] = set()
        evidence_records = {
            record.evidence_id: record
            for record in self.repository.list_source_evidence(context.job.id)
        }
        for concept in ledger.concepts:
            judgment = _coverage_judgment(
                context,
                CurationStage.COVERAGE_RECOMPUTE,
                concept.concept_id,
            )
            missing_facts = judgment.missing_fact_records
            if judgment.missing_facts and not missing_facts:
                raise PinnedInputChanged(
                    "V2 gap generation requires V2 audited missing-fact records"
                )
            if not missing_facts:
                continue
            expected_fact_ids.update(fact.fact_id for fact in missing_facts)
            evidence = _v2_gap_evidence(
                concept,
                missing_facts,
                passages,
            )
            for record in _evidence_records(
                RescueLocalization(
                    concept=concept,
                    support="supported",
                    evidence=evidence,
                    rationale=("Audited missing facts are grounded in primary sources."),
                )
            ):
                evidence_records[record.evidence_id] = record
            supporting_notes: list[ExistingGapSupport] = []
            for note_id in judgment.supporting_note_ids:
                candidate = candidate_by_id.get(note_id)
                if candidate is None or not candidate.selected:
                    continue
                note = self.companion.get_note(note_id)
                if note is None:
                    raise PinnedInputChanged(
                        "Gap-generation support is absent from the companion index"
                    )
                supporting_notes.append(
                    ExistingGapSupport(
                        note_id=note.note_id,
                        text=note.text,
                        extra=note.extra,
                    )
                )
            result = await asyncio.to_thread(
                service.generate,
                V2GapGenerationRequest(
                    concept=concept,
                    missing_facts=missing_facts,
                    evidence=evidence,
                    lecture_title=pinned_lecture.title,
                    lecture_entity_count=ledger.lecture_entity_count,
                    forbidden_cloze_targets_by_fact=FactForbiddenClozeMap(
                        facts=tuple(
                            FactForbiddenClozeTargets(
                                fact_id=fact.fact_id,
                                targets=_forbidden_cloze_targets(
                                    lecture_title=pinned_lecture.title,
                                    concept=concept,
                                    lecture_entity_count=ledger.lecture_entity_count,
                                ),
                            )
                            for fact in missing_facts
                        )
                    ),
                    existing_supports=tuple(supporting_notes),
                    initial_tags=("OMS::Generated",),
                    lecture_id=context.job.lecture_id,
                    pinned_lecture_metadata=pinned_lecture,
                ),
            )
            results.append(result)
            proposals.extend(result.proposals)
            unresolved.extend(
                {
                    "concept_id": concept.concept_id,
                    **item.model_dump(mode="json"),
                }
                for item in result.unresolved
            )

        existing_notes = [
            note
            for candidate in all_candidates
            if (note := self.companion.get_note(candidate.note_id)) is not None
        ]
        dedupe = DeduplicationService(self.embedder)
        cards: list[GapCard] = []
        accepted_proposals: list[GapCardProposal] = []
        proposal_payloads: list[dict[str, Any]] = []
        for proposal in proposals:
            classification = await dedupe.classify(
                proposal,
                existing_notes,
                accepted_proposals,
            )
            proposal_payloads.append(
                {
                    **_proposal_payload(proposal),
                    "dedupe": {
                        "disposition": classification.disposition,
                        "nearest_matches": [
                            {
                                "identifier": match.identifier,
                                "score": match.score,
                                "exact": match.exact,
                            }
                            for match in classification.nearest_matches
                        ],
                    },
                }
            )
            if classification.disposition == "duplicate":
                same_fact_duplicate = any(
                    accepted.fact_id == proposal.fact_id
                    and accepted.content_hash == proposal.content_hash
                    for accepted in accepted_proposals
                )
                if same_fact_duplicate:
                    continue
                unresolved.append(
                    {
                        "concept_id": proposal.concept_id,
                        "fact_id": proposal.fact_id,
                        "status": "unresolved",
                        "reason": "duplicate_of_existing_or_generated",
                    }
                )
                continue
            accepted_proposals.append(proposal)
            cards.append(
                _gap_card_from_proposal(
                    proposal,
                    classification,
                    job_id=str(context.job.id),
                )
            )
        generated_fact_ids = {
            str(card.provenance.get("fact_id", ""))
            for card in cards
            if card.provenance.get("fact_id")
        }
        unresolved_fact_ids = {
            str(item.get("fact_id", "")) for item in unresolved if item.get("fact_id")
        }
        if (
            generated_fact_ids & unresolved_fact_ids
            or generated_fact_ids | unresolved_fact_ids != expected_fact_ids
        ):
            raise GapValidationError(
                "post-dedupe gap resolutions do not exactly cover missing facts"
            )
        return StageProduct(
            kind="grounded_gap_cards_v2",
            payload={
                "schema_name": "gap_cards_v2",
                "proposals": proposal_payloads,
                "unresolved": unresolved,
                "forbidden_cloze_targets": sorted(
                    {
                        target
                        for concept in ledger.concepts
                        for target in _forbidden_cloze_targets(
                            lecture_title=pinned_lecture.title,
                            concept=concept,
                            lecture_entity_count=ledger.lecture_entity_count,
                        )
                    }
                ),
            },
            source_evidence=tuple(evidence_records.values()),
            gap_cards=tuple(cards),
            usage=_v2_gap_usage(results),
        )

    async def _reconciliation_stage(
        self,
        context: StageContext,
    ) -> StageProduct:
        if context.job.pipeline_contract_version.value in {"card_centric_v1", "card_centric_v2"}:
            return await self._card_reconciliation(context)
        return await self._reconciliation(context)

    async def _card_reconciliation(self, context: StageContext) -> StageProduct:
        ledger = _card_ledger(context)
        coverage = _merged_card_coverage(context)
        classifications = _all_card_classifications(context)
        generated = _card_deduped(context)
        semantic_dedupe_reviews = _card_dedupe_reviews(context)
        _validate_semantic_dedupe_reviews(semantic_dedupe_reviews, generated)
        selection = _payload(context, CurationStage.CARD_SELECTION)
        scope = TagScopeResult.model_validate(
            _payload(context, CurationStage.CARD_TAG_SCOPE)["scope"]
        )
        census = _card_census(_payload(context, CurationStage.SOURCE_INDEX))
        is_v2 = context.job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V2
        selection_result: QualitySelectionResult | None = None
        if is_v2:
            try:
                selection_result = QualitySelectionResult.model_validate(
                    {name: selection[name] for name in QualitySelectionResult.model_fields}
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise PinnedInputChanged("card-centric selection artifact is malformed") from exc
            selection_order = tuple(
                item.identity
                for item in sorted(
                    selection_result.selection_metadata,
                    key=lambda item: item.selected_position,
                )
            )
            if (
                selection.get("selected_count")
                != len(selection_result.selected_existing_note_ids)
                + len(selection_result.selected_generated_card_ids)
                or tuple(selection.get("selection_order", ())) != selection_order
            ):
                raise PinnedInputChanged("card-centric selection metadata/order/count changed")
        terminal_payload = selection.get("terminal_resolutions")
        if not isinstance(terminal_payload, list):
            raise PinnedInputChanged("card-centric selection terminal resolutions are malformed")
        try:
            terminal_resolutions = tuple(
                GeneratedFactResolution.model_validate(value) for value in terminal_payload
            )
        except (TypeError, ValueError) as exc:
            raise PinnedInputChanged(
                "card-centric selection terminal resolutions are malformed"
            ) from exc
        expected_terminal_resolutions = _dedupe_terminal_resolutions(
            generated,
            semantic_dedupe_reviews,
        )
        if [
            item.model_dump(mode="json") for item in terminal_resolutions
        ] != expected_terminal_resolutions:
            raise PinnedInputChanged("card-centric selection terminal resolutions changed")
        raw_generated = _card_generated(context)
        raw_by_id = {item.card_id: item for item in raw_generated}
        deduped_by_id = {item.card_id: item for item in generated}
        if (
            len(raw_by_id) != len(raw_generated)
            or set(raw_by_id) != set(deduped_by_id)
            or any(
                raw_by_id[card_id].fact_id != deduped_by_id[card_id].fact_id
                or raw_by_id[card_id].split != deduped_by_id[card_id].split
                or raw_by_id[card_id].split_index != deduped_by_id[card_id].split_index
                for card_id in raw_by_id
            )
        ):
            raise PinnedInputChanged("card-centric S7/S8 generated identities changed")
        fast, fallback_ids = _card_fast_classifier(context) if is_v2 else (None, ())
        required_fact_ids = tuple(
            f"{concept.concept_id}-M{index + 1}"
            for concept in ledger.concepts
            if not _coverage_suppresses_recovery(coverage[concept.concept_id])
            for index in range(
                concept.suggested_fact_count
                if context.job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V2
                else 1
            )
        )
        selected_nids = (
            selection_result.selected_existing_note_ids
            if selection_result is not None
            else tuple(selection["selected_existing_note_ids"])
        )
        selected_generated_card_ids = (
            selection_result.selected_generated_card_ids
            if selection_result is not None
            else tuple(selection["selected_generated_card_ids"])
        )
        selected_review_ids = (
            selection_result.semantic_review_required_card_ids
            if selection_result is not None
            else tuple(selection.get("semantic_review_required_card_ids", []))
        )
        if set(selected_review_ids) != {review.card_id for review in semantic_dedupe_reviews}:
            raise PinnedInputChanged("selection semantic review IDs do not match dedupe reviews")
        existing_coverage_by_nid = {
            item.note_id: item.covered_concept_ids
            for item in classifications
            if (
                selection_eligible_v2(item, _card_source_index(context))
                if is_v2
                else selection_eligible(item, _card_source_index(context))
            )
        }
        if fast is not None:
            existing_coverage_by_nid.update(
                {
                    item.note_id: item.grounded_concept_ids
                    for item in fast.results
                    if fast_selection_eligible_v2(item, _card_source_index(context))
                }
            )
        forbidden_by_fact = {
            f"{concept.concept_id}-M{index + 1}": (
                concept.forbidden_cloze_targets_by_fact[index]
                if index < len(concept.forbidden_cloze_targets_by_fact)
                else ()
                if is_v2
                else ledger.forbidden_cloze_targets
            )
            for concept in ledger.concepts
            for index in range(concept.suggested_fact_count if is_v2 else 1)
        }
        acknowledgement = (
            selection_result.overflow_acknowledgement.as_dict()
            if selection_result is not None
            and selection_result.overflow_acknowledgement is not None
            else selection.get("overflow_acknowledgement")
        )
        acknowledgement_valid = False
        if acknowledgement is not None and hasattr(
            self.repository, "validate_card_centric_overflow_acknowledgement"
        ):
            acknowledgement_valid = bool(
                self.repository.validate_card_centric_overflow_acknowledgement(
                    context.job.id,
                    review_revision=context.job.review_revision,
                    selected_note_ids=selected_nids,
                    selected_generated_ids=selected_generated_card_ids,
                    cap=int(
                        selection_result.cap if selection_result is not None else selection["cap"]
                    ),
                    document=cast(dict[str, Any], acknowledgement),
                )
            )
        initial_snapshot = CardCentricReconciliationInput(
            pipeline_contract_version=cast(
                Literal["card_centric_v1", "card_centric_v2"],
                context.job.pipeline_contract_version.value,
            ),
            concept_ids=tuple(concept.concept_id for concept in ledger.concepts),
            coverage={concept.concept_id: "uncovered" for concept in ledger.concepts},
            required_fact_ids=required_fact_ids,
            uncovered_after_s5=tuple(
                concept_id
                for concept_id, value in _card_coverage_payload(context).items()
                if not _coverage_suppresses_recovery(value)
            ),
            residual_ran_for=tuple(
                _payload(context, CurationStage.CARD_RESIDUAL).get("uncovered_concept_ids", [])
            ),
            generated_cards=tuple(
                GeneratedResolution(
                    card_id=item.card_id,
                    fact_id=item.fact_id,
                    text=item.text,
                    extra=item.extra,
                    split=item.split,
                    split_index=item.split_index,
                )
                for item in generated
                if item.status == "generated" and item.card_id in set(selected_generated_card_ids)
            ),
            raw_generated_cards=tuple(
                GeneratedResolution(
                    card_id=item.card_id,
                    fact_id=item.fact_id,
                    text=item.text,
                    extra=item.extra,
                    split=item.split,
                    split_index=item.split_index,
                )
                for item in raw_generated
                if item.status == "generated"
            ),
            canonical_generated_cards=tuple(
                GeneratedResolution(
                    card_id=item.card_id,
                    fact_id=item.fact_id,
                    text=item.text,
                    extra=item.extra,
                    split=item.split,
                    split_index=item.split_index,
                )
                for item in generated
                if item.status == "generated"
            ),
            terminal_resolutions=terminal_resolutions,
            terminal_resolutions_provided=True,
            # A duplicate is not an unresolved fact. Until P3-D's duplicate
            # coverage reconciliation has its selected target, A1/A2 must
            # fail closed rather than misrepresent it as an intentional gap.
            canonical_unresolved_fact_ids=tuple(
                item.fact_id for item in generated if item.status == "unresolved"
            ),
            unresolved_fact_ids=tuple(
                item.fact_id for item in generated if item.status == "unresolved"
            ),
            expected_scoped_nids=scope.scoped_note_ids,
            classifications=(
                _v2_reconciliation_classifications(
                    classifications, fast.results, fallback_ids, scope
                )
                if fast is not None
                else tuple(
                    AuditResolution(
                        nid=item.note_id,
                        verdict=cast(
                            Literal["keep", "drop", "uncertain"],
                            {"YES": "keep", "NO": "drop", "MAYBE": "uncertain"}[item.verdict],
                        ),
                    )
                    for item in classifications
                    if item.note_id in set(scope.scoped_note_ids)
                )
            ),
            eligible_yes_nids=tuple(
                sorted(
                    {
                        *(
                            item.note_id
                            for item in classifications
                            if (
                                selection_eligible_v2(item, _card_source_index(context))
                                if is_v2
                                else selection_eligible(item, _card_source_index(context))
                            )
                        ),
                        *(
                            item.note_id
                            for item in (fast.results if fast is not None else ())
                            if fast_selection_eligible_v2(item, _card_source_index(context))
                        ),
                    }
                )
            ),
            selected_nids=selected_nids,
            selected_generated_card_ids=selected_generated_card_ids,
            generated_card_ids=tuple(
                item.card_id for item in generated if item.status == "generated"
            ),
            source_passage_ids=tuple(
                passage.passage_id for passage in _card_source_index(context).passages
            ),
            forbidden_cloze_targets=ledger.all_forbidden_targets
            if context.job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V2
            else ledger.forbidden_cloze_targets,
            forbidden_cloze_targets_by_fact=forbidden_by_fact,
            prompt_sync_stale=bool(
                _payload(context, CurationStage.PREFLIGHT).get("prompt_sync_stale", False)
            ),
            untagged_rate=census.trust.untagged_rate,
            target=int(
                selection_result.target if selection_result is not None else selection["target"]
            ),
            cap=int(selection_result.cap if selection_result is not None else selection["cap"]),
            mandatory_nids=tuple(
                selection_result.mandatory_note_ids
                if selection_result is not None
                else selection["mandatory_note_ids"]
            ),
            mandatory_generated_card_ids=tuple(
                selection_result.mandatory_generated_card_ids
                if selection_result is not None
                else selection.get("mandatory_generated_card_ids", [])
            ),
            covered_concept_ids_by_nid=existing_coverage_by_nid,
            generated_concept_id_by_card_id={
                item.card_id: item.concept_id for item in generated if item.status == "generated"
            },
            # Only carry an acknowledgement into an initial S9 snapshot after
            # the repository has checked its immutable HMAC/revision binding.
            # On later persistence, the repository performs the same check
            # before adding the document to the stored snapshot.
            overflow_acknowledgement=(
                cast(dict[str, object], acknowledgement) if acknowledgement_valid else None
            ),
            selection_metadata=(
                selection_result.selection_metadata if selection_result is not None else ()
            ),
            selection_order=(selection_order if selection_result is not None else ()),
            selected_count=(
                int(selection["selected_count"]) if selection_result is not None else None
            ),
            below_warning_floor=(
                selection_result.below_warning_floor if selection_result is not None else None
            ),
            semantic_review_required_card_ids=selected_review_ids,
            historical_yes_rates=(_pinned_card_v2_a11_history(context) if is_v2 else ()),
            t6_selected_nids=tuple(
                note_id
                for note_id in selection["selected_existing_note_ids"]
                if note_id
                not in {
                    item.note_id
                    for item in classifications
                    if selection_eligible_v2(item, _card_source_index(context))
                }
                and note_id
                not in {
                    item.note_id
                    for item in (fast.results if fast is not None else ())
                    if fast_selection_eligible_v2(item, _card_source_index(context))
                }
            ),
        )
        snapshot = initial_snapshot.model_copy(
            update={"coverage": selected_card_centric_coverage(initial_snapshot)}
        )
        report = reconcile_card_centric(snapshot)
        return StageProduct(
            kind="card_centric_reconciliation",
            payload={
                "contract_version": "card_centric_s9_v1",
                **report.model_dump(mode="json"),
                "selection": selection,
                "snapshot": snapshot.model_dump(mode="json"),
                "semantic_dedupe_reviews": [
                    review.model_dump(mode="json") for review in semantic_dedupe_reviews
                ],
                "terminal_resolutions": _dedupe_terminal_resolutions(
                    generated,
                    semantic_dedupe_reviews,
                ),
            },
            blocking_error=_card_reconciliation_error(report, snapshot),
        )

    async def _reconciliation(
        self,
        context: StageContext,
    ) -> StageProduct:
        gaps_payload = _payload(context, CurationStage.GAPS)
        if gaps_payload.get("schema_name") != "gap_cards_v2":
            return StageProduct(
                kind="reconciliation_compatibility_v1",
                payload={
                    "schema_name": "reconciliation_v1_compatibility",
                    "can_render_envelope": True,
                    "passed": [],
                    "failed": [],
                    "warned": [
                        {
                            "assertion_id": "legacy_v1",
                            "message": ("Legacy V1 run cannot provide V2 reconciliation"),
                        }
                    ],
                    "snapshot": None,
                },
            )
        snapshot = _reconciliation_snapshot(context, self.repository)
        report = reconcile(snapshot)
        failed_ids = [finding.assertion_id for finding in report.failed]
        return StageProduct(
            kind="reconciliation_report_v2",
            payload={
                "schema_name": "reconciliation_v2",
                **report.model_dump(mode="json"),
                "snapshot": snapshot.model_dump(mode="json"),
                "metrics": _reconciliation_metrics(snapshot),
            },
            blocking_error=(
                "Reconciliation failed: " + ", ".join(failed_ids) if failed_ids else None
            ),
        )

    async def _judge_groups(
        self,
        context: StageContext,
        *,
        source_stage: CurationStage,
        kind: str,
    ) -> StageProduct:
        raw_groups = cast(
            dict[str, list[dict[str, Any]]],
            _payload(context, source_stage).get("groups", {}),
        )
        ledger_by_id = {concept.concept_id: concept for concept in _ledger(context).concepts}
        prompt_text, prompt_hash = _resolved_prompt(
            context,
            context.job.judgment_rubric_version,
        )
        schema_name = _resolved_prompt_schema(
            context,
            context.job.judgment_rubric_version,
        )
        if schema_name not in {"coverage_v1", "coverage_v2"}:
            raise PinnedInputChanged("Pinned coverage prompt schema is unsupported")
        coverage_schema = cast(
            Literal["coverage_v1", "coverage_v2"],
            schema_name,
        )
        service = JudgmentService(
            self.structured,
            self.repository,
            self.companion,
            provider=_provider(context),
            model=self._model(context),
            prompt_version=context.job.judgment_rubric_version,
            prompt_text=prompt_text,
            prompt_hash=prompt_hash,
            schema_name=coverage_schema,
        )
        results: dict[str, dict[str, Any]] = {}
        projected: list[Candidate] = []
        usages: list[JudgmentResult] = []
        for concept_id, values in raw_groups.items():
            candidates = [_candidate_from_payload(value) for value in values]
            for deck_candidates in _priority_candidate_groups(candidates):
                result = await asyncio.to_thread(
                    service.judge,
                    ledger_by_id[concept_id],
                    deck_candidates,
                    passages=_source_passages(context),
                )
                usages.append(result)
                results[concept_id] = {
                    "judgment": result.judgment.model_dump(mode="json"),
                    "cache_key": result.cache_key,
                    "cache_hit": result.cache_hit,
                    "provider": result.provider.value,
                    "model": result.model,
                    "request_id": result.request_id,
                }
                runtime = (
                    runtime_judgment_from_v2(result.judgment)
                    if isinstance(result.judgment, CoverageJudgmentV2)
                    else result.judgment
                )
                supporting = set(runtime.supporting_note_ids)
                projected.extend(
                    _judged_candidate(
                        candidate,
                        runtime,
                        selected=candidate.note_id in supporting,
                    )
                    for candidate in deck_candidates
                )
                if supporting:
                    break
        merged = _merge_candidates(projected)
        return StageProduct(
            kind=kind,
            payload={
                "schema_name": schema_name,
                "judgments": results,
                "projected_candidates": [_candidate_payload(candidate) for candidate in merged],
            },
            candidates=merged,
            usage=_judgment_usage(kind, usages),
            cache_hits=sum(result.cache_hit for result in usages),
        )


def _priority_candidate_groups(
    candidates: Sequence[Candidate],
) -> tuple[tuple[Candidate, ...], ...]:
    if not candidates:
        return ((),)
    if not any("deck_priority" in candidate.provenance for candidate in candidates):
        return (tuple(candidates),)
    grouped: dict[int, list[Candidate]] = {}
    for candidate in candidates:
        raw_priority = candidate.provenance.get("deck_priority", 0)
        priority = raw_priority if isinstance(raw_priority, int) else 0
        grouped.setdefault(priority, []).append(candidate)
    return tuple(tuple(grouped[key]) for key in sorted(grouped))


def _provider(context: StageContext) -> ProviderName:
    return ProviderName(context.job.provider)


def _card_classifier_prompt(catalog: AnkiPromptCatalogService) -> str:
    prompt = AnkiPromptLibrary(catalog.bundled_directory).load("card-centric-classifier-v1")
    return prompt.content


def _structured_capabilities(
    structured: StructuredTextService,
    provider: ProviderName,
    model: str,
) -> ProviderCapabilities:
    """Use Wave 2's model-aware capability API when the generator exposes it."""
    capabilities_for = getattr(structured.generator, "capabilities_for", None)
    if not callable(capabilities_for):
        return ProviderCapabilities()
    capabilities = capabilities_for(provider, model)
    if not isinstance(capabilities, ProviderCapabilities):
        raise PinnedInputChanged("LLM capability report is invalid")
    return capabilities


def _card_record(note: Any) -> CardRecord:
    return CardRecord(
        note_id=note.note_id,
        content_sha256=note.content_sha256,
        text=note.text,
        extra=note.extra,
        tags=tuple(note.tags),
        deck_names=tuple(note.deck_names),
    )


def _card_residual_targets(
    ledger: CardConceptLedger,
    coverage: Mapping[str, dict[str, Any]],
    residual_mode: str,
) -> tuple[CardConcept, ...]:
    if residual_mode == "all_concepts":
        return ledger.concepts
    if residual_mode != "gaps_only":
        raise PinnedInputChanged("card-centric residual mode is invalid")
    return tuple(
        concept
        for concept in ledger.concepts
        if not _coverage_suppresses_recovery(coverage[concept.concept_id])
    )


def _coverage_suppresses_recovery(value: Mapping[str, Any]) -> bool:
    """Only thorough/generated-grade evidence may suppress terminal recovery.

    Fast-pass evidence remains visible as covered in S5 diagnostics, but the
    T6-only selector may correctly exclude it after the warning floor. Treat an
    all-fast coverage row as provisional so S6/S7 and S9 fact accounting still
    produce a selected, duplicate, generated, or unresolved terminal outcome.
    """

    status = value.get("status")
    evidence = value.get("evidence")
    if status not in {"covered", "uncovered"} or not isinstance(evidence, list):
        raise PinnedInputChanged("card-centric coverage entry is malformed")
    if status == "uncovered":
        return False
    if any(not isinstance(item, dict) for item in evidence):
        raise PinnedInputChanged("covered card-centric concept has malformed evidence")
    if not evidence:
        # Preserve legacy/handcrafted v1 coverage rows that predate evidence
        # quality labels. New v2 S5 artifacts always include evidence here.
        return True
    return any(item.get("evidence_quality") != "fast_pass" for item in evidence)


def _card_concept_centroid_terms(concept: CardConcept) -> tuple[str, ...]:
    """Return stable, separately normalized primary/alias S4a query terms."""
    primary = normalize_semantic_text(concept.primary_entity)
    if not primary:
        raise PinnedInputChanged("card-centric concept primary entity is blank")
    terms = {primary}
    terms.update(
        normalized for alias in concept.aliases if (normalized := normalize_semantic_text(alias))
    )
    return (primary, *sorted(terms - {primary}))


def _read_card_prefilter(
    context: StageContext,
    scope: TagScopeResult,
) -> tuple[SemanticPreFilterResult, tuple[int, ...]]:
    """Validate the S4a/S4b partition, including diagnostic pass-through rows."""
    payload = _payload(context, CurationStage.CARD_PREFILTER)
    if not isinstance(payload, dict):
        raise PinnedInputChanged("card-centric prefilter artifact is malformed")
    try:
        prefilter = SemanticPreFilterResult.model_validate(
            {name: payload[name] for name in SemanticPreFilterResult.model_fields}
        )
        unavailable = tuple(
            sorted({int(value) for value in payload.get("embedding_unavailable_note_ids", [])})
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PinnedInputChanged("card-centric prefilter artifact is malformed") from exc
    pre_filtered = set(prefilter.pre_filtered_note_ids)
    excluded = set(prefilter.pre_excluded_note_ids)
    if (
        len(pre_filtered) != len(prefilter.pre_filtered_note_ids)
        or len(excluded) != len(prefilter.pre_excluded_note_ids)
        or pre_filtered & excluded
        or pre_filtered | excluded != set(scope.scoped_note_ids)
        or not set(unavailable) <= pre_filtered
        or any(note_id <= 0 for note_id in unavailable)
    ):
        raise PinnedInputChanged("card-centric prefilter does not partition scoped notes")
    return prefilter, unavailable


def _card_records(payload: dict[str, Any]) -> tuple[CardRecord, ...]:
    raw = payload.get("cards")
    if not isinstance(raw, list):
        raise PinnedInputChanged("card-centric source-index cards are malformed")
    try:
        cards = tuple(CardRecord.model_validate(value) for value in raw)
    except (TypeError, ValueError) as exc:
        raise PinnedInputChanged("card-centric source-index cards are malformed") from exc
    if len({card.note_id for card in cards}) != len(cards):
        raise PinnedInputChanged("card-centric source-index has duplicate cards")
    return cards


def _card_residual_v2_semantic_audit(
    cards: Mapping[int, CardRecord],
) -> tuple[set[int], dict[str, Any]]:
    """Partition S6's pinned source cards using the indexer's Text/Extra rule."""
    searchable_note_ids: list[int] = []
    blank_note_ids: list[int] = []
    for note_id, card in sorted(cards.items()):
        semantic_text = card.text if card.text.strip() else card.extra
        if semantic_text.strip():
            searchable_note_ids.append(note_id)
        else:
            blank_note_ids.append(note_id)
    canonical_searchable_ids = json.dumps(
        {
            "domain": _CARD_CENTRIC_V2_S6_SEMANTIC_ELIGIBILITY_AUDIT_DOMAIN,
            "searchable_note_ids": searchable_note_ids,
            "version": _CARD_CENTRIC_V2_S6_SEMANTIC_ELIGIBILITY_AUDIT_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return set(searchable_note_ids), {
        "semantic_eligibility_audit_version": (
            _CARD_CENTRIC_V2_S6_SEMANTIC_ELIGIBILITY_AUDIT_VERSION
        ),
        "semantic_eligibility_audit_domain": (
            _CARD_CENTRIC_V2_S6_SEMANTIC_ELIGIBILITY_AUDIT_DOMAIN
        ),
        "embedding_unavailable_blank_note_ids": blank_note_ids,
        "searchable_note_count": len(searchable_note_ids),
        "searchable_note_ids_sha256": hashlib.sha256(
            canonical_searchable_ids.encode("utf-8")
        ).hexdigest(),
    }


def _card_census(payload: dict[str, Any]) -> SnapshotCensus:
    try:
        return SnapshotCensus.model_validate(payload["census"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PinnedInputChanged("card-centric census is malformed") from exc


def _card_source_index(context: StageContext) -> CardCentricSourceIndex:
    payload = _payload(context, CurationStage.SOURCE_INDEX)
    try:
        return CardCentricSourceIndex.model_validate(payload["source_index"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PinnedInputChanged("card-centric source index is malformed") from exc


def _card_classifier_usage(classified: ClassifierResult) -> StageUsage | None:
    audits = classified.telemetry.batches
    if not audits:
        return None
    request_ids = [audit.request_id for audit in audits]
    identity = json.dumps(request_ids, separators=(",", ":"))
    return StageUsage(
        request_id=(f"card_classify:{hashlib.sha256(identity.encode()).hexdigest()[:24]}"),
        input_tokens=sum(audit.input_tokens for audit in audits),
        output_tokens=sum(audit.output_tokens for audit in audits),
        cost_microusd=sum(audit.cost_microusd for audit in audits),
    )


def _card_ledger(context: StageContext) -> CardConceptLedger:
    try:
        return CardConceptLedger.model_validate(
            _payload(context, CurationStage.CARD_LEDGER)["ledger"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PinnedInputChanged("card-centric ledger artifact is malformed") from exc


def _card_classifier(context: StageContext, stage: CurationStage) -> ClassifierResult:
    try:
        return ClassifierResult.model_validate(_payload(context, stage)["classifier"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PinnedInputChanged("card-centric classifier artifact is malformed") from exc


def _card_fast_classifier(
    context: StageContext,
) -> tuple[FastClassificationResult, tuple[int, ...]]:
    try:
        payload = _payload(context, CurationStage.CARD_FAST_CLASSIFY)
        classifier = FastClassificationResult.model_validate(payload["fast_classifier"])
        fallback = tuple(sorted({int(value) for value in payload.get("fallback_note_ids", [])}))
    except (KeyError, TypeError, ValueError) as exc:
        raise PinnedInputChanged("card-centric fast classifier artifact is malformed") from exc
    if any(note_id <= 0 for note_id in fallback):
        raise PinnedInputChanged("card-centric fast fallback contains invalid note IDs")
    return classifier, fallback


def _classifier_execution(context: StageContext) -> ResolvedClassifierExecution:
    """Resolve P2-C execution defaults without rewriting legacy job documents."""
    configuration = context.job.resolved_model_config
    resolver = getattr(configuration, "resolved_classifier_execution", None)
    if callable(resolver):
        return cast(ResolvedClassifierExecution, resolver())
    return ResolvedClassifierExecution()


def _card_classifier_for_version(
    context: StageContext,
    *,
    structured: StructuredTextService,
    prompt_catalog: AnkiPromptCatalogService,
    capabilities: ProviderCapabilities,
) -> tuple[CardCentricClassifier, ResolvedClassifierExecution | None]:
    """Build legacy v1 or frozen v2 classifier settings without cross-version bleed."""
    if context.job.pipeline_contract_version is not PipelineContractVersion.CARD_CENTRIC_V2:
        return (
            CardCentricClassifier(
                structured,
                instruction=_card_classifier_prompt(prompt_catalog),
                capabilities=capabilities,
            ),
            None,
        )
    execution = _classifier_execution(context)
    return (
        CardCentricClassifier(
            structured,
            instruction=_pinned_card_v2_prompt(context, "card-centric-classifier"),
            batch_size=execution.thorough_batch_size,
            concurrency=execution.thorough_concurrency,
            retry_attempts=execution.thorough_retry_attempts,
            thinking_budget_tokens=execution.thinking_budget_tokens,
            require_nonblank_reason=True,
            capabilities=capabilities,
        ),
        execution,
    )


def _classifier_generation_parameters(
    provider: str,
    model: str,
    execution: ResolvedClassifierExecution,
    *,
    prompt_id: str | None,
) -> dict[str, Any]:
    """Frozen P2-C hook for ``ResolvedStageModelIdentity.generation_parameters``.

    P1/I0 owns pairing this canonical payload with the corresponding immutable
    prompt snapshot, whose content is hashed by ``ResolvedStageModelIdentity``.
    """
    return {
        "provider": provider,
        "model": model,
        "prompt_id": prompt_id,
        "execution": execution.canonical_document(),
        "execution_sha256": execution.generation_parameters_sha256(),
        "generation_options": {
            "cacheable_source_prefix": True,
            "thinking": "disabled",
            "thinking_budget_tokens": execution.thinking_budget_tokens,
        },
    }


def _fast_batch_degradation_reason(
    results: Sequence[FastCardClassification],
    *,
    expected_note_ids: Sequence[int],
    allowed_concepts: set[str],
    allowed_passages: set[str],
) -> str | None:
    """Return a safe reason code; no partial S4b batch is ever accepted."""
    observed_note_ids = tuple(item.note_id for item in results)
    if len(observed_note_ids) != len(set(observed_note_ids)) or set(observed_note_ids) != set(
        expected_note_ids
    ):
        return "partition_mismatch"
    for item in results:
        if not item.reason.strip():
            return "blank_reason"
        if not set(item.grounded_concept_ids) <= allowed_concepts:
            return "invented_concept_id"
        if not set(item.supporting_passage_ids) <= allowed_passages:
            return "invented_passage_id"
        if item.verdict == "LIKELY_YES" and (
            not item.grounded_concept_ids or not item.supporting_passage_ids
        ):
            return "ungrounded_likely_yes"
    return None


def _terminal_fast_classifications(
    classifications: Sequence[FastCardClassification],
) -> tuple[FastCardClassification, ...]:
    """S4b NEEDS_REVIEW is routing metadata, not a terminal judgment."""
    return tuple(item for item in classifications if item.verdict != "NEEDS_REVIEW")


def _effective_v2_fallback_note_ids(
    fallback_note_ids: Sequence[int],
    classifications: Sequence[CardClassification],
) -> tuple[int, ...]:
    """S4a exclusions are diagnostic only and never selection fallback candidates."""
    del fallback_note_ids, classifications
    return ()


def _unrecovered_s4a_exclusion_note_ids(
    fallback_note_ids: Sequence[int],
    classifications: Sequence[CardClassification],
) -> tuple[int, ...]:
    """Expose S4a exclusions for audit accounting without making them selectable."""
    classified_ids = {item.note_id for item in classifications}
    return tuple(
        note_id for note_id in sorted(set(fallback_note_ids)) if note_id not in classified_ids
    )


def _all_card_classifications(context: StageContext) -> tuple[CardClassification, ...]:
    if context.job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V2:
        _validate_card_residual_v2_semantic_audit(context)
    primary = _card_classifier(context, CurationStage.CARD_CLASSIFY).results
    residual_raw = _payload(context, CurationStage.CARD_RESIDUAL).get("classifier")
    if residual_raw is None:
        return primary
    try:
        residual = ClassifierResult.model_validate(residual_raw).results
    except ValueError as exc:
        raise PinnedInputChanged("card-centric residual classifier artifact is malformed") from exc
    combined = (*primary, *residual)
    if len({item.note_id for item in combined}) != len(combined):
        raise PinnedInputChanged("card-centric classifiers judged one note more than once")
    return tuple(sorted(combined, key=lambda item: item.note_id))


def _validate_card_residual_v2_semantic_audit(context: StageContext) -> None:
    """Validate a present v2 S6 eligibility audit before downstream use.

    Missing fields are tolerated for previously persisted v2 artifacts; a new
    S6 output always carries the whole additive audit as one deterministic set.
    """
    payload = _payload(context, CurationStage.CARD_RESIDUAL)
    if not isinstance(payload, dict):
        raise PinnedInputChanged("card-centric residual semantic eligibility audit is malformed")
    audit_fields = (
        "semantic_eligibility_audit_version",
        "semantic_eligibility_audit_domain",
        "embedding_unavailable_blank_note_ids",
        "searchable_note_count",
        "searchable_note_ids_sha256",
    )
    present_fields = set(audit_fields) & set(payload)
    if not present_fields:
        return
    if present_fields != set(audit_fields):
        raise PinnedInputChanged("card-centric residual semantic eligibility audit is malformed")
    version = payload["semantic_eligibility_audit_version"]
    domain = payload["semantic_eligibility_audit_domain"]
    blank_note_ids = payload["embedding_unavailable_blank_note_ids"]
    searchable_count = payload["searchable_note_count"]
    searchable_ids_sha256 = payload["searchable_note_ids_sha256"]
    if (
        type(version) is not str
        or type(domain) is not str
        or version != _CARD_CENTRIC_V2_S6_SEMANTIC_ELIGIBILITY_AUDIT_VERSION
        or domain != _CARD_CENTRIC_V2_S6_SEMANTIC_ELIGIBILITY_AUDIT_DOMAIN
        or type(searchable_count) is not int
        or searchable_count < 0
        or type(blank_note_ids) is not list
        or any(type(note_id) is not int or note_id <= 0 for note_id in blank_note_ids)
        or blank_note_ids != sorted(blank_note_ids)
        or len(set(blank_note_ids)) != len(blank_note_ids)
        or type(searchable_ids_sha256) is not str
        or len(searchable_ids_sha256) != 64
        or any(character not in "0123456789abcdef" for character in searchable_ids_sha256)
    ):
        raise PinnedInputChanged("card-centric residual semantic eligibility audit is malformed")
    cards = {
        card.note_id: card for card in _card_records(_payload(context, CurationStage.SOURCE_INDEX))
    }
    _, expected = _card_residual_v2_semantic_audit(cards)
    if (
        version != expected["semantic_eligibility_audit_version"]
        or domain != expected["semantic_eligibility_audit_domain"]
        or blank_note_ids != expected["embedding_unavailable_blank_note_ids"]
        or searchable_count != expected["searchable_note_count"]
        or searchable_ids_sha256 != expected["searchable_note_ids_sha256"]
    ):
        raise PinnedInputChanged("card-centric residual semantic eligibility audit changed")


def _card_coverage_payload(context: StageContext) -> dict[str, dict[str, Any]]:
    raw = _payload(context, CurationStage.CARD_COVERAGE).get("coverage")
    if not isinstance(raw, dict):
        raise PinnedInputChanged("card-centric coverage artifact is malformed")
    return cast(dict[str, dict[str, Any]], raw)


def _merged_card_coverage(context: StageContext) -> dict[str, dict[str, Any]]:
    coverage = {
        key: {"status": value["status"], "evidence": list(value.get("evidence", []))}
        for key, value in _card_coverage_payload(context).items()
    }
    source = _card_source_index(context)
    is_v2 = context.job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V2
    for item in _all_card_classifications(context):
        evidence_quality = evidence_quality_v2(item, source) if is_v2 else None
        eligible = evidence_quality is not None if is_v2 else selection_eligible(item, source)
        if eligible:
            for concept_id in item.covered_concept_ids:
                if concept_id in coverage:
                    coverage[concept_id]["status"] = "covered"
                    evidence = {
                        "note_id": item.note_id,
                        "supporting_passage_ids": list(item.supporting_passage_ids),
                    }
                    if evidence_quality is not None:
                        evidence["evidence_quality"] = evidence_quality.value
                    coverage[concept_id]["evidence"].append(evidence)
    if is_v2:
        fast, _ = _card_fast_classifier(context)
        for fast_item in fast.results:
            if not fast_selection_eligible_v2(fast_item, source):
                continue
            for concept_id in fast_item.grounded_concept_ids:
                if concept_id in coverage:
                    coverage[concept_id]["status"] = "covered"
                    coverage[concept_id]["evidence"].append(
                        {
                            "note_id": fast_item.note_id,
                            "supporting_passage_ids": list(fast_item.supporting_passage_ids),
                            "evidence_quality": "fast_pass",
                        }
                    )
    return coverage


def _card_generated(context: StageContext) -> tuple[GeneratedCardResolution, ...]:
    raw = _payload(context, CurationStage.CARD_GAP_FILL).get("resolutions", [])
    try:
        return tuple(GeneratedCardResolution.model_validate(value) for value in raw)
    except (TypeError, ValueError) as exc:
        raise PinnedInputChanged("card-centric gap artifact is malformed") from exc


def _card_deduped(context: StageContext) -> tuple[GeneratedCardResolution, ...]:
    raw = _payload(context, CurationStage.DEDUPE).get("resolutions", [])
    try:
        return tuple(GeneratedCardResolution.model_validate(value) for value in raw)
    except (TypeError, ValueError) as exc:
        raise PinnedInputChanged("card-centric dedupe artifact is malformed") from exc


def _card_dedupe_reviews(context: StageContext) -> tuple[SemanticDedupeReview, ...]:
    raw = _payload(context, CurationStage.DEDUPE).get("semantic_dedupe_reviews", [])
    if not isinstance(raw, list):
        raise PinnedInputChanged("card-centric semantic dedupe reviews are malformed")
    try:
        reviews = tuple(SemanticDedupeReview.model_validate(value) for value in raw)
    except (TypeError, ValueError) as exc:
        raise PinnedInputChanged("card-centric semantic dedupe reviews are malformed") from exc
    if len({review.card_id for review in reviews}) != len(reviews):
        raise PinnedInputChanged("card-centric semantic dedupe reviews repeat a card")
    return tuple(sorted(reviews, key=lambda review: review.card_id))


def _validate_semantic_dedupe_reviews(
    reviews: Sequence[SemanticDedupeReview],
    generated: Sequence[GeneratedCardResolution],
) -> None:
    generated_by_id = {item.card_id: item for item in generated}
    for review in reviews:
        item = generated_by_id.get(review.card_id)
        if item is None or item.status != "generated" or item.fact_id != review.fact_id:
            raise PinnedInputChanged("semantic dedupe review does not match a generated card")


def _dedupe_terminal_resolutions(
    generated: Sequence[GeneratedCardResolution],
    semantic_dedupe_reviews: Sequence[SemanticDedupeReview] = (),
) -> list[dict[str, Any]]:
    """Persist one frozen terminal fact per resolved fact.

    A post-retry semantic review is deliberately non-terminal.  Omit its full
    fact until manual review resolves it, rather than recording an incomplete
    split set as generated or silently declaring it unique.
    """
    _validate_semantic_dedupe_reviews(semantic_dedupe_reviews, generated)
    reviewed_fact_ids = {review.fact_id for review in semantic_dedupe_reviews}
    rows_by_fact: dict[str, list[GeneratedCardResolution]] = {}
    for item in generated:
        if item.fact_id not in reviewed_fact_ids:
            rows_by_fact.setdefault(item.fact_id, []).append(item)

    resolutions: list[GeneratedFactResolution] = []
    for fact_id in sorted(rows_by_fact):
        rows = rows_by_fact[fact_id]
        statuses = {row.status for row in rows}
        if len(statuses) != 1:
            raise PinnedInputChanged("dedupe fact has conflicting terminal states")
        status = rows[0].status
        if status == "generated":
            resolutions.append(
                GeneratedFactResolution(
                    fact_id=fact_id,
                    kind=GeneratedResolutionKind.GENERATED,
                    generated_card_ids=tuple(
                        row.card_id
                        for row in sorted(
                            rows,
                            key=lambda row: (
                                row.split_index is None,
                                row.split_index if row.split_index is not None else 0,
                                row.card_id,
                            ),
                        )
                    ),
                )
            )
            continue
        if len(rows) != 1:
            raise PinnedInputChanged("dedupe fact has repeated non-split terminal states")
        row = rows[0]
        if status == "unresolved":
            resolutions.append(
                GeneratedFactResolution(
                    fact_id=fact_id,
                    kind=GeneratedResolutionKind.UNRESOLVED,
                    unresolved_reason=row.reason,
                )
            )
            continue
        duplicate_of = (
            DuplicateIdentity(existing_note_id=row.duplicate_of_existing_note_id)
            if row.duplicate_of_existing_note_id is not None
            else DuplicateIdentity(generated_card_id=row.duplicate_of_generated_card_id)
        )
        resolutions.append(
            GeneratedFactResolution(
                fact_id=fact_id,
                kind=GeneratedResolutionKind.DUPLICATE_OF_EXISTING,
                duplicate_of=duplicate_of,
            )
        )
    return [resolution.model_dump(mode="json") for resolution in resolutions]


def record_exhausted_semantic_dedupe_review(
    dedupe_payload: Mapping[str, Any],
    review: SemanticDedupeReview,
) -> dict[str, Any]:
    """P1/I0 hook: immutably append one post-retry semantic review record.

    P1 invokes this only after the worker exhausts retries for a propagated
    semantic-provider failure. It must first create ``review`` through the
    dedupe service's lexical advisory adapter; normal S8 classification never
    calls this hook or catches that provider failure.
    """
    raw_resolutions = dedupe_payload.get("resolutions", [])
    if not isinstance(raw_resolutions, list):
        raise PinnedInputChanged("card-centric dedupe artifact is malformed")
    try:
        generated = tuple(
            GeneratedCardResolution.model_validate(value) for value in raw_resolutions
        )
        existing = tuple(
            SemanticDedupeReview.model_validate(value)
            for value in dedupe_payload.get("semantic_dedupe_reviews", [])
        )
    except (TypeError, ValueError) as exc:
        raise PinnedInputChanged("card-centric dedupe artifact is malformed") from exc
    _validate_semantic_dedupe_reviews((review,), generated)
    if any(item.card_id == review.card_id for item in existing):
        raise PinnedInputChanged("semantic dedupe review already exists for this card")
    payload = dict(dedupe_payload)
    reviews = tuple(sorted((*existing, review), key=lambda item: item.card_id))
    payload["semantic_dedupe_reviews"] = [item.model_dump(mode="json") for item in reviews]
    payload["terminal_resolutions"] = _dedupe_terminal_resolutions(generated, reviews)
    return payload


def _combined_usage(label: str, usages: Sequence[StageUsage]) -> StageUsage | None:
    if not usages:
        return None
    joined = json.dumps([value.request_id for value in usages], separators=(",", ":"))
    return StageUsage(
        f"{label}:{hashlib.sha256(joined.encode()).hexdigest()[:24]}",
        sum(value.input_tokens for value in usages),
        sum(value.output_tokens for value in usages),
        sum(value.cost_microusd for value in usages),
    )


def _card_tokens(value: str) -> set[str]:
    return {
        token
        for token in __import__("re").findall(r"[a-z0-9]+", value.casefold())
        if len(token) > 2
    }


def _normalized_card_note(card: CardRecord) -> NormalizedNote:
    """Minimal complete adapter; its note identity remains the Anki note ID."""
    return NormalizedNote(
        note_id=card.note_id,
        model_name="Cloze",
        text=card.text,
        extra=card.extra,
        raw_fields={"Text": card.text, "Extra": card.extra},
        tags=card.tags,
        card_ids=(),
        media=(),
        token_signature=hashlib.sha256(f"{card.text}\0{card.extra}".encode()).hexdigest(),
        content_sha256=card.content_sha256,
        deck_names=card.deck_names,
    )


def _dedupe_gap_proposal(
    item: GeneratedCardResolution,
    context: StageContext,
) -> GapCardProposal:
    """Adapt a generated row with its stable card identity for semantic dedupe."""
    stage_model = context.job.resolved_model_config.gap_fill_s7
    return GapCardProposal(
        concept_id=item.concept_id,
        note_type="Cloze",
        fields={"Text": item.text, "Extra": item.extra},
        source_refs=(),
        evidence_ids=item.evidence_ids,
        initial_tags=(),
        provider=ProviderName(stage_model.provider),
        model=stage_model.model,
        prompt_version=context.job.gap_prompt_version,
        confidence=1.0,
        content_hash=hashlib.sha256(f"{item.text}\0{item.extra}".encode()).hexdigest(),
        provenance={"card_centric_generated_card_id": item.card_id},
        fact_id=item.fact_id,
        split=item.split,
    )


def _mandatory_card_note_ids(
    classifications: Sequence[CardClassification],
    fast_classifications: Sequence[FastCardClassification],
    ledger: CardConceptLedger,
    source: CardCentricSourceIndex,
    *,
    v2: bool,
) -> tuple[int, ...]:
    priorities = {
        concept.concept_id
        for concept in ledger.concepts
        if concept.importance == "high" or concept.emphasis_flag
    }
    note_ids = {
        item.note_id
        for item in classifications
        if (selection_eligible_v2(item, source) if v2 else selection_eligible(item, source))
        and priorities & set(item.covered_concept_ids)
    }
    if v2:
        note_ids.update(
            item.note_id
            for item in fast_classifications
            if fast_selection_eligible_v2(item, source)
            and priorities & set(item.grounded_concept_ids)
        )
    return tuple(sorted(note_ids))


def _v2_card_candidates(
    context: StageContext,
    classifications: Sequence[CardClassification],
    fast_classifications: Sequence[FastCardClassification],
    fallback_note_ids: Sequence[int],
    selected_note_ids: set[int],
    source: CardCentricSourceIndex,
    selection_metadata_by_identity: Mapping[str, dict[str, object]] | None = None,
) -> tuple[Candidate, ...]:
    """Materialize scoped S4 terminals plus legitimate unscoped residuals."""
    cards = {
        card.note_id: card for card in _card_records(_payload(context, CurationStage.SOURCE_INDEX))
    }
    scope = TagScopeResult.model_validate(_payload(context, CurationStage.CARD_TAG_SCOPE)["scope"])
    all_thorough = {item.note_id: item for item in classifications}
    scoped_ids = set(scope.scoped_note_ids)
    thorough = {note_id: item for note_id, item in all_thorough.items() if note_id in scoped_ids}
    residual = {
        note_id: item for note_id, item in all_thorough.items() if note_id not in scoped_ids
    }
    fast = {item.note_id: item for item in _terminal_fast_classifications(fast_classifications)}
    needs_review_ids = {
        item.note_id for item in fast_classifications if item.verdict == "NEEDS_REVIEW"
    }
    if CurationStage.CARD_FAST_CLASSIFY in context.prior_payloads:
        _, pre_excluded_note_ids = _card_fast_classifier(context)
    else:
        pre_excluded_note_ids = tuple(fallback_note_ids)
    fallback = set(_unrecovered_s4a_exclusion_note_ids(pre_excluded_note_ids, classifications))
    selection_metadata_by_identity = selection_metadata_by_identity or {}
    if needs_review_ids - set(thorough):
        raise PinnedInputChanged("v2 S4b NEEDS_REVIEW rows lack S4c terminal results")
    if set(thorough) & (set(fast) | fallback) or set(fast) & fallback:
        raise PinnedInputChanged("v2 classification artifacts contain duplicate note identities")
    if set(thorough) | set(fast) | fallback != scoped_ids:
        raise PinnedInputChanged("v2 classification artifacts do not conserve scoped notes")
    candidates: list[Candidate] = []
    for note_id in sorted(scoped_ids | set(residual)):
        card = cards.get(note_id)
        if card is None:
            raise PinnedInputChanged("v2 classification artifact references an unknown card")
        concept_ids: tuple[str, ...]
        verdict: str
        predicted_band: str
        eligible: bool
        flags: tuple[str, ...]
        reason: str
        provenance_kind: str
        if note_id in residual:
            thorough_item = residual[note_id]
            concept_ids = thorough_item.covered_concept_ids
            verdict = thorough_item.verdict.lower()
            predicted_band = thorough_item.verdict
            eligible = selection_eligible_v2(thorough_item, source)
            flags = thorough_item.flags
            reason = thorough_item.reason
            provenance_kind = "residual"
        elif note_id in thorough:
            thorough_item = thorough[note_id]
            concept_ids = thorough_item.covered_concept_ids
            verdict = thorough_item.verdict.lower()
            predicted_band = thorough_item.verdict
            eligible = selection_eligible_v2(thorough_item, source)
            flags = thorough_item.flags
            reason = thorough_item.reason
            provenance_kind = "thorough"
        elif note_id in fast:
            fast_item = fast[note_id]
            concept_ids = fast_item.grounded_concept_ids
            verdict = {
                "LIKELY_YES": "keep",
                "LIKELY_NO": "drop",
                "NEEDS_REVIEW": "uncertain",
            }[fast_item.verdict]
            predicted_band = fast_item.verdict
            eligible = fast_selection_eligible_v2(fast_item, source)
            flags = fast_item.flags
            reason = fast_item.reason
            provenance_kind = "fast"
        elif note_id in fallback:
            concept_ids = ()
            verdict = "uncertain"
            predicted_band = "PREFILTER_FALLBACK"
            eligible = False
            flags = ()
            reason = "documented semantic prefilter fallback"
            provenance_kind = "prefilter_fallback"
        else:
            raise PinnedInputChanged("v2 classification terminal is unavailable")
        candidates.append(
            Candidate(
                note_id=note_id,
                content_hash=card.content_sha256,
                best_concept_id=concept_ids[0] if concept_ids else "unmapped",
                provenance={
                    "card_centric_v2": {
                        "classification_kind": provenance_kind,
                        "covered_concept_ids": list(concept_ids),
                        "flags": list(flags),
                        "selection_eligible": eligible,
                    },
                    "selection": selection_metadata_by_identity.get(
                        f"existing:{note_id}",
                        {"selected": False},
                    ),
                },
                scores={},
                predicted_band=predicted_band,
                verdict=verdict,
                confidence=1.0 if eligible else 0.5,
                reason=reason,
                context_trap="context_trap" in flags,
                recall_direction="card_centric_v2",
                mnemonic_classification="none",
                dedupe_disposition="eligible" if note_id in selected_note_ids else "excluded",
                selected=note_id in selected_note_ids,
            )
        )
    return tuple(candidates)


def _v2_reconciliation_classifications(
    thorough: Sequence[CardClassification],
    fast: Sequence[FastCardClassification],
    fallback_note_ids: Sequence[int],
    scope: TagScopeResult,
) -> tuple[AuditResolution, ...]:
    """Project only the scoped S4 terminal universe into A3's audit view."""
    scoped_ids = set(scope.scoped_note_ids)
    thorough_by_id = {item.note_id: item for item in thorough if item.note_id in scoped_ids}
    fast_by_id = {item.note_id: item for item in _terminal_fast_classifications(fast)}
    needs_review_ids = {item.note_id for item in fast if item.verdict == "NEEDS_REVIEW"}
    fallback = set(
        _unrecovered_s4a_exclusion_note_ids(fallback_note_ids, tuple(thorough_by_id.values()))
    )
    if needs_review_ids - set(thorough_by_id):
        raise PinnedInputChanged("v2 reconciliation has unresolved S4b NEEDS_REVIEW rows")
    if set(thorough_by_id) & (set(fast_by_id) | fallback) or set(fast_by_id) & fallback:
        raise PinnedInputChanged("v2 reconciliation has duplicate scoped classifications")
    if set(thorough_by_id) | set(fast_by_id) | fallback != scoped_ids:
        raise PinnedInputChanged("v2 reconciliation cannot account for every scoped card")
    rows: list[AuditResolution] = []
    for note_id in sorted(scope.scoped_note_ids):
        if note_id in thorough_by_id:
            verdict = cast(
                Literal["keep", "drop", "uncertain"],
                {"YES": "keep", "NO": "drop", "MAYBE": "uncertain"}[
                    thorough_by_id[note_id].verdict
                ],
            )
        elif note_id in fast_by_id:
            verdict = cast(
                Literal["keep", "drop", "uncertain"],
                {
                    "LIKELY_YES": "keep",
                    "LIKELY_NO": "drop",
                    "NEEDS_REVIEW": "uncertain",
                }[fast_by_id[note_id].verdict],
            )
        else:
            verdict = "uncertain"
        rows.append(AuditResolution(nid=note_id, verdict=verdict))
    return tuple(rows)


def _token_overlap(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left or right else 0.0


def _card_ledger_prompt(
    catalog: AnkiPromptCatalogService,
    version: PipelineContractVersion,
) -> str:
    asset = (
        "card-centric-ledger-v2"
        if version is PipelineContractVersion.CARD_CENTRIC_V2
        else "card-centric-ledger-v1"
    )
    return AnkiPromptLibrary(catalog.bundled_directory).load(asset).content


def _card_gap_prompt(
    catalog: AnkiPromptCatalogService,
    version: PipelineContractVersion,
) -> str:
    asset = (
        "card-centric-gap-v2"
        if version is PipelineContractVersion.CARD_CENTRIC_V2
        else "card-centric-gap-v1"
    )
    return AnkiPromptLibrary(catalog.bundled_directory).load(asset).content


def _card_fast_classifier_prompt(catalog: AnkiPromptCatalogService) -> str:
    return AnkiPromptLibrary(catalog.bundled_directory).load("card-centric-fast-classifier").content


_CARD_V2_PINNED_PROMPT_SCHEMAS = {
    "card-centric-ledger-v2": "lcl_v2",
    "card-centric-fast-classifier": "card_centric_fast_classify_v2",
    "card-centric-classifier": "card_centric_classify_v1",
    "card-centric-gap-v2": "gap_cards_v2",
}


def _pinned_card_v2_lecture_metadata(context: StageContext) -> PinnedLectureMetadata:
    """Consume P1's immutable, stage-identity-bound lecture document.

    P1 prepares ``replay_inputs["pinned_lecture"]`` before hashing the stage.
    Reading either the mutable repository title or a second preflight copy would
    let the provider input diverge from that identity, so missing integration
    fails closed.
    """
    raw = _card_v2_replay_input(context, "pinned_lecture")
    try:
        metadata = PinnedLectureMetadata.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise PinnedInputChanged("P1 pinned lecture metadata is unavailable or malformed") from exc
    if metadata.lecture_id != context.job.lecture_id:
        raise PinnedInputChanged("P1 pinned lecture metadata names another lecture")
    return metadata


def _pinned_card_v2_a11_history(context: StageContext) -> tuple[float, ...]:
    """Consume the distinct-job A11 window frozen into P1's S9 identity."""
    raw = _card_v2_replay_input(context, "a11_history")
    try:
        history = A11HistorySnapshot.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise PinnedInputChanged("P1 frozen A11 history is unavailable or malformed") from exc
    return tuple(entry.yes_rate for entry in history.entries)


def _card_v2_replay_input(context: StageContext, key: str) -> object:
    replay_inputs = getattr(context, "replay_inputs", None)
    if not isinstance(replay_inputs, Mapping):
        return None
    return replay_inputs.get(key)


def _validate_card_gap_batch_v2(
    batch: CardGapBatch,
    expected_fact_ids: set[str],
    forbidden_cloze_targets_by_fact: Mapping[str, tuple[str, ...]],
) -> None:
    """Enforce strict new S7 terminal structures before stage persistence."""
    returned_fact_ids = {item.fact_id for item in batch.resolutions}
    if returned_fact_ids != expected_fact_ids:
        raise PinnedInputChanged("card-centric v2 gap output must resolve every requested fact")
    for fact_id in sorted(expected_fact_ids):
        matching = [item for item in batch.resolutions if item.fact_id == fact_id]
        unresolved = [item for item in matching if item.status == "unresolved"]
        generated = [item for item in matching if item.status == "generated"]
        if unresolved:
            if len(unresolved) != 1 or generated:
                raise PinnedInputChanged(
                    f"Fact {fact_id}: unresolved output must be one exclusive row"
                )
            continue
        if not generated:
            raise PinnedInputChanged(f"Fact {fact_id}: resolution is missing")
        if len(generated) == 1:
            card = generated[0]
            if card.split or card.split_index is not None:
                raise PinnedInputChanged(f"Fact {fact_id}: unsplit output must omit split_index")
            continue
        indices = [card.split_index for card in generated]
        if (
            any(not card.split for card in generated)
            or any(index is None for index in indices)
            or indices != list(range(1, len(generated) + 1))
        ):
            raise PinnedInputChanged(
                f"Fact {fact_id}: split output requires sequential split_index values"
            )
    violations = _forbidden_cloze_rows(
        tuple(
            GeneratedResolution(
                card_id=item.fact_id,
                fact_id=item.fact_id,
                text=item.text,
                extra=item.extra,
                split=item.split,
                split_index=item.split_index,
            )
            for item in batch.resolutions
            if item.status == "generated"
        ),
        dict(forbidden_cloze_targets_by_fact),
        (),
    )
    if violations:
        details = "; ".join(
            f"{fact_id}: {', '.join(forbidden_cloze_targets_by_fact.get(fact_id, ()))}"
            for fact_id in sorted(set(violations))
        )
        raise PinnedInputChanged(
            "card-centric v2 gap output blanks forbidden targets for facts: " + details
        )


def _pinned_card_v2_prompt(context: StageContext, prompt_id: str) -> str:
    """Read an immutable v2 internal prompt from S0, never a live catalog file."""
    expected_schema = _CARD_V2_PINNED_PROMPT_SCHEMAS[prompt_id]
    raw_snapshot = _payload(context, CurationStage.PREFLIGHT).get("prompt_snapshot", [])
    if not isinstance(raw_snapshot, list):
        raise PinnedInputChanged("Pinned prompt snapshot is malformed")
    entries = [
        value for value in raw_snapshot if isinstance(value, dict) and value.get("id") == prompt_id
    ]
    if len(entries) != 1:
        raise PinnedInputChanged(f"Pinned v2 prompt {prompt_id} is unavailable or duplicated")
    entry = entries[0]
    content = entry.get("content")
    prompt_hash = entry.get("prompt_hash")
    raw_metadata = entry.get("metadata")
    try:
        metadata = PromptMetadata.model_validate(raw_metadata)
    except (TypeError, ValueError):
        metadata = None
    if (
        not isinstance(content, str)
        or not content.strip()
        or not isinstance(prompt_hash, str)
        or hashlib.sha256(content.encode("utf-8")).hexdigest()[:12] != prompt_hash
        or metadata is None
        or metadata.id != prompt_id
        or metadata.schema_name != expected_schema
        or metadata.response_format != "json"
    ):
        raise PinnedInputChanged("Pinned v2 prompt snapshot is malformed")
    return content


def _v3_phase_f_inputs(
    context: StageContext,
) -> tuple[
    dict[str, Any], LectureScope, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]
]:
    if context.job.pipeline_contract_version is not PipelineContractVersion.CARD_CENTRIC_V3:
        raise PinnedInputChanged("Phase F requires the card_centric_v3 contract")
    r0 = _payload(context, CurationStage.V3_R0_PREFLIGHT)
    r3 = _payload(context, CurationStage.V3_R3_SCOPE)
    r4 = _v3_r4_verification(context, _payload(context, CurationStage.V3_R4_INDEX_VERIFICATION), r0)
    r5 = _payload(context, CurationStage.V3_R5_RETRIEVAL)
    r6 = _payload(context, CurationStage.V3_R6_CALIBRATION)
    r7 = _payload(context, CurationStage.V3_R7_CLASSIFICATION)
    try:
        scope = LectureScope.model_validate(r3["scope"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PinnedInputChanged("Pinned Phase F scope is malformed") from exc
    policy = _r3_policy(context, r0)
    for artifact, message in (
        (r5, "Pinned R5 retrieval identity changed"),
        (r6, "Pinned R6 calibration identity changed"),
        (r7, "Pinned R7 classification identity changed"),
    ):
        _v3_artifact_valid(artifact, message)
    if (
        scope.policy_sha256 != policy.policy_sha256
        or r5.get("policy_sha256") != policy.policy_sha256
        or r5.get("scope_sha256") != scope.scope_sha256
        or r5.get("r4_verification_sha256") != r4["verification_sha256"]
        or r6.get("policy_sha256") != policy.policy_sha256
        or r6.get("scope_sha256") != scope.scope_sha256
        or r6.get("r5_artifact_sha256") != r5.get("artifact_sha256")
        or r7.get("policy_sha256") != policy.policy_sha256
        or r7.get("scope_sha256") != scope.scope_sha256
        or r7.get("r6_artifact_sha256") != r6.get("artifact_sha256")
    ):
        raise PinnedInputChanged("Pinned R0/R3/R5/R6/R7 closure changed")
    _r7_routes(context, r0)
    if (
        r5.get("semantic_generation") != r4["semantic_generation"]
        or r6.get("semantic_generation") != r4["semantic_generation"]
    ):
        raise PinnedInputChanged("Pinned Phase F semantic generation changed")
    return r0, scope, r4, r5, r6, r7


def _v3_artifact_valid(payload: Mapping[str, Any], message: str) -> None:
    if payload.get("artifact_sha256") != canonical_sha256(
        {key: value for key, value in payload.items() if key != "artifact_sha256"}
    ):
        raise PinnedInputChanged(message)


def _v3_blocked_product(
    kind: str,
    r7: Mapping[str, Any],
    error: str,
    *,
    costs: StageCostSession | None = None,
) -> StageProduct:
    payload = {"r7_artifact_sha256": r7["artifact_sha256"], "records": [], "blocking": error}
    if costs is not None:
        _seal_v3_costs(costs, payload)
    payload["artifact_sha256"] = canonical_sha256(payload)
    return StageProduct(kind=kind, payload=payload, blocking_error=error)


def _v3_r7_rows(r7: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows, bundles = r7.get("final_partition"), r7.get("bundles")
    if not isinstance(rows, list) or not isinstance(bundles, list):
        raise PinnedInputChanged("Pinned R7 final partition is malformed")
    bundle_ids = [item.get("bundle_id") for item in bundles if isinstance(item, Mapping)]
    row_ids = [item.get("bundle_id") for item in rows if isinstance(item, Mapping)]
    if (
        len(bundle_ids) != len(bundles)
        or len(row_ids) != len(rows)
        or set(bundle_ids) != set(row_ids)
        or len(row_ids) != len(set(row_ids))
        or r7.get("bundles_sha256") != canonical_payload_sha256(bundles)
    ):
        raise PinnedInputChanged("Pinned R7 final partition closure changed")
    return cast(list[Mapping[str, Any]], rows)


def _v3_bundle_fact_id(r7: Mapping[str, Any], bundle_id: str) -> str:
    for bundle in cast(list[Mapping[str, Any]], r7["bundles"]):
        if bundle.get("bundle_id") == bundle_id and isinstance(bundle.get("fact_id"), str):
            return cast(str, bundle["fact_id"])
    raise PinnedInputChanged("Pinned R7 bundle is unavailable")


def _v3_bundle_candidate_note_id(r7: Mapping[str, Any], bundle_id: str) -> int:
    for bundle in cast(list[Mapping[str, Any]], r7["bundles"]):
        candidate = bundle.get("candidate")
        if bundle.get("bundle_id") == bundle_id and isinstance(candidate, Mapping):
            note_id = candidate.get("note_id")
            if type(note_id) is int and note_id > 0:
                return note_id
    raise PinnedInputChanged("Pinned R7 bundle candidate is unavailable")


def _v3_records_by_fact(payload: Mapping[str, Any], name: str) -> dict[str, Mapping[str, Any]]:
    raw = payload.get(name)
    if not isinstance(raw, list):
        raise PinnedInputChanged(f"Pinned {name} records are malformed")
    records = {
        cast(str, item.get("fact_id")): item
        for item in raw
        if isinstance(item, Mapping) and isinstance(item.get("fact_id"), str)
    }
    if len(records) != len(raw):
        raise PinnedInputChanged(f"Pinned {name} fact closure is malformed")
    return records


def _v3_r8_raw_safety(
    r5: Mapping[str, Any],
    r6: Mapping[str, Any],
    expected_ids: Mapping[int, str],
    semantic_ids: Mapping[int, str],
    threshold: float,
    raw_limit: int,
) -> list[str]:
    candidates = r5.get("candidates")
    raw_semantic, raw_lexical = r5.get("raw_semantic"), r5.get("raw_lexical")
    if (
        not isinstance(candidates, list)
        or not isinstance(raw_semantic, list)
        or not isinstance(raw_lexical, list)
    ):
        raise PinnedInputChanged("Pinned R5 raw traces are malformed")
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or expected_ids.get(
            int(candidate.get("note_id", 0))
        ) != candidate.get("content_sha256"):
            raise PinnedInputChanged("Pinned R5 candidate closure changed")
    for lane in raw_semantic:
        if not isinstance(lane, list):
            raise PinnedInputChanged("Pinned R5 semantic trace is malformed")
        for hit in lane:
            if (
                not isinstance(hit, Mapping)
                or int(hit.get("note_id", 0)) not in expected_ids
                or not isinstance(hit.get("score"), (int, float))
                or semantic_ids.get(int(hit.get("note_id", 0))) != hit.get("content_hash")
            ):
                raise PinnedInputChanged("Pinned R5 semantic raw-hit closure changed")
    for hit in raw_lexical:
        if not isinstance(hit, Mapping) or int(hit.get("note_id", 0)) not in expected_ids:
            raise PinnedInputChanged("Pinned R5 lexical raw-hit closure changed")
    problems: list[str] = []
    if len(raw_lexical) >= raw_limit:
        problems.append("lexical raw cap filled")
    if any(
        len(lane) >= raw_limit and float(lane[-1]["score"]) >= threshold
        for lane in raw_semantic
        if lane
    ):
        problems.append("semantic raw cap ends above threshold")
    if r6.get("per_fact_cap_excluded_note_ids") or r6.get("global_cap_excluded_note_ids"):
        problems.append("R6 candidate cap excluded a possible card")
    return problems


def _v3_residual_qualifies(candidate: Mapping[str, Any], threshold: float) -> bool:
    return (
        bool(candidate.get("exact_match_reasons"))
        or candidate.get("lexical_rank") is not None
        or (
            candidate.get("semantic_score") is not None
            and float(candidate["semantic_score"]) >= threshold
        )
    )


def _v3_positive_initial(rows: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        row.get("disposition") == "keep"
        or (
            row.get("disposition") == "redundant"
            and bool(row.get("supporting_passage_ids"))
            and isinstance(row.get("redundant_with_candidate_id"), str)
        )
        for row in rows
    )


def _v3_residual_bundle_from_fact(
    concept: ScopedConcept,
    fact: ScopedFact,
    source_evidence: Mapping[str, str],
    policy_sha256: str,
    scope_sha256: str,
    candidate: Mapping[str, Any],
) -> CandidateEvidenceBundle:
    """Project a residual candidate without depending on an initial R6 representative."""
    note_id = int(candidate["note_id"])
    projected = concept.model_copy(
        update={"facts": (fact,), "source_evidence_ids": fact.evidence_ids}
    )
    seed: dict[str, Any] = {
        "bundle_id": f"residual:{concept.concept_id}:{fact.fact_id}:note:{note_id}",
        "policy_sha256": policy_sha256,
        "scope_sha256": scope_sha256,
        "concept": projected,
        "fact_id": fact.fact_id,
        "candidate": CandidateCardFields(
            candidate_id=f"note:{note_id}",
            note_id=note_id,
            text=str(candidate["text"]),
            extra=str(candidate["extra"]),
            tags=tuple(sorted(candidate["tags"], key=str.casefold)),
            deck="\n".join(sorted(candidate["decks"])),
        ),
        # R8 intentionally discards R5's tag boost and boosted calibrated score.
        "retrieval_scores": _v3_residual_scores(candidate),
        "exact_match_reasons": tuple(sorted(candidate["exact_match_reasons"])),
        "selected_passages": tuple(
            SelectedPassage(
                passage_id=evidence_id,
                text=source_evidence[evidence_id],
                selection_reason="fact_scope_evidence",
            )
            for evidence_id in fact.evidence_ids
        ),
        "duplicate_sibling_ids": (),
        "allowed_concept_ids": (concept.concept_id,),
        "allowed_fact_ids": (fact.fact_id,),
        "allowed_passage_ids": fact.evidence_ids,
        "input_byte_estimate": 0,
        "input_token_estimate": 0,
        "max_input_bytes": MAX_BUNDLE_BYTES,
        "max_input_tokens": MAX_BUNDLE_TOKENS,
        "truncated": False,
        "degraded": False,
    }
    return _r7_exact_bundle(seed)


def _v3_residual_scores(candidate: Mapping[str, Any]) -> tuple[RetrievalScore, ...]:
    return tuple(
        sorted(
            (
                RetrievalScore(identity=name, score=float(candidate[name]))
                for name in ("base_rrf", "semantic_score", "semantic_rank", "lexical_rank")
                if candidate.get(name) is not None
            ),
            key=lambda item: item.identity,
        )
    )


def _v3_scope_evidence(scope: LectureScope, r3: Mapping[str, Any]) -> dict[str, str]:
    bundle = r3.get("source_bundle")
    if (
        not isinstance(bundle, Mapping)
        or canonical_payload_sha256(bundle) != scope.source_bundle_sha256
    ):
        raise PinnedInputChanged("Pinned R3 source bundle identity changed")
    values: dict[str, str] = {}
    for item in cast(list[Mapping[str, Any]], bundle.get("evidence", [])):
        evidence_id, text = item.get("evidence_id"), item.get("normalized_text")
        if not isinstance(evidence_id, str) or not isinstance(text, str):
            raise PinnedInputChanged("Pinned R3 evidence is malformed")
        values[evidence_id] = text
    if set(values) != {item.evidence_id for item in scope.evidence}:
        raise PinnedInputChanged("Pinned R3 evidence closure changed")
    return values


def _v3_r9_partition(rows: Sequence[Mapping[str, object]], expected: set[str]) -> None:
    grouped: dict[str, list[Mapping[str, object]]] = {fact_id: [] for fact_id in expected}
    for row in rows:
        fact_id = row.get("fact_id")
        if fact_id not in grouped:
            raise PinnedInputChanged("R9 output names an unrequested fact")
        grouped[fact_id].append(row)
    if any(not values for values in grouped.values()):
        raise PinnedInputChanged("R9 output does not partition confirmed missing facts")


def _v3_r8_records(scope: LectureScope, payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    records = payload.get("records")
    if not isinstance(records, list):
        raise PinnedInputChanged("Pinned R8 records are malformed")
    expected = {fact.fact_id: fact for concept in scope.concepts for fact in concept.facts}
    by_fact = {
        cast(str, record.get("fact_id")): record
        for record in records
        if isinstance(record, Mapping) and isinstance(record.get("fact_id"), str)
    }
    if len(by_fact) != len(records) or set(by_fact) != set(expected):
        raise PinnedInputChanged("Pinned R8 records do not partition scoped facts")
    allowed = {"covered_initial", "covered_residual", "unresolved", "confirmed_missing"}
    for fact_id, record in by_fact.items():
        if (
            record.get("state") not in allowed
            or record.get("generation_allowed") is not expected[fact_id].generation_allowed
        ):
            raise PinnedInputChanged("Pinned R8 fact resolution is malformed")
    return [by_fact[fact.fact_id] for concept in scope.concepts for fact in concept.facts]


def _v3_usage(usages: Sequence[StageUsage]) -> StageUsage | None:
    if not usages:
        return None
    return StageUsage(
        request_id=f"r9:{len(usages)}",
        input_tokens=sum(item.input_tokens for item in usages),
        output_tokens=sum(item.output_tokens for item in usages),
        cost_microusd=sum(item.cost_microusd for item in usages),
    )


def _r3_policy(context: StageContext, payload: dict[str, Any]) -> CourseCurationPolicy:
    raw = payload.get("policy")
    try:
        policy = CourseCurationPolicy.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise PinnedInputChanged("Pinned R3 policy is malformed") from exc
    if (
        payload.get("policy_sha256") != policy.policy_sha256
        or payload.get("policy_revision") != policy.revision
        or context.job.policy_sha256 != policy.policy_sha256
        or payload.get("model_config_sha256") != context.job.model_config_sha256
    ):
        raise PinnedInputChanged("Pinned R3 policy identity changed")
    return policy


def _r7_routes(
    context: StageContext, payload: dict[str, Any]
) -> tuple[ResolvedStageModel, ResolvedStageModel, str]:
    cheap = context.job.resolved_model_config.cheap_classify_r7
    thorough = context.job.resolved_model_config.thorough_classify_r7
    if cheap is None or thorough is None:
        raise PinnedInputChanged("Pinned R7 classifier routes are unavailable")
    rate_table_sha256 = payload.get("rate_table_sha256")
    if not isinstance(rate_table_sha256, str):
        raise PinnedInputChanged("Pinned R7 rate table is unavailable")
    if payload.get("cheap_classify_r7") != r7_route_document(cheap) or payload.get(
        "thorough_classify_r7"
    ) != r7_route_document(thorough):
        raise PinnedInputChanged("Pinned R7 classifier route changed")
    try:
        expected = r7_pin_document(cheap, thorough, rate_table_sha256)
    except ClassificationInputError as exc:
        raise PinnedInputChanged(str(exc)) from exc
    if payload.get("r7_classification") != expected:
        raise PinnedInputChanged("Pinned R7 classification envelope changed")
    return cheap, thorough, rate_table_sha256


def _v3_r7_bundles(
    scope: LectureScope,
    r3: Mapping[str, Any],
    r6: Mapping[str, Any],
    policy_sha256: str,
) -> tuple[CandidateEvidenceBundle, ...]:
    """Materialize only fact-cited R3 evidence for each R6 representative."""
    bundle = r3.get("source_bundle")
    if (
        not isinstance(bundle, Mapping)
        or canonical_payload_sha256(bundle) != scope.source_bundle_sha256
    ):
        raise PinnedInputChanged("Pinned R3 source bundle identity changed")
    raw_evidence = bundle.get("evidence")
    if not isinstance(raw_evidence, list):
        raise PinnedInputChanged("Pinned R3 source bundle is malformed")
    scope_evidence = {item.evidence_id: item for item in scope.evidence}
    evidence: dict[str, Mapping[str, Any]] = {}
    for item in raw_evidence:
        if not isinstance(item, Mapping):
            raise PinnedInputChanged("Pinned R3 source bundle evidence is malformed")
        try:
            evidence_id = str(item["evidence_id"])
            text = str(item["normalized_text"])
            content_sha = str(item["content_sha256"])
        except (KeyError, TypeError) as exc:
            raise PinnedInputChanged("Pinned R3 source bundle evidence is malformed") from exc
        reference = scope_evidence.get(evidence_id)
        if (
            reference is None
            or reference.content_sha256 != content_sha
            or hashlib.sha256(text.encode("utf-8")).hexdigest() != content_sha
            or evidence_id in evidence
        ):
            raise PinnedInputChanged("Pinned R3 evidence content closure changed")
        evidence[evidence_id] = item
    if set(evidence) != set(scope_evidence):
        raise PinnedInputChanged("Pinned R3 source bundle evidence closure changed")
    facts = {fact.fact_id: (concept, fact) for concept in scope.concepts for fact in concept.facts}
    records = r6.get("records")
    if not isinstance(records, list):
        raise PinnedInputChanged("Pinned R6 records are malformed")
    built: list[CandidateEvidenceBundle] = []
    observed_facts: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            raise PinnedInputChanged("Pinned R6 record is malformed")
        concept_id, fact_id = record.get("concept_id"), record.get("fact_id")
        if (
            not isinstance(concept_id, str)
            or not isinstance(fact_id, str)
            or fact_id not in facts
            or facts[fact_id][0].concept_id != concept_id
        ):
            raise PinnedInputChanged("Pinned R6 fact closure changed")
        if fact_id in observed_facts:
            raise PinnedInputChanged("Pinned R6 has duplicate fact records")
        if record.get("fact_sha256") != canonical_sha256(
            {key: value for key, value in record.items() if key != "fact_sha256"}
        ):
            raise PinnedInputChanged("Pinned R6 fact identity changed")
        observed_facts.add(fact_id)
        concept, fact = facts[fact_id]
        candidates = record.get("all_candidates")
        clusters = record.get("clusters")
        if not isinstance(candidates, list) or not isinstance(clusters, list):
            raise PinnedInputChanged("Pinned R6 candidate/cluster closure is malformed")
        by_note: dict[int, Mapping[str, Any]] = {}
        for item in candidates:
            if not isinstance(item, Mapping):
                raise PinnedInputChanged("Pinned R6 candidate identities are malformed")
            note_id = item.get("note_id")
            if type(note_id) is not int or note_id <= 0 or note_id in by_note:
                raise PinnedInputChanged("Pinned R6 candidate identities are malformed")
            by_note[note_id] = item
        if len(by_note) != len(candidates):
            raise PinnedInputChanged("Pinned R6 candidate identities are malformed")
        represented: set[int] = set()
        fact_bundles: list[tuple[float, CandidateEvidenceBundle]] = []
        for cluster in clusters:
            if not isinstance(cluster, Mapping):
                raise PinnedInputChanged("Pinned R6 cluster is malformed")
            representative = cluster.get("representative_note_id")
            siblings = cluster.get("sibling_note_ids")
            missing = cluster.get("missing_vector_note_ids")
            if (
                type(representative) is not int
                or representative <= 0
                or not _r7_positive_ids(siblings)
                or not _r7_positive_ids(missing)
            ):
                raise PinnedInputChanged("Pinned R6 cluster closure changed")
            representative_id = representative
            sibling_ids = cast(list[int], siblings)
            missing_vector_ids = cast(list[int], missing)
            if (
                representative_id not in sibling_ids
                or not set(sibling_ids) <= set(by_note)
                or not set(missing_vector_ids) <= set(sibling_ids)
                or represented.intersection(sibling_ids)
            ):
                raise PinnedInputChanged("Pinned R6 cluster closure changed")
            represented.update(sibling_ids)
            candidate = by_note[representative_id]
            candidate_text = candidate.get("text")
            candidate_extra = candidate.get("extra")
            tags = _r7_ordered_strings(candidate.get("tags"), "tags", casefold=True)
            reasons = _r7_ordered_strings(candidate.get("exact_match_reasons"), "exact reasons")
            decks = candidate.get("decks")
            if (
                not isinstance(candidate_text, str)
                or not isinstance(candidate_extra, str)
                or not isinstance(decks, list)
            ):
                raise PinnedInputChanged("Pinned R6 card fields are malformed")
            if any(type(deck) is not str or not deck.strip() for deck in decks):
                raise PinnedInputChanged("Pinned R6 card fields are malformed")
            decks = cast(list[str], decks)
            projected = concept.model_copy(
                update={"facts": (fact,), "source_evidence_ids": fact.evidence_ids}
            )
            passages = tuple(
                SelectedPassage(
                    passage_id=evidence_id,
                    text=str(evidence[evidence_id]["normalized_text"]),
                    selection_reason="fact_scope_evidence",
                )
                for evidence_id in fact.evidence_ids
            )
            try:
                scores = _r7_scores(candidate)
            except (TypeError, ValueError) as exc:
                raise PinnedInputChanged("Pinned R6 retrieval scores are malformed") from exc
            seed = {
                "bundle_id": f"bundle:{concept_id}:{fact_id}:note:{representative_id}",
                "policy_sha256": policy_sha256,
                "scope_sha256": scope.scope_sha256,
                "concept": projected,
                "fact_id": fact_id,
                "candidate": CandidateCardFields(
                    candidate_id=f"note:{representative_id}",
                    note_id=representative_id,
                    text=candidate_text,
                    extra=candidate_extra,
                    tags=tags,
                    deck="\n".join(sorted(set(decks))),
                ),
                "retrieval_scores": scores,
                "exact_match_reasons": reasons,
                "selected_passages": passages,
                "duplicate_sibling_ids": tuple(
                    f"note:{note_id}" for note_id in sibling_ids if note_id != representative_id
                ),
                "allowed_concept_ids": (concept_id,),
                "allowed_fact_ids": (fact_id,),
                "allowed_passage_ids": fact.evidence_ids,
                "input_byte_estimate": 0,
                "input_token_estimate": 0,
                "max_input_bytes": MAX_BUNDLE_BYTES,
                "max_input_tokens": MAX_BUNDLE_TOKENS,
                "truncated": False,
                "degraded": scope.degraded_mode != "none"
                or representative_id in missing_vector_ids,
            }
            fact_bundles.append((float(candidate["calibrated_score"]), _r7_exact_bundle(seed)))
        if represented != set(by_note):
            raise PinnedInputChanged(
                "Pinned R6 representatives/siblings do not partition candidates"
            )
        built.extend(
            bundle
            for _score, bundle in sorted(
                fact_bundles,
                key=lambda item: (-item[0], item[1].candidate.note_id),
            )[:CLASSIFICATION_CANDIDATES_PER_FACT]
        )
    expected_facts = {fact.fact_id for concept in scope.concepts for fact in concept.facts}
    if observed_facts != expected_facts:
        raise PinnedInputChanged("Pinned R6 records do not partition R3 facts")
    return tuple(
        sorted(
            built, key=lambda item: (item.concept.concept_id, item.fact_id, item.candidate.note_id)
        )
    )


def _r7_scores(candidate: Mapping[str, Any]) -> tuple[RetrievalScore, ...]:
    values: list[RetrievalScore] = []
    for name in (
        "base_rrf",
        "boost_total",
        "calibrated_score",
        "semantic_score",
        "semantic_rank",
        "lexical_rank",
    ):
        value = candidate.get(name)
        if value is not None:
            values.append(RetrievalScore(identity=name, score=float(value)))
    return tuple(sorted(values, key=lambda item: item.identity))


def _r7_positive_ids(value: object) -> bool:
    return (
        isinstance(value, list)
        and all(type(item) is int and item > 0 for item in value)
        and value == sorted(value)
        and len(value) == len(set(value))
    )


def _r7_ordered_strings(value: object, label: str, *, casefold: bool = False) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or any(type(item) is not str or not item.strip() for item in value)
        or value != sorted(value, key=str.casefold if casefold else None)
        or len(value) != len(set(value))
    ):
        raise PinnedInputChanged(f"Pinned R6 {label} are malformed")
    return tuple(value)


def _r7_exact_bundle(seed: dict[str, Any]) -> CandidateEvidenceBundle:
    """Find the short decimal fixed point required by the existing bundle hash contract."""
    estimate = 0
    for _ in range(8):
        seed["input_byte_estimate"] = estimate
        seed["input_token_estimate"] = estimate
        provisional = CandidateEvidenceBundle.model_construct(**seed)
        actual = len(
            json.dumps(
                provisional.canonical_payload(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )
        if actual > MAX_BUNDLE_BYTES:
            raise PinnedInputChanged("R7 bundle exceeds its 16384-byte input bound")
        if actual == estimate:
            try:
                return CandidateEvidenceBundle.model_validate(seed)
            except (TypeError, ValueError) as exc:
                raise PinnedInputChanged("Pinned R7 bundle fields are malformed") from exc
        estimate = actual
    raise PinnedInputChanged("R7 bundle estimate did not stabilize")


def _r3_route(context: StageContext, payload: dict[str, Any]) -> ResolvedStageModel:
    route = context.job.resolved_model_config.scope_r3
    if route is None:
        raise PinnedInputChanged("Pinned R3 scope route is unavailable")
    expected = {
        "provider": route.provider,
        "model": route.model,
        "thinking_mode": route.thinking_mode,
        "fixture_validation_signature": route.fixture_validation_signature,
    }
    if payload.get("scope_r3") != expected:
        raise PinnedInputChanged("Pinned R3 scope route changed")
    return route


def _r3_prompt(payload: dict[str, Any]) -> PinnedScopePrompt:
    snapshot = payload.get("prompt_snapshot")
    if not isinstance(snapshot, list):
        raise PinnedInputChanged("Pinned R3 prompt snapshot is malformed")
    entries = [
        entry
        for entry in snapshot
        if isinstance(entry, dict) and entry.get("id") == "card-centric-scope-v3"
    ]
    if len(entries) != 1:
        raise PinnedInputChanged("Pinned R3 scope prompt is unavailable or duplicated")
    entry = entries[0]
    try:
        metadata = PromptMetadata.model_validate(entry.get("metadata"))
        return PinnedScopePrompt(
            id=str(entry["id"]),
            version=str(entry["version"]),
            content=str(entry["content"]),
            content_sha256=str(entry["content_sha256"]),
            metadata=metadata,
        )
    except (KeyError, ScopeInputError, TypeError, ValueError) as exc:
        raise PinnedInputChanged("Pinned R3 prompt snapshot is malformed") from exc


def _r3_passages(payload: dict[str, Any]) -> tuple[SourcePassage, ...]:
    raw = payload.get("passages")
    if not isinstance(raw, list):
        raise PinnedInputChanged("Pinned R3 source passages are malformed")
    try:
        passages = tuple(_r3_passage(item) for item in raw)
    except (TypeError, ValueError) as exc:
        raise PinnedInputChanged("Pinned R3 source passages are malformed") from exc
    if len({passage.passage_id for passage in passages}) != len(passages):
        raise PinnedInputChanged("Pinned R3 source passages have duplicate IDs")
    return passages


def _r3_passage(raw: object) -> SourcePassage:
    if not isinstance(raw, Mapping):
        raise TypeError("source passage is not an object")
    source_kind = SourceKind(str(raw["source_kind"]))
    summary_section = raw.get("summary_section")
    passage = SourcePassage.create(
        revision_id=int(raw["revision_id"]),
        lecture_id=int(raw["lecture_id"]),
        artifact_id=str(raw["artifact_id"]),
        source_kind=source_kind,
        locator=str(raw["locator"]),
        text=str(raw["text"]),
        extraction_status=cast(Any, raw["extraction_status"]),
        slide_number=(int(raw["slide_number"]) if raw.get("slide_number") is not None else None),
        start_seconds=(
            float(raw["start_seconds"]) if raw.get("start_seconds") is not None else None
        ),
        end_seconds=(float(raw["end_seconds"]) if raw.get("end_seconds") is not None else None),
        source_id=str(raw["source_id"]),
        summary_backrefs=tuple(str(item) for item in raw.get("summary_backrefs", ())),
        summary_section=cast(Any, summary_section),
    )
    expected = {
        "passage_id": passage.passage_id,
        "source_id": passage.source_id,
        "revision_id": passage.revision_id,
        "lecture_id": passage.lecture_id,
        "artifact_id": passage.artifact_id,
        "source_kind": passage.source_kind.value,
        "locator": passage.locator,
        "text": passage.text,
        "content_hash": passage.content_hash,
        "extraction_status": passage.extraction_status,
        "slide_number": passage.slide_number,
        "start_seconds": passage.start_seconds,
        "end_seconds": passage.end_seconds,
        "summary_backrefs": list(passage.summary_backrefs),
        "summary_section": passage.summary_section,
    }
    normalized_raw = dict(raw)
    if "summary_backrefs" in normalized_raw:
        normalized_raw["summary_backrefs"] = list(normalized_raw["summary_backrefs"])
    if isinstance(normalized_raw.get("source_kind"), SourceKind):
        normalized_raw["source_kind"] = normalized_raw["source_kind"].value
    if set(normalized_raw) != set(expected) or normalized_raw != expected:
        raise ValueError("source passage identity changed")
    return passage


def _r3_emphasis(payload: dict[str, Any]) -> tuple[SourceEmphasisEvidence, ...]:
    raw = payload.get("emphasis_evidence")
    if not isinstance(raw, list):
        raise PinnedInputChanged("Pinned R3 emphasis evidence is malformed")
    try:
        evidence = tuple(SourceEmphasisEvidence.model_validate(item) for item in raw)
    except (TypeError, ValueError) as exc:
        raise PinnedInputChanged("Pinned R3 emphasis evidence is malformed") from exc
    if len({item.evidence_id for item in evidence}) != len(evidence):
        raise PinnedInputChanged("Pinned R3 emphasis evidence has duplicate IDs")
    return evidence


def _r3_fidelity(
    policy: CourseCurationPolicy,
    r1: dict[str, Any],
    r2: dict[str, Any],
    emphasis: Sequence[SourceEmphasisEvidence],
) -> R2FidelityDiagnostic:
    raw = r2.get("fidelity_diagnostic")
    try:
        fidelity = R2FidelityDiagnostic.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise PinnedInputChanged("Pinned R3 fidelity diagnostic is malformed") from exc
    source_sha256 = r1.get("style_source_sha256")
    sidecar_sha256 = r1.get("style_sidecar_sha256")
    if (
        not _r3_sha256(source_sha256)
        or not _r3_sha256(sidecar_sha256)
        or fidelity.policy_sha256 != policy.policy_sha256
        or fidelity.source_sha256 != source_sha256
        or fidelity.sidecar_sha256 != sidecar_sha256
        or any(
            item.policy_sha256 != policy.policy_sha256
            or item.source_sha256 != source_sha256
            or item.sidecar_sha256 != sidecar_sha256
            for item in emphasis
        )
    ):
        raise PinnedInputChanged("Pinned R3 style identity changed")
    return fidelity


def _r3_reuse(
    r0: dict[str, Any],
    replay_inputs: Mapping[str, Any],
) -> ScopeReuseArtifact | None:
    values = [
        value
        for value in (
            r0.get("scope_r3_reuse"),
            replay_inputs.get("scope_r3_reuse"),
        )
        if value is not None
    ]
    if not values:
        return None
    if len(values) != 1 or not isinstance(values[0], Mapping):
        raise PinnedInputChanged("Pinned R3 scope reuse is malformed")
    try:
        return ScopeReuseArtifact(
            scope=LectureScope.model_validate(values[0]["scope"]),
            scope_request_sha256=str(values[0]["scope_request_sha256"]),
        )
    except (KeyError, ScopeInputError, TypeError, ValueError) as exc:
        raise PinnedInputChanged("Pinned R3 scope reuse is malformed") from exc


def _r3_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _v3_r11_persisted_rows(
    *,
    scope: LectureScope,
    r4: Mapping[str, Any],
    existing: Sequence[Mapping[str, Any]],
    generated: Sequence[Mapping[str, Any]],
) -> tuple[tuple[Candidate, ...], tuple[GapCard, ...]]:
    """Project only R11 identities that can be atomically materialized."""
    companion_generation = r4.get("companion_generation")
    verification_sha256 = r4.get("verification_sha256")
    identities = r4.get("card_identities")
    if (
        not isinstance(companion_generation, str)
        or not companion_generation
        or not isinstance(verification_sha256, str)
        or len(verification_sha256) != 64
        or not isinstance(identities, list)
    ):
        raise PinnedInputChanged("R11 lacks the validated R4 companion identity closure")
    identity_by_note: dict[int, str] = {}
    for identity in identities:
        if (
            not isinstance(identity, Mapping)
            or not isinstance(identity.get("note_id"), int)
            or not isinstance(identity.get("content_sha256"), str)
            or len(cast(str, identity["content_sha256"])) != 64
            or identity["note_id"] in identity_by_note
        ):
            raise PinnedInputChanged("R11 has a malformed R4 card identity")
        identity_by_note[identity["note_id"]] = identity["content_sha256"]
    facts = {
        fact.fact_id: (concept.concept_id, fact)
        for concept in scope.concepts
        for fact in concept.facts
    }
    source_by_id = {
        evidence.evidence_id: SourceReference(
            source_kind=SourceKind(cast(str, evidence.source_kind)),
            revision_id=cast(int, evidence.revision_id),
            locator=evidence.locator,
            content_hash=evidence.content_sha256,
        )
        for evidence in scope.evidence
    }
    rows_by_note: dict[int, list[dict[str, object]]] = {}
    for row in existing:
        note_id, fact_id = (
            row.get("note_id"),
            row.get("fact_id"),
        )
        if (
            not isinstance(note_id, int)
            or not isinstance(fact_id, str)
            or fact_id not in facts
            or note_id not in identity_by_note
        ):
            raise PinnedInputChanged("R11 selection lacks an exact R4 candidate identity")
        disposition = row.get("disposition")
        selected = row.get("selected")
        if not isinstance(disposition, str) or not isinstance(selected, bool):
            raise PinnedInputChanged("R11 selection is malformed")
        rows_by_note.setdefault(note_id, []).append(
            {
                "fact_id": fact_id,
                "concept_id": facts[fact_id][0],
                "disposition": disposition,
                "selected": selected,
            }
        )
    candidates = tuple(
        Candidate(
            note_id=note_id,
            content_hash=identity_by_note[note_id],
            best_concept_id=cast(str, rows[0]["concept_id"]),
            provenance={
                "card_centric_v3": {
                    "r4_verification_sha256": verification_sha256,
                    "companion_generation": companion_generation,
                    "note_id": note_id,
                    "content_sha256": identity_by_note[note_id],
                    "fact_rows": sorted(
                        rows,
                        key=lambda item: (
                            cast(str, item["fact_id"]),
                            cast(str, item["disposition"]),
                        ),
                    ),
                }
            },
            scores={},
            predicted_band="r11_review",
            verdict=(
                "keep"
                if any(cast(bool, row["selected"]) for row in rows)
                else cast(str, rows[0]["disposition"])
            ),
            confidence=1.0,
            reason="validated R11 frozen review projection",
            context_trap=False,
            recall_direction="",
            mnemonic_classification="",
            dedupe_disposition=(
                "keep"
                if any(cast(bool, row["selected"]) for row in rows)
                else cast(str, rows[0]["disposition"])
            ),
            selected=any(cast(bool, row["selected"]) for row in rows),
            retrieval_pass=RetrievalPass.PASS_1,
        )
        for note_id, rows in sorted(rows_by_note.items())
    )
    cards: list[GapCard] = []
    seen_card_ids: set[str] = set()
    for row in generated:
        status = row.get("status")
        card_id, fact_id, text, extra, evidence_ids = (
            row.get("card_id"),
            row.get("fact_id"),
            row.get("text"),
            row.get("extra"),
            row.get("evidence_ids"),
        )
        if (
            not isinstance(status, str)
            or status
            not in {"generated", "duplicate_of_existing", "duplicate_of_generated", "unresolved"}
            or not isinstance(card_id, str)
            or not card_id.strip()
            or card_id in seen_card_ids
            or not isinstance(fact_id, str)
            or fact_id not in facts
            or not isinstance(text, str)
            or not text.strip()
            or not isinstance(extra, str)
            or not isinstance(evidence_ids, list)
            or not evidence_ids
            or any(not isinstance(item, str) or item not in source_by_id for item in evidence_ids)
        ):
            raise PinnedInputChanged("R11 generated row lacks an exact persisted identity")
        seen_card_ids.add(card_id)
        cards.append(
            GapCard(
                concept_id=facts[fact_id][0],
                text=text,
                extra=extra,
                selected=status == "generated",
                validation_state=status,
                source_refs=tuple(source_by_id[item] for item in evidence_ids),
                evidence_ids=tuple(evidence_ids),
                provenance={
                    "card_centric_v3": {
                        "fact_id": fact_id,
                        "status": status,
                        "policy_scope_sha256": scope.scope_sha256,
                    }
                },
                card_id=card_id,
            )
        )
    return candidates, tuple(cards)


def _v3_r4_verification(
    context: StageContext,
    payload: dict[str, Any],
    r0: dict[str, Any],
) -> dict[str, Any]:
    """Validate the frozen R4 closure before any R5 query embedding."""
    required = {
        "kind",
        "policy_sha256",
        "companion_generation",
        "lexical_generation",
        "semantic_generation",
        "deck_allowlist",
        "tag_allowlist",
        "card_identities",
        "cards_sha256",
        "semantic_identities",
        "semantic_manifest",
        "semantic_manifest_sha256",
        "census",
        "census_sha256",
        "verification_sha256",
    }
    if (
        set(payload) - {"artifact_sha256"} != required
        or payload.get("kind") != "card_centric_v3_index_verification"
    ):
        raise PinnedInputChanged("Pinned R4 verification is malformed")
    if (
        payload["policy_sha256"] != context.job.policy_sha256
        or payload["policy_sha256"] != r0.get("policy_sha256")
        or payload["companion_generation"] != context.job.companion_generation
        or payload["lexical_generation"] != payload["companion_generation"]
        or payload["semantic_generation"] != context.job.semantic_generation
        or tuple(payload["deck_allowlist"]) != context.job.deck_allowlist
        or tuple(payload["tag_allowlist"]) != context.job.tag_allowlist
    ):
        raise PinnedInputChanged("Pinned R4 generation or policy changed")
    cards = payload["card_identities"]
    semantic = payload["semantic_identities"]
    if (
        not isinstance(cards, list)
        or not isinstance(semantic, list)
        or len({int(row["note_id"]) for row in cards}) != len(cards)
        or len({int(row["note_id"]) for row in semantic}) != len(semantic)
        or cards != sorted(cards, key=lambda row: int(row["note_id"]))
        or semantic != sorted(semantic, key=lambda row: int(row["note_id"]))
        or canonical_sha256(cards) != payload["cards_sha256"]
    ):
        raise PinnedInputChanged("Pinned R4 identities are malformed")
    for item in cards:
        if not isinstance(item, dict) or set(item) != {"note_id", "content_sha256"}:
            raise PinnedInputChanged("Pinned R4 card identity is malformed")
    census = SnapshotCensus.model_validate(payload["census"])
    if canonical_sha256(payload["census"]) != payload["census_sha256"] or set(census.mapping) != {
        int(item["note_id"]) for item in cards
    }:
        raise PinnedInputChanged("Pinned R4 census closure changed")
    manifest = payload["semantic_manifest"]
    if not isinstance(manifest, dict) or set(manifest) != {
        "generation",
        "model",
        "dimensions",
        "matrix_sha256",
    }:
        raise PinnedInputChanged("Pinned R4 semantic manifest is malformed")
    if (
        manifest["generation"] != payload["semantic_generation"]
        or canonical_sha256(manifest) != payload["semantic_manifest_sha256"]
    ):
        raise PinnedInputChanged("Pinned R4 semantic manifest changed")
    verification = {
        key: value
        for key, value in payload.items()
        if key not in {"verification_sha256", "artifact_sha256"}
    }
    if canonical_sha256(verification) != payload["verification_sha256"]:
        raise PinnedInputChanged("Pinned R4 verification hash changed")
    return payload


def _resolved_prompt(
    context: StageContext,
    prompt_id: str,
) -> tuple[str, str]:
    raw_snapshot = _payload(context, CurationStage.PREFLIGHT).get(
        "prompt_snapshot",
        [],
    )
    if not isinstance(raw_snapshot, list):
        raise PinnedInputChanged("Pinned prompt snapshot is malformed")
    for value in raw_snapshot:
        if not isinstance(value, dict) or value.get("id") != prompt_id:
            continue
        content = value.get("content")
        prompt_hash = value.get("prompt_hash")
        if (
            not isinstance(content, str)
            or not content.strip()
            or not isinstance(prompt_hash, str)
            or len(prompt_hash) != 12
        ):
            raise PinnedInputChanged("Pinned prompt snapshot is malformed")
        return content, prompt_hash
    raise PinnedInputChanged(f"Pinned prompt {prompt_id} is unavailable; start a new curation job")


def _resolved_prompt_schema(
    context: StageContext,
    prompt_id: str,
) -> str:
    raw_snapshot = _payload(context, CurationStage.PREFLIGHT).get(
        "prompt_snapshot",
        [],
    )
    if not isinstance(raw_snapshot, list):
        raise PinnedInputChanged("Pinned prompt snapshot is malformed")
    for value in raw_snapshot:
        if not isinstance(value, dict) or value.get("id") != prompt_id:
            continue
        metadata = value.get("metadata")
        if not isinstance(metadata, dict):
            raise PinnedInputChanged("Pinned prompt metadata is malformed")
        schema_name = metadata.get("schema")
        if not isinstance(schema_name, str) or not schema_name.strip():
            raise PinnedInputChanged("Pinned prompt schema is missing")
        return schema_name.strip()
    raise PinnedInputChanged(f"Pinned prompt {prompt_id} is unavailable; start a new curation job")


def _resolved_prompt_metadata(
    context: StageContext,
    prompt_id: str,
) -> dict[str, Any]:
    raw_snapshot = _payload(context, CurationStage.PREFLIGHT).get(
        "prompt_snapshot",
        [],
    )
    if not isinstance(raw_snapshot, list):
        raise PinnedInputChanged("Pinned prompt snapshot is malformed")
    for value in raw_snapshot:
        if not isinstance(value, dict) or value.get("id") != prompt_id:
            continue
        metadata = value.get("metadata")
        if not isinstance(metadata, dict):
            raise PinnedInputChanged("Pinned prompt metadata is malformed")
        return dict(metadata)
    raise PinnedInputChanged(f"Pinned prompt {prompt_id} is unavailable; start a new curation job")


def revision_fingerprint(revision: StudyRevision) -> str:
    payload = {
        "revision_id": revision.id,
        "lecture_id": revision.lecture_id,
        "kind": revision.kind.value,
        "source_sha256": revision.source_sha256,
        "derived_sha256": revision.derived_sha256,
        "prompt_sha256": revision.prompt_sha256,
    }
    if revision.provenance_kind == "imported_cleaned" or revision.import_id is not None:
        payload["provenance_kind"] = revision.provenance_kind
        payload["import_id"] = revision.import_id
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _payload(
    context: StageContext,
    stage: CurationStage,
) -> dict[str, Any]:
    try:
        return context.prior_payloads[stage]
    except KeyError:
        raise PinnedInputChanged(f"Committed {stage.value} artifact is unavailable") from None


def _v3_cost_session(context: StageContext, r0: Mapping[str, object]) -> StageCostSession:
    """Build the fail-closed, exact-prefix ledger before any v3 provider seam."""
    try:
        return StageCostSession.from_prior(context, r0)
    except CostAuthorizationError as exc:
        raise PinnedInputChanged(str(exc)) from exc


def _seal_v3_costs(costs: StageCostSession, payload: dict[str, object]) -> None:
    costs.seal(payload)


def _cost_token_estimate(value: str) -> int:
    """Frozen conservative UTF-8 estimate; replace only with a pinned tokenizer version."""
    return max(1, (len(value.encode("utf-8")) + 3) // 4)


class _GuardedStructuredService:
    """Reserve before the existing structured service starts its attempt lifecycle."""

    def __init__(self, service: StructuredTextService, costs: StageCostSession, stage: str) -> None:
        self._service = service
        self._costs = costs
        self._stage = stage

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)

    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: Any,
        provider: ProviderName,
        model: str,
        options: GenerationOptions,
    ) -> Any:
        output_cap = options.max_tokens
        if type(output_cap) is not int or output_cap < 1:
            raise CostAuthorizationError("structured dispatch requires an explicit output cap")
        request = {
            "instruction": instruction,
            "input": input_text,
            "provider": provider.value,
            "model": model,
            "output_schema": output_model.model_json_schema(),
            "options": {
                "thinking": options.thinking.value,
                "thinking_budget_tokens": options.thinking_budget_tokens,
                "temperature": options.temperature,
                "max_tokens": output_cap,
            },
        }
        request_sha256 = canonical_payload_sha256(request)
        prefix = options.cacheable_source_prefix or ""
        input_tokens = _cost_token_estimate(instruction + input_text)
        prefix_tokens = _cost_token_estimate(prefix) if prefix else 0
        predicted_output = max(1, output_cap // 2)
        entry = self._costs.reserve(
            stage=self._stage,
            modality="structured",
            model=model,
            request_sha256=request_sha256,
            predicted_usage=TokenUsage(
                input_tokens=input_tokens,
                cache_creation_tokens=prefix_tokens,
                output_tokens=predicted_output,
            ),
            reserved_usage=TokenUsage(
                input_tokens=input_tokens * RESERVED_INPUT_SAFETY_MULTIPLIER,
                cache_creation_tokens=prefix_tokens * RESERVED_INPUT_SAFETY_MULTIPLIER,
                output_tokens=output_cap,
            ),
        )
        try:
            with provider_cost_reservation(
                replace(entry, observed=None, observed_estimated=False).document()
            ):
                result = self._service.generate_json(
                    instruction,
                    input_text,
                    output_model=output_model,
                    provider=provider,
                    model=model,
                    options=options,
                )
        except StructuredOutputError as exc:
            self._observe_structured(entry.call_id, exc.generation)
            raise
        self._observe_structured(entry.call_id, result)
        return result

    def _observe_structured(self, call_id: str, result: Any) -> None:
        cache_creation = int(result.cache_creation_input_tokens)
        cache_read = int(result.cache_read_input_tokens)
        self._costs.observe(
            call_id,
            TokenUsage(
                input_tokens=max(0, int(result.input_tokens) - cache_creation - cache_read),
                cache_creation_tokens=cache_creation,
                cache_read_tokens=cache_read,
                output_tokens=int(result.output_tokens),
            ),
        )


class _GuardedEmbeddingClient:
    """The cache-miss/embed seam; embedding clients do not expose provider usage."""

    def __init__(
        self,
        client: EmbeddingClient,
        costs: StageCostSession,
        stage: str,
        fallback_model: str,
    ) -> None:
        self._client = client
        self._costs = costs
        self._stage = stage
        model = getattr(client, "model", fallback_model)
        self._model = model if isinstance(model, str) and model else fallback_model

    async def embed(self, texts: Sequence[str], *, input_type: Literal["document", "query"]) -> Any:
        request = {"input_type": input_type, "texts": list(texts), "model": self._model}
        usage = TokenUsage(embedding_tokens=sum(_cost_token_estimate(text) for text in texts))
        attempts = getattr(self._client, "max_attempts", 1)
        batch_size = getattr(self._client, "batch_size", len(texts) or 1)
        split_on_limit = bool(getattr(self._client, "split_on_limit", False))
        if (
            type(attempts) is not int
            or attempts < 1
            or type(batch_size) is not int
            or batch_size < 1
        ):
            raise CostAuthorizationError("embedding delegate dispatch bound is unavailable")
        batches = [
            len(texts[index : index + batch_size]) for index in range(0, len(texts), batch_size)
        ]
        dispatches = attempts * sum((2 * size - 1) if split_on_limit else 1 for size in batches)
        entry = self._costs.reserve(
            stage=self._stage,
            modality="embedding",
            model=self._model,
            request_sha256=canonical_payload_sha256(request),
            predicted_usage=usage,
            reserved_usage=TokenUsage(embedding_tokens=usage.embedding_tokens * dispatches),
        )
        with provider_cost_reservation(
            replace(entry, observed=None, observed_estimated=False).document()
        ):
            result = await self._client.embed(texts, input_type=input_type)
        self._costs.observe(entry.call_id, usage, estimated=True)
        return result


class _CostedSemanticService:
    """Expose the existing query-cache override without changing retrieval contracts."""

    def __init__(self, service: SemanticIndexService, embedder: EmbeddingClient) -> None:
        self._service = service
        self._embedder = embedder

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)

    async def search(self, *args: Any, **kwargs: Any) -> Any:
        kwargs["embedding_client"] = self._embedder
        return await self._service.search(*args, **kwargs)


def _source_passages(context: StageContext) -> list[SourcePassage]:
    payload = _payload(context, CurationStage.SOURCE_INDEX)
    values = payload.get("passages")
    if not isinstance(values, list):
        raise PinnedInputChanged("Source-index artifact is malformed")
    return [_passage_from_payload(value) for value in values]


def _ledger(context: StageContext) -> LectureConceptLedger:
    payload = _payload(context, CurationStage.LCL)
    schema_name = payload.get("schema_name", "lcl_v1")
    if schema_name == "lcl_v2":
        ledger = LectureConceptLedgerV2.model_validate(payload.get("ledger"))
        return runtime_ledger_from_v2(
            ledger,
            _source_passages(context),
        )
    if schema_name != "lcl_v1":
        raise PinnedInputChanged("Committed LCL schema is unsupported")
    return LectureConceptLedger.model_validate(payload.get("ledger"))


def _judgment_payload(
    context: StageContext,
    stage: CurationStage,
) -> dict[str, dict[str, Any]]:
    return cast(
        dict[str, dict[str, Any]],
        _payload(context, stage).get("judgments", {}),
    )


def _coverage_judgment(
    context: StageContext,
    stage: CurationStage,
    concept_id: str,
) -> CoverageJudgment:
    payload = _payload(context, stage)
    schema_name = payload.get("schema_name", "coverage_v1")
    raw = _judgment_payload(context, stage).get(concept_id)
    if not isinstance(raw, dict) or not isinstance(raw.get("judgment"), dict):
        raise PinnedInputChanged("Coverage judgment artifact is malformed")
    if schema_name == "coverage_v2":
        return runtime_judgment_from_v2(CoverageJudgmentV2.model_validate(raw["judgment"]))
    if schema_name != "coverage_v1":
        raise PinnedInputChanged("Committed coverage schema is unsupported")
    return CoverageJudgment.model_validate(raw["judgment"])


def _judgment_record(
    context: StageContext,
    stage: CurationStage,
    concept_id: str,
) -> dict[str, Any]:
    raw = _judgment_payload(context, stage).get(concept_id)
    if not isinstance(raw, dict) or not isinstance(raw.get("judgment"), dict):
        raise PinnedInputChanged("Coverage judgment artifact is malformed")
    return dict(raw)


def _final_judgment_stage(
    context: StageContext,
    concept_id: str,
) -> CurationStage:
    for stage in reversed(_COVERAGE_JUDGMENT_STAGES):
        payload = context.prior_payloads.get(stage)
        if not isinstance(payload, dict):
            continue
        judgments = payload.get("judgments")
        if isinstance(judgments, dict) and concept_id in judgments:
            return stage
    raise PinnedInputChanged("Coverage judgment artifact is absent for a lecture concept")


def _combined_support_ids(
    context: StageContext,
    concept_id: str,
) -> set[int]:
    supports: set[int] = set()
    for stage in _COVERAGE_JUDGMENT_STAGES:
        payload = context.prior_payloads.get(stage)
        if not isinstance(payload, dict):
            continue
        judgments = payload.get("judgments")
        if not isinstance(judgments, dict) or concept_id not in judgments:
            continue
        supports.update(
            _coverage_judgment(
                context,
                stage,
                concept_id,
            ).supporting_note_ids
        )
    return supports


_COVERAGE_JUDGMENT_STAGES = (
    CurationStage.JUDGMENT_PASS_1,
    CurationStage.JUDGMENT_PASS_2,
    CurationStage.CONVERGENCE_PASS_3,
    CurationStage.CONVERGENCE_PASS_4,
    CurationStage.CONVERGENCE_PASS_5,
)


def _prior_convergence(
    context: StageContext,
    ledger: LectureConceptLedger,
    *,
    pass_number: int,
) -> tuple[dict[str, ConvergenceState], dict[str, list[str]]]:
    if pass_number == 3:
        states: dict[str, ConvergenceState] = {}
        first_groups = _retrieval_groups(
            context,
            CurationStage.RETRIEVAL_PASS_1,
        )
        second_groups = _retrieval_groups(
            context,
            CurationStage.RETRIEVAL_PASS_2,
        )
        for concept in ledger.concepts:
            first_ids = _group_note_ids(first_groups.get(concept.concept_id, []))
            judgment = _coverage_judgment(
                context,
                _final_judgment_stage(context, concept.concept_id),
                concept.concept_id,
            )
            if concept.concept_id not in second_groups:
                states[concept.concept_id] = ConvergenceState(
                    concept_id=concept.concept_id,
                    passes_run=1,
                    seen_note_ids=first_ids,
                    growth=(1.0 if first_ids else 0.0,),
                    converged=not judgment.missing_facts,
                )
                continue
            second_update = update_growth(
                seen_note_ids=first_ids,
                retrieved_note_ids=_group_note_ids(second_groups.get(concept.concept_id, [])),
            )
            states[concept.concept_id] = ConvergenceState(
                concept_id=concept.concept_id,
                passes_run=2,
                seen_note_ids=second_update.seen_note_ids,
                growth=(
                    1.0 if first_ids else 0.0,
                    second_update.growth,
                ),
                converged=(not judgment.missing_facts or second_update.converged),
            )
        return states, {concept.concept_id: [] for concept in ledger.concepts}
    previous_stage = {
        4: CurationStage.CONVERGENCE_PASS_3,
        5: CurationStage.CONVERGENCE_PASS_4,
    }[pass_number]
    payload = _payload(context, previous_stage)
    raw_states = payload.get("concepts")
    if not isinstance(raw_states, list):
        raise PinnedInputChanged("Convergence artifact is malformed")
    states = {
        state.concept_id: state
        for value in raw_states
        for state in (ConvergenceState.model_validate(value),)
    }
    expected = {concept.concept_id for concept in ledger.concepts}
    if set(states) != expected or len(states) != len(raw_states):
        raise PinnedInputChanged("Convergence artifact does not partition lecture concepts")
    raw_expanded = payload.get("expanded_paraphrases", {})
    if not isinstance(raw_expanded, dict):
        raise PinnedInputChanged("Convergence paraphrases are malformed")
    expanded: dict[str, list[str]] = {}
    for concept_id in expected:
        values = raw_expanded.get(concept_id, [])
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise PinnedInputChanged("Convergence paraphrases are malformed")
        expanded[concept_id] = list(values)
    return states, expanded


def _retrieval_groups(
    context: StageContext,
    stage: CurationStage,
) -> dict[str, list[dict[str, Any]]]:
    groups = _payload(context, stage).get("groups", {})
    if not isinstance(groups, dict) or any(
        not isinstance(values, list) for values in groups.values()
    ):
        raise PinnedInputChanged("Retrieval artifact is malformed")
    return cast(dict[str, list[dict[str, Any]]], groups)


def _group_note_ids(values: Sequence[dict[str, Any]]) -> tuple[int, ...]:
    return tuple(sorted({_candidate_from_payload(value).note_id for value in values}))


def _retrieval_scope(context: StageContext) -> RetrievalScope:
    return RetrievalScope(
        filters=CompanionFilters(
            deck_allowlist=context.job.deck_allowlist,
            tag_allowlist=context.job.tag_allowlist,
            excluded_tag_prefixes=("suspended",),
        ),
        lecture_tag_prefix=context.job.target_tag,
        block_tag_prefix=context.job.block_id,
    )


def _passage_payload(passage: SourcePassage) -> dict[str, Any]:
    return {
        "passage_id": passage.passage_id,
        "source_id": passage.source_id,
        "revision_id": passage.revision_id,
        "lecture_id": passage.lecture_id,
        "artifact_id": passage.artifact_id,
        "source_kind": passage.source_kind.value,
        "locator": passage.locator,
        "text": passage.text,
        "content_hash": passage.content_hash,
        "extraction_status": passage.extraction_status,
        "slide_number": passage.slide_number,
        "start_seconds": passage.start_seconds,
        "end_seconds": passage.end_seconds,
        "summary_backrefs": list(passage.summary_backrefs),
        "summary_section": passage.summary_section,
    }


def _passage_from_payload(value: object) -> SourcePassage:
    if not isinstance(value, dict):
        raise PinnedInputChanged("Source passage artifact is malformed")
    try:
        return SourcePassage(
            passage_id=str(value["passage_id"]),
            source_id=str(value.get("source_id", value["passage_id"])),
            revision_id=int(value["revision_id"]),
            lecture_id=int(value["lecture_id"]),
            artifact_id=str(value["artifact_id"]),
            source_kind=SourceKind(str(value["source_kind"])),
            locator=str(value["locator"]),
            text=str(value["text"]),
            content_hash=str(value["content_hash"]),
            extraction_status=cast(
                Any,
                str(value["extraction_status"]),
            ),
            slide_number=(
                None if value.get("slide_number") is None else int(value["slide_number"])
            ),
            start_seconds=(
                None if value.get("start_seconds") is None else float(value["start_seconds"])
            ),
            end_seconds=(None if value.get("end_seconds") is None else float(value["end_seconds"])),
            summary_backrefs=tuple(str(item) for item in value.get("summary_backrefs", [])),
            summary_section=cast(Any, value.get("summary_section")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PinnedInputChanged("Source passage artifact is malformed") from exc


def _candidate_payload(candidate: Candidate) -> dict[str, Any]:
    return {
        "note_id": candidate.note_id,
        "content_hash": candidate.content_hash,
        "best_concept_id": candidate.best_concept_id,
        "provenance": candidate.provenance,
        "scores": candidate.scores,
        "predicted_band": candidate.predicted_band,
        "verdict": candidate.verdict,
        "confidence": candidate.confidence,
        "reason": candidate.reason,
        "context_trap": candidate.context_trap,
        "recall_direction": candidate.recall_direction,
        "mnemonic_classification": candidate.mnemonic_classification,
        "dedupe_disposition": candidate.dedupe_disposition,
        "selected": candidate.selected,
        "retrieval_pass": candidate.retrieval_pass.value,
    }


def _candidate_from_payload(value: Mapping[str, Any]) -> Candidate:
    return Candidate(
        note_id=int(value["note_id"]),
        content_hash=str(value["content_hash"]),
        best_concept_id=str(value["best_concept_id"]),
        provenance=dict(value["provenance"]),
        scores={str(key): float(score) for key, score in dict(value["scores"]).items()},
        predicted_band=str(value["predicted_band"]),
        verdict=str(value["verdict"]),
        confidence=float(value["confidence"]),
        reason=str(value["reason"]),
        context_trap=bool(value["context_trap"]),
        recall_direction=str(value["recall_direction"]),
        mnemonic_classification=str(value["mnemonic_classification"]),
        dedupe_disposition=str(value["dedupe_disposition"]),
        selected=bool(value["selected"]),
        retrieval_pass=RetrievalPass(str(value["retrieval_pass"])),
    )


def _judged_candidate(
    candidate: Candidate,
    judgment: CoverageJudgment,
    *,
    selected: bool,
) -> Candidate:
    return replace(
        candidate,
        predicted_band=judgment.status,
        verdict=(
            "include" if selected else "uncertain" if judgment.status == "partial" else "drop"
        ),
        confidence=(
            1.0 if judgment.status == "covered" else 0.7 if judgment.status == "partial" else 0.0
        ),
        reason=judgment.rationale,
        selected=selected,
    )


def _audited_candidate(
    candidate: Candidate,
    audit: AuditVerdictV2,
) -> Candidate:
    provenance = dict(candidate.provenance)
    provenance["audit"] = audit.model_dump(mode="json")
    return replace(
        candidate,
        provenance=provenance,
        verdict={
            "keep": "include",
            "drop": "drop",
            "uncertain": "uncertain",
        }[audit.verdict],
        confidence={"keep": 1.0, "drop": 0.0, "uncertain": 0.5}[audit.verdict],
        reason=audit.reason,
        context_trap="context_trap" in audit.structure_issue,
        selected=audit.verdict == "keep",
    )


def _merge_candidates(
    candidates: Sequence[Candidate],
) -> tuple[Candidate, ...]:
    grouped: dict[int, list[Candidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.note_id, []).append(candidate)
    merged: list[Candidate] = []
    for matches in grouped.values():
        ordered = sorted(
            matches,
            key=lambda candidate: (
                not candidate.selected,
                -candidate.scores.get("boosted_score", 0.0),
                candidate.best_concept_id,
            ),
        )
        chosen = ordered[0]
        provenance = dict(chosen.provenance)
        provenance["concept_matches"] = [
            {
                "concept_id": candidate.best_concept_id,
                "retrieval_pass": candidate.retrieval_pass.value,
                "selected": candidate.selected,
                "score": candidate.scores.get("boosted_score", 0.0),
            }
            for candidate in ordered
        ]
        merged.append(replace(chosen, provenance=provenance))
    return tuple(sorted(merged, key=lambda candidate: candidate.note_id))


def _projected_candidates(
    payload: Mapping[str, Any],
) -> tuple[Candidate, ...]:
    values = payload.get("projected_candidates", [])
    if not isinstance(values, list):
        raise PinnedInputChanged("Judgment artifact is malformed")
    return tuple(_candidate_from_payload(value) for value in values if isinstance(value, dict))


def _localization(
    concept: LectureConcept,
    value: Mapping[str, Any],
) -> RescueLocalization:
    raw_evidence = value.get("evidence", [])
    if not isinstance(raw_evidence, list):
        raise PinnedInputChanged("Rescue artifact is malformed")
    return RescueLocalization(
        concept=concept,
        support=cast(RescueSupport, str(value["support"])),
        evidence=tuple(_passage_from_payload(passage) for passage in raw_evidence),
        rationale=str(value["rationale"]),
    )


def _localization_from_concept(
    concept: LectureConcept,
    passages: Sequence[SourcePassage],
) -> RescueLocalization:
    by_id = {passage.passage_id: passage for passage in passages}
    try:
        evidence = tuple(
            passage
            for reference in concept.source_refs
            if (passage := by_id[reference.passage_id]).source_kind is not SourceKind.SUMMARY
        )
    except KeyError as exc:
        raise PinnedInputChanged("Concept evidence is absent from the source artifact") from exc
    if not evidence:
        raise PinnedInputChanged("Concept has no primary source evidence")
    return RescueLocalization(
        concept=concept,
        support="supported",
        evidence=evidence,
        rationale="The audited coverage gap is grounded in the LCL evidence.",
    )


def _evidence_records(
    localization: RescueLocalization,
) -> list[SourceEvidence]:
    support = {
        "supported": EvidenceSupport.SUPPORTED,
        "partial": EvidenceSupport.PARTIAL,
        "unsupported": EvidenceSupport.UNSUPPORTED,
    }[localization.support]
    records = []
    for passage in localization.evidence:
        identity = source_evidence_id(
            localization.concept.concept_id,
            passage.passage_id,
        )
        source_ref = SourceReference(
            source_kind=passage.source_kind,
            revision_id=passage.revision_id,
            locator=passage.locator,
            content_hash=passage.content_hash,
        )
        records.append(
            SourceEvidence(
                evidence_id=identity,
                concept_id=localization.concept.concept_id,
                support=support,
                statement=passage.text,
                source_refs=(source_ref,),
                content_hash=passage.content_hash,
            )
        )
    return records


def _v2_gap_evidence(
    concept: LectureConcept,
    missing_facts: Sequence[MissingFactV2],
    passages: Sequence[SourcePassage],
) -> tuple[SourcePassage, ...]:
    passage_by_source_id = {passage.source_id: passage for passage in passages}
    requested_ids = set(concept.source_passage_ids)
    requested_ids.update(passage_id for fact in missing_facts for passage_id in fact.passage_ids)
    missing_ids = requested_ids - set(passage_by_source_id)
    if missing_ids:
        raise PinnedInputChanged("Gap-generation evidence is absent from the source artifact")
    evidence = tuple(
        passage_by_source_id[source_id]
        for source_id in sorted(requested_ids)
        if passage_by_source_id[source_id].source_kind is not SourceKind.SUMMARY
    )
    if not evidence:
        raise PinnedInputChanged("Audited missing facts have no primary-source evidence")
    return evidence


def _forbidden_cloze_targets(
    *,
    lecture_title: str,
    concept: LectureConcept,
    lecture_entity_count: int,
) -> tuple[str, ...]:
    values = [lecture_title.strip()]
    if lecture_entity_count == 1 and concept.primary_entity.strip():
        values.append(concept.primary_entity.strip())
    return tuple(dict.fromkeys(value for value in values if value))


def _gap_card_from_proposal(
    proposal: GapCardProposal,
    classification: Any,
    *,
    job_id: str,
) -> GapCard:
    return GapCard(
        concept_id=proposal.concept_id,
        text=proposal.fields["Text"],
        extra=proposal.fields.get("Extra", ""),
        selected=classification.disposition == "unique",
        validation_state=("valid" if classification.disposition == "unique" else "overlap"),
        source_refs=proposal.source_refs,
        evidence_ids=proposal.evidence_ids,
        provenance={
            **proposal.provenance,
            "provider": proposal.provider.value,
            "model": proposal.model,
            "prompt_version": proposal.prompt_version,
            "note_type": proposal.note_type,
            "dedupe_disposition": classification.disposition,
            "nearest_matches": [
                {
                    "identifier": match.identifier,
                    "score": match.score,
                    "exact": match.exact,
                }
                for match in classification.nearest_matches
            ],
        },
        initial_tags=proposal.initial_tags,
        content_hash=proposal.content_hash,
        card_id=str(
            uuid5(
                NAMESPACE_URL,
                (
                    f"oms-gap:{job_id}:{proposal.concept_id}:"
                    f"{proposal.fact_id or ''}:{proposal.content_hash}"
                ),
            )
        ),
    )


def _reconciliation_snapshot(
    context: StageContext,
    repository: AnkiCurationRepository,
) -> ReconciliationInput:
    ledger = _ledger(context)
    gaps_payload = _payload(context, CurationStage.GAPS)
    raw_unresolved = gaps_payload.get("unresolved", [])
    if not isinstance(raw_unresolved, list):
        raise PinnedInputChanged("Gap unresolved records are malformed")
    unresolved_fact_ids = tuple(
        str(item["fact_id"])
        for item in raw_unresolved
        if isinstance(item, dict) and item.get("fact_id")
    )
    cards = tuple(repository.list_gap_cards(context.job.id))
    generated_cards = tuple(
        GeneratedResolution(
            card_id=card.card_id,
            fact_id=str(card.provenance.get("fact_id", "")),
            text=card.text,
        )
        for card in cards
        if card.card_id and card.provenance.get("fact_id")
    )
    generated_fact_ids = {card.fact_id for card in generated_cards}
    unresolved_ids = set(unresolved_fact_ids)

    convergence_payload = _payload(
        context,
        CurationStage.CONVERGENCE_PASS_5,
    )
    raw_convergence = convergence_payload.get("concepts", [])
    if not isinstance(raw_convergence, list):
        raise PinnedInputChanged("Convergence artifact is malformed")
    convergence_by_id = {
        str(item["concept_id"]): bool(item.get("converged", False))
        for item in raw_convergence
        if isinstance(item, dict) and item.get("concept_id")
    }

    concepts: list[ConceptResolution] = []
    for concept in ledger.concepts:
        judgment = _coverage_judgment(
            context,
            CurationStage.COVERAGE_RECOMPUTE,
            concept.concept_id,
        )
        missing_fact_ids = tuple(fact.fact_id for fact in judgment.missing_fact_records)
        missing = set(missing_fact_ids)
        resolved = generated_fact_ids | unresolved_ids
        status = (
            "covered"
            if not missing or missing <= generated_fact_ids
            else "intentional_gap"
            if missing <= resolved
            else "incomplete"
        )
        concepts.append(
            ConceptResolution(
                concept_id=concept.concept_id,
                missing_fact_ids=missing_fact_ids,
                status=status,
                converged=convergence_by_id.get(
                    concept.concept_id,
                    False,
                ),
                cited_passage_ids=concept.source_passage_ids,
            )
        )

    audit_payload = _payload(context, CurationStage.CARD_AUDIT)
    raw_verdicts = audit_payload.get("verdicts", [])
    if not isinstance(raw_verdicts, list):
        raise PinnedInputChanged("Card-audit artifact is malformed")
    audit_verdicts = tuple(
        AuditResolution(
            nid=int(item["nid"]),
            verdict=cast(
                Literal["keep", "drop", "uncertain"],
                str(item["verdict"]),
            ),
        )
        for item in raw_verdicts
        if isinstance(item, dict)
    )
    candidates = tuple(repository.list_candidates(context.job.id))
    intentionally_uncited = {item.passage_id for item in ledger.intentionally_uncited}
    source_passage_ids = tuple(
        passage.source_id
        for passage in _source_passages(context)
        if passage.source_kind is not SourceKind.SUMMARY
        and passage.source_id not in intentionally_uncited
    )
    raw_forbidden = gaps_payload.get("forbidden_cloze_targets", [])
    if not isinstance(raw_forbidden, list):
        raise PinnedInputChanged("Gap forbidden-cloze targets are malformed")
    preflight = _payload(context, CurationStage.PREFLIGHT)
    return ReconciliationInput(
        concepts=tuple(concepts),
        generated_cards=generated_cards,
        unresolved_fact_ids=unresolved_fact_ids,
        expected_audit_nids=tuple(candidate.note_id for candidate in candidates),
        audit_verdicts=audit_verdicts,
        source_passage_ids=source_passage_ids,
        forbidden_cloze_targets=tuple(str(value) for value in raw_forbidden),
        prompt_sync_stale=bool(preflight.get("prompt_sync_stale", False)),
    )


def _reconciliation_metrics(
    snapshot: ReconciliationInput,
) -> dict[str, Any]:
    audit_keep = sum(item.verdict == "keep" for item in snapshot.audit_verdicts)
    audit_drop = sum(item.verdict == "drop" for item in snapshot.audit_verdicts)
    audit_uncertain = sum(item.verdict == "uncertain" for item in snapshot.audit_verdicts)
    audit_total = len(snapshot.audit_verdicts)
    unresolved = set(snapshot.unresolved_fact_ids)
    cited = {
        passage_id for concept in snapshot.concepts for passage_id in concept.cited_passage_ids
    }
    return {
        "audit_keep": audit_keep,
        "audit_drop": audit_drop,
        "audit_uncertain": audit_uncertain,
        "audit_drop_rate": audit_drop / audit_total if audit_total else 0.0,
        "unresolved_concepts": sum(
            bool(set(concept.missing_fact_ids) & unresolved) for concept in snapshot.concepts
        ),
        "uncited_passage_ids": sorted(set(snapshot.source_passage_ids) - cited),
        "prompt_sync_stale": snapshot.prompt_sync_stale,
    }


def _card_reconciliation_error(
    report: ReconciliationReport,
    snapshot: CardCentricReconciliationInput | None = None,
) -> str | None:
    if report.can_render_envelope:
        return None
    if snapshot is not None and _reviewable_pending_overflow(report, snapshot):
        return None
    findings = " | ".join(f"{finding.assertion_id}: {finding.message}" for finding in report.failed)
    return "Card-centric reconciliation failed: " + findings


def _reviewable_pending_overflow(
    report: ReconciliationReport,
    snapshot: CardCentricReconciliationInput,
) -> bool:
    """Allow review to obtain a required signature, never permit issuance early."""
    total = len(snapshot.selected_nids) + len(snapshot.selected_generated_card_ids)
    if (
        total <= snapshot.cap
        or snapshot.overflow_acknowledgement is not None
        or {finding.assertion_id for finding in report.failed} != {"selection_cap"}
        or not {
            "selection_mandatory",
            "selection_conservation",
            "selection_metadata",
        }
        <= set(report.passed)
    ):
        return False
    if snapshot.pipeline_contract_version == "card_centric_v1":
        return set(snapshot.selected_nids) == set(snapshot.mandatory_nids)
    if snapshot.pipeline_contract_version != "card_centric_v2":  # pragma: no cover - model bound
        return False
    metadata = tuple(sorted(snapshot.selection_metadata, key=lambda item: item.selected_position))
    expected_identities = {
        *(f"existing:{note_id}" for note_id in snapshot.selected_nids),
        *(f"generated:{card_id}" for card_id in snapshot.selected_generated_card_ids),
    }
    overflow = tuple(item for item in metadata if item.selected_position > snapshot.cap)
    return (
        len(metadata) == total
        and len(expected_identities) == total
        and {item.identity for item in metadata} == expected_identities
        and [item.selected_position for item in metadata] == list(range(1, total + 1))
        and tuple(item.identity for item in metadata) == snapshot.selection_order
        and snapshot.selected_count == total
        and snapshot.below_warning_floor == (total < WARNING_FLOOR)
        and len(overflow) == total - snapshot.cap
        and all(
            item.mandatory
            and item.overflow_reason is not None
            and bool(item.overflow_reason.strip())
            and item.manual_acknowledgement_required
            for item in overflow
        )
    )


def _proposal_payload(proposal: GapCardProposal) -> dict[str, Any]:
    return {
        "concept_id": proposal.concept_id,
        "fact_id": proposal.fact_id,
        "split": proposal.split,
        "image_needed": proposal.image_needed,
        "note_type": proposal.note_type,
        "fields": proposal.fields,
        "source_refs": [
            {
                "source_kind": ref.source_kind.value,
                "revision_id": ref.revision_id,
                "locator": ref.locator,
                "content_hash": ref.content_hash,
            }
            for ref in proposal.source_refs
        ],
        "evidence_ids": proposal.evidence_ids,
        "initial_tags": proposal.initial_tags,
        "provider": proposal.provider.value,
        "model": proposal.model,
        "prompt_version": proposal.prompt_version,
        "confidence": proposal.confidence,
        "content_hash": proposal.content_hash,
        "provenance": proposal.provenance,
    }


def _judgment_usage(
    kind: str,
    results: Sequence[JudgmentResult],
) -> StageUsage | None:
    if not results:
        return None
    request_identity = json.dumps(
        [result.request_id or result.cache_key for result in results],
        sort_keys=True,
        separators=(",", ":"),
    )
    return StageUsage(
        request_id=(f"{kind}:{hashlib.sha256(request_identity.encode()).hexdigest()[:24]}"),
        input_tokens=sum(result.input_tokens for result in results),
        output_tokens=sum(result.output_tokens for result in results),
        cost_microusd=sum(result.cost_microusd for result in results),
    )


def _convergence_usage(
    pass_number: int,
    expansions: Sequence[ExpansionResult],
    judgments: Sequence[JudgmentResult],
) -> StageUsage | None:
    request_ids = [request_id for expansion in expansions for request_id in expansion.request_ids]
    request_ids.extend(result.request_id or result.cache_key for result in judgments)
    if not request_ids:
        return None
    identity = json.dumps(
        request_ids,
        sort_keys=True,
        separators=(",", ":"),
    )
    return StageUsage(
        request_id=(
            f"convergence_pass_{pass_number}:{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
        ),
        input_tokens=(
            sum(result.input_tokens for result in expansions)
            + sum(result.input_tokens for result in judgments)
        ),
        output_tokens=(
            sum(result.output_tokens for result in expansions)
            + sum(result.output_tokens for result in judgments)
        ),
        cost_microusd=(
            sum(result.cost_microusd for result in expansions)
            + sum(result.cost_microusd for result in judgments)
        ),
    )


def _audit_usage(result: AuditRunResult) -> StageUsage | None:
    if not result.request_ids:
        return None
    identity = json.dumps(
        result.request_ids,
        sort_keys=True,
        separators=(",", ":"),
    )
    return StageUsage(
        request_id=(f"card_audit:{hashlib.sha256(identity.encode()).hexdigest()[:24]}"),
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_microusd=result.cost_microusd,
    )


def _proposal_usage(
    proposals: Sequence[GapCardProposal],
) -> StageUsage | None:
    if not proposals:
        return None
    request_ids = [
        str(proposal.provenance.get("generation_request_id", "unknown")) for proposal in proposals
    ]
    digest = hashlib.sha256(
        json.dumps(
            request_ids,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return StageUsage(
        request_id=f"gaps:{digest[:24]}",
        input_tokens=sum(
            int(proposal.provenance.get("generation_input_tokens", 0))
            + int(proposal.provenance.get("entailment_input_tokens", 0))
            for proposal in proposals
        ),
        output_tokens=sum(
            int(proposal.provenance.get("generation_output_tokens", 0))
            + int(proposal.provenance.get("entailment_output_tokens", 0))
            for proposal in proposals
        ),
        cost_microusd=sum(
            int(proposal.provenance.get("generation_cost_microusd", 0))
            + int(proposal.provenance.get("entailment_cost_microusd", 0))
            for proposal in proposals
        ),
    )


def _v2_gap_usage(
    results: Sequence[V2GapGenerationResult],
) -> StageUsage | None:
    attempts = [attempt for result in results for attempt in result.attempts]
    if not attempts:
        return None
    identity = json.dumps(
        [attempt.request_id for attempt in attempts],
        sort_keys=True,
        separators=(",", ":"),
    )
    return StageUsage(
        request_id=(f"gaps_v2:{hashlib.sha256(identity.encode()).hexdigest()[:24]}"),
        input_tokens=sum(attempt.input_tokens for attempt in attempts),
        output_tokens=sum(attempt.output_tokens for attempt in attempts),
        cost_microusd=sum(attempt.cost_microusd for attempt in attempts),
    )
