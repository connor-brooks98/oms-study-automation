import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from oms_hub.anki.audit import AuditRunResult, CardAuditService
from oms_hub.anki.card_centric import (
    CARD_CENTRIC_UNCONDITIONAL_RESIDUAL_RATE,
    CardCentricClassifier,
    CardCentricLedgerService,
    build_snapshot_census,
    build_source_index,
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
    CardGapBatch,
    CardRecord,
    ClassifierResult,
    FastCardClassification,
    FastClassificationResult,
    GeneratedCardResolution,
    SemanticPreFilterResult,
    SnapshotCensus,
    TagScopeResult,
    serialize_card_centric_ledger,
)
from oms_hub.anki.convergence import (
    ConvergenceState,
    ExpansionResult,
    ParaphraseExpansionService,
    update_growth,
)
from oms_hub.anki.dedupe import DeduplicationService
from oms_hub.anki.domain import (
    Candidate,
    CurationStage,
    EvidenceSupport,
    GapCard,
    PipelineContractVersion,
    RetrievalPass,
    SourceEvidence,
    SourceKind,
    SourceReference,
    StageUsage,
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
from oms_hub.anki.reconciliation import (
    AuditResolution,
    CardCentricReconciliationInput,
    ConceptResolution,
    GeneratedResolution,
    ReconciliationInput,
    ReconciliationReport,
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
)
from oms_hub.anki.runtime import AnkiRuntime
from oms_hub.anki.semantic.domain import EmbeddingClient
from oms_hub.anki.semantic.service import SemanticIndexService
from oms_hub.anki.semantic.store import SemanticSnapshotStore
from oms_hub.anki.source_index import (
    LectureSourceIndex,
    SourceScope,
)
from oms_hub.anki.sources import (
    LectureSourceExtractor,
    OutlineRepository,
    SourcePassage,
)
from oms_hub.anki.v2_contracts import (
    AuditVerdictV2,
    CoverageJudgmentV2,
    LectureConceptLedgerV2,
    MissingFactV2,
)
from oms_hub.ingestion.domain import StudyRevision
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.llm.domain import GenerationOptions, ProviderCapabilities, ProviderName
from oms_hub.llm.repository import LLMSettingsRepository
from oms_hub.llm.structured import StructuredOutputError, StructuredTextService

SourceIndexFactory = Callable[[UUID], LectureSourceIndex]


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
            if revision_fingerprint(revision) != job.source_revision_hashes[revision_id]:
                raise PinnedInputChanged(
                    f"Selected source revision {revision_id} changed after the job was queued"
                )
            if not revision.immutable_source_path.is_file():
                raise PinnedInputChanged(
                    f"Selected source revision {revision_id} file is unavailable"
                )

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
        }
        return await handlers[context.stage](context)

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
                    "cache_prefix_sha256": result.cache_prefix_sha256,
                },
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
            terms = {
                concept.primary_entity.casefold(),
                *(alias.casefold() for alias in concept.aliases),
            }
            passages = [
                passage
                for passage in source.passages
                if passage.authority == "slide"
                and any(term in passage.text.casefold() for term in terms)
            ]
            matched[concept.concept_id] = [passage.passage_id for passage in passages]
            counts[concept.concept_id] = sum(len(passage.text.strip()) for passage in passages)
        return StageProduct(
            kind="card_centric_evidence_audit",
            payload={
                "evidence_poor_concept_ids": sorted(
                    key for key, value in counts.items() if value < 50
                ),
                "matched_slide_passage_ids": matched,
                "matched_slide_char_counts": counts,
                "threshold_chars": 50,
            },
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
        if context.job.semantic_generation is None:
            raise PinnedInputChanged("card-centric v2 job has no pinned semantic generation")
        ledger = _card_ledger(context)
        concept_queries = tuple(
            " ".join((concept.primary_entity, *concept.aliases)).strip()
            for concept in ledger.concepts
        )
        scores = await self.semantic.pinned_similarity(
            concept_queries,
            note_ids=scoped_note_ids,
            expected_generation=context.job.semantic_generation,
        )
        if set(scores) != set(scoped_note_ids):
            raise PinnedInputChanged("pinned semantic scores do not cover scoped notes")
        threshold = 0.55
        pre_filtered = tuple(
            sorted(note_id for note_id, score in scores.items() if score >= threshold)
        )
        pre_excluded = tuple(sorted(set(scoped_note_ids) - set(pre_filtered)))
        ordered_scores = sorted(scores.values())
        midpoint = len(ordered_scores) // 2
        median = (
            ordered_scores[midpoint]
            if len(ordered_scores) % 2
            else (ordered_scores[midpoint - 1] + ordered_scores[midpoint]) / 2
        )
        result = SemanticPreFilterResult(
            pre_filtered_note_ids=pre_filtered,
            pre_excluded_note_ids=pre_excluded,
            threshold=threshold,
            similarity_stats={
                "min": min(ordered_scores),
                "max": max(ordered_scores),
                "mean": sum(ordered_scores) / len(ordered_scores),
                "median": median,
            },
        )
        return StageProduct(
            kind="card_centric_prefilter",
            payload=result.model_dump(mode="json"),
        )

    async def _card_fast_classify(self, context: StageContext) -> StageProduct:
        """S4b: authorized fast triage of semantically relevant scoped cards."""
        source = _card_source_index(context)
        cards_by_id = {
            card.note_id: card
            for card in _card_records(_payload(context, CurationStage.SOURCE_INDEX))
        }
        try:
            prefilter = SemanticPreFilterResult.model_validate(
                _payload(context, CurationStage.CARD_PREFILTER)
            )
        except (TypeError, ValueError) as exc:
            raise PinnedInputChanged("card-centric prefilter artifact is malformed") from exc
        scope = TagScopeResult.model_validate(
            _payload(context, CurationStage.CARD_TAG_SCOPE)["scope"]
        )
        if set(prefilter.pre_filtered_note_ids) | set(prefilter.pre_excluded_note_ids) != set(
            scope.scoped_note_ids
        ):
            raise PinnedInputChanged("card-centric prefilter does not partition scoped notes")
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
        results: list[FastCardClassification] = []
        usages: list[StageUsage] = []
        degraded_batches: list[dict[str, Any]] = []
        for batch_index, start in enumerate(range(0, len(selected), 60)):
            batch = selected[start : start + 60]
            expected_note_ids = tuple(card.note_id for card in batch)
            reason_code: str | None = None
            try:
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
                    options=GenerationOptions(cacheable_source_prefix=source.prefix),
                )
            except StructuredOutputError as exc:
                reason_code = "structured_output_invalid"
                usages.append(
                    StageUsage(
                        exc.generation.request_id,
                        exc.generation.input_tokens,
                        exc.generation.output_tokens,
                        exc.generation.cost_microusd,
                    )
                )
            else:
                usages.append(
                    StageUsage(
                        generated.request_id,
                        generated.input_tokens,
                        generated.output_tokens,
                        generated.cost_microusd,
                    )
                )
                reason_code = _fast_batch_degradation_reason(
                    generated.value.results,
                    expected_note_ids=expected_note_ids,
                    allowed_concepts=allowed_concepts,
                    allowed_passages=allowed_passages,
                )
                if reason_code is None:
                    results.extend(generated.value.results)
            if reason_code is not None:
                results.extend(
                    FastCardClassification(
                        note_id=note_id,
                        verdict="NEEDS_REVIEW",
                        reason=f"S4b degraded batch: {reason_code}",
                    )
                    for note_id in expected_note_ids
                )
                degraded_batches.append(
                    {
                        "batch_index": batch_index,
                        "note_ids": list(expected_note_ids),
                        "reason_code": reason_code,
                    }
                )
        fast = FastClassificationResult(
            results=tuple(sorted(results, key=lambda item: item.note_id))
        )
        return StageProduct(
            kind="card_centric_fast_classification",
            payload={
                "fast_classifier": fast.model_dump(mode="json"),
                "source_sha256": source.source_sha256,
                "model_config": context.job.resolved_model_config.canonical_document(),
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
        classifier = CardCentricClassifier(
            self.structured,
            instruction=_card_classifier_prompt(self.prompts),
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
            eligible = (
                selection_eligible_v2(item, source) if is_v2 else selection_eligible(item, source)
            )
            if not eligible:
                continue
            for concept_id in item.covered_concept_ids:
                if concept_id in coverage:
                    coverage[concept_id].append(
                        {
                            "note_id": item.note_id,
                            "supporting_passage_ids": list(item.supporting_passage_ids),
                        }
                    )
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
        if not targets:
            return StageProduct(
                kind="card_centric_residual",
                payload={
                    "audits": [],
                    "classifier": None,
                    "uncovered_concept_ids": [],
                    "residual_mode": residual_mode,
                },
            )
        query_specs = tuple(
            (concept.concept_id, f"{concept.primary_entity} {alias}")
            for concept in targets
            for alias in (concept.aliases or (concept.primary_entity,))
        )
        queries = tuple(query for _, query in query_specs)
        hits = await self.semantic.search(queries, eligible_note_ids=set(cards), limit=12)
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
                if top is None or top < 0.40:
                    row.update({"classified_note_ids": [], "semantic_skip": True})
                    audit.append(row)
                    continue
                gated = tuple(sorted(item.note_id for item in usable if item.score >= 0.50))
                row["classified_note_ids"] = list(gated)
                row["semantic_skip"] = False
                hit_ids.update(gated)
                audit.append(row)
                continue
            hit_ids.update(ids)
            row["classified_note_ids"] = list(ids)
            audit.append(row)
        selected = tuple(cards[note_id] for note_id in sorted(hit_ids) if note_id in cards)
        stage_model = context.job.resolved_model_config.residual_s6
        classified = await CardCentricClassifier(
            self.structured,
            instruction=_card_classifier_prompt(self.prompts),
            capabilities=_structured_capabilities(
                self.structured, ProviderName(stage_model.provider), stage_model.model
            ),
        ).classify(
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
        output: list[GeneratedCardResolution] = []
        evidence_records: list[SourceEvidence] = []
        passages_by_id = {passage.passage_id: passage for passage in source.passages}
        usages: list[StageUsage] = []
        for concept in ledger.concepts:
            if coverage[concept.concept_id]["status"] == "covered":
                continue
            fact_count = concept.suggested_fact_count if is_v2 else 1
            missing_facts = [
                {
                    "fact_id": f"{concept.concept_id}-M{index + 1}",
                    "statement": (
                        concept.fact_descriptions[index] if is_v2 else concept.canonical_statement
                    ),
                    "forbidden_cloze_targets": (
                        list(concept.forbidden_cloze_targets_by_fact[index])
                        if is_v2 and index < len(concept.forbidden_cloze_targets_by_fact)
                        else []
                    ),
                }
                for index in range(fact_count)
            ]
            result = await asyncio.to_thread(
                self.structured.generate_json,
                instruction,
                json.dumps(
                    {
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
                        "lecture_title": self.repository.lecture_title(context.job.lecture_id),
                        "lecture_entity_count": ledger.lecture_entity_count,
                        "forbidden_cloze_targets": list(
                            ledger.all_forbidden_targets
                            if is_v2
                            else ledger.forbidden_cloze_targets
                        ),
                        "is_mechanism": concept.is_mechanism if is_v2 else False,
                        "existing_supports": [],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                output_model=CardGapBatch,
                provider=ProviderName(stage_model.provider),
                model=stage_model.model,
                options=GenerationOptions(cacheable_source_prefix=source.prefix),
            )
            expected = {fact["fact_id"] for fact in missing_facts}
            returned = {item.fact_id for item in result.value.resolutions}
            if returned != expected:
                raise PinnedInputChanged(
                    "card-centric gap output must resolve every requested fact"
                )
            for fact_id in expected:
                matching = [item for item in result.value.resolutions if item.fact_id == fact_id]
                unresolved = [item for item in matching if item.status == "unresolved"]
                generated_rows = [item for item in matching if item.status == "generated"]
                if unresolved and (len(unresolved) != 1 or generated_rows):
                    raise PinnedInputChanged(
                        f"Fact {fact_id}: unresolved output must be one exclusive row"
                    )
                if not unresolved and not generated_rows:
                    raise PinnedInputChanged(f"Fact {fact_id}: resolution is missing")
                if len(generated_rows) > 1 and not all(item.split for item in generated_rows):
                    raise PinnedInputChanged(
                        f"Fact {fact_id}: repeated generated rows must all be split"
                    )
            for item in result.value.resolutions:
                if item.status == "generated" and (
                    not set(item.source_passage_ids)
                    <= {passage.passage_id for passage in source.passages}
                    or (
                        not is_v2
                        and all(value.startswith("SUM:") for value in item.source_passage_ids)
                    )
                ):
                    raise PinnedInputChanged("generated card must cite admissible lecture evidence")
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
                        status=item.status,
                        reason=item.reason,
                    )
                )
            usages.append(
                StageUsage(
                    result.request_id,
                    result.input_tokens,
                    result.output_tokens,
                    result.cost_microusd,
                )
            )
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
        is_v2 = context.job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V2
        fast, fallback_ids = _card_fast_classifier(context) if is_v2 else (None, ())
        if is_v2:
            assert fast is not None
            fallback_ids = _effective_v2_fallback_note_ids(fallback_ids, classifications)
            selected, excluded, generated_ids = select_high_yield_v2(
                classifications,
                fast_classifications=fast.results,
                fast_fallback_note_ids=fallback_ids,
                ledger=ledger,
                source_index=source,
                generated_cards=generated,
                target=65,
                cap=70,
                minimum=60,
            )
        else:
            selected, excluded, generated_ids = select_high_yield(
                classifications,
                ledger=ledger,
                source_index=source,
                generated_card_ids=[
                    item.card_id for item in generated if item.status == "generated"
                ],
            )
        mandatory_note_ids = _mandatory_card_note_ids(
            classifications,
            fast.results if fast is not None else (),
            ledger,
            source,
            v2=is_v2,
        )
        mandatory_generated_card_ids = tuple(
            item.card_id
            for item in generated
            if is_v2
            and item.status == "generated"
            and any(
                concept.concept_id == item.concept_id
                and (concept.importance == "high" or concept.emphasis_flag)
                for concept in ledger.concepts
            )
        )
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
                    }
                },
                content_hash=hashlib.sha256(
                    f"{item.concept_id}\0{item.text}\0{item.extra}".encode()
                ).hexdigest(),
                card_id=item.card_id,
            )
            for item in generated
            if item.status == "generated"
        )
        return StageProduct(
            kind="card_centric_selection",
            payload={
                "selected_existing_note_ids": list(selected),
                "excluded_existing_note_ids": list(excluded),
                "selected_generated_card_ids": list(generated_ids),
                "target": 65,
                "cap": 70,
                "minimum_target": 60,
                "mandatory_note_ids": list(mandatory_note_ids),
                "mandatory_generated_card_ids": list(mandatory_generated_card_ids),
                # Review acknowledgements are issued only after the reviewer has
                # saved the exact selection at a concrete review revision.
                "overflow_acknowledgement": None,
            },
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
        fast, _ = _card_fast_classifier(context)
        existing_ids.update(
            item.note_id for item in fast.results if fast_selection_eligible_v2(item, source)
        )
        existing_notes = tuple(
            _normalized_card_note(cards[note_id])
            for note_id in sorted(existing_ids)
            if note_id in cards
        )
        deduper = DeduplicationService(
            self.embedder,
            duplicate_threshold=0.88,
            overlap_threshold=0.80,
            nearest_limit=5,
        )
        resolved: list[GeneratedCardResolution] = []
        accepted: list[GapCardProposal] = []
        accepted_ids: dict[str, str] = {}
        for item in generated:
            if item.status != "generated":
                resolved.append(item)
                continue
            proposal = _dedupe_gap_proposal(item, context)
            outcome = await deduper.classify(proposal, existing_notes, accepted)
            if outcome.disposition == "unique":
                resolved.append(item)
                accepted.append(proposal)
                accepted_ids[f"proposal:{proposal.concept_id}"] = item.card_id
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
                update["duplicate_of_generated_card_id"] = accepted_ids[nearest]
            else:
                raise PinnedInputChanged("semantic dedupe returned an unknown identity")
            resolved.append(item.model_copy(update=update))
        return StageProduct(
            kind="card_centric_dedupe",
            payload={"resolutions": [item.model_dump(mode="json") for item in resolved]},
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
                    lecture_title=self.repository.lecture_title(context.job.lecture_id),
                    lecture_entity_count=ledger.lecture_entity_count,
                    forbidden_cloze_targets=_forbidden_cloze_targets(
                        lecture_title=self.repository.lecture_title(context.job.lecture_id),
                        concept=concept,
                        lecture_entity_count=ledger.lecture_entity_count,
                    ),
                    existing_supports=tuple(supporting_notes),
                    initial_tags=("OMS::Generated",),
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
                            lecture_title=self.repository.lecture_title(context.job.lecture_id),
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
        selection = _payload(context, CurationStage.CARD_SELECTION)
        scope = TagScopeResult.model_validate(
            _payload(context, CurationStage.CARD_TAG_SCOPE)["scope"]
        )
        census = _card_census(_payload(context, CurationStage.SOURCE_INDEX))
        is_v2 = context.job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V2
        fast, fallback_ids = _card_fast_classifier(context) if is_v2 else (None, ())
        required_fact_ids = tuple(
            f"{concept.concept_id}-M{index + 1}"
            for concept in ledger.concepts
            if coverage[concept.concept_id]["status"] == "uncovered"
            for index in range(
                concept.suggested_fact_count
                if context.job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V2
                else 1
            )
        )
        selected_nids = tuple(selection["selected_existing_note_ids"])
        selected_generated_card_ids = tuple(selection["selected_generated_card_ids"])
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
                if value["status"] == "uncovered"
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
                )
                for item in generated
                if item.status == "generated" and item.card_id in set(selected_generated_card_ids)
            ),
            canonical_generated_cards=tuple(
                GeneratedResolution(
                    card_id=item.card_id,
                    fact_id=item.fact_id,
                    text=item.text,
                    extra=item.extra,
                    split=item.split,
                )
                for item in generated
                if item.status == "generated"
            ),
            canonical_unresolved_fact_ids=tuple(
                item.fact_id for item in generated if item.status != "generated"
            ),
            unresolved_fact_ids=tuple(
                item.fact_id for item in generated if item.status != "generated"
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
            prompt_sync_stale=bool(
                _payload(context, CurationStage.PREFLIGHT).get("prompt_sync_stale", False)
            ),
            untagged_rate=census.trust.untagged_rate,
            target=int(selection["target"]),
            cap=int(selection["cap"]),
            mandatory_nids=tuple(selection["mandatory_note_ids"]),
            mandatory_generated_card_ids=tuple(selection.get("mandatory_generated_card_ids", [])),
            covered_concept_ids_by_nid=existing_coverage_by_nid,
            generated_concept_id_by_card_id={
                item.card_id: item.concept_id for item in generated if item.status == "generated"
            },
            overflow_acknowledgement=selection.get("overflow_acknowledgement"),
            historical_yes_rates=(
                tuple(self.repository.card_centric_yes_rate_history(context.job.id))
                if is_v2 and hasattr(self.repository, "card_centric_yes_rate_history")
                else ()
            ),
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
            },
            blocking_error=_card_reconciliation_error(report),
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
    prompt = AnkiPromptLibrary(catalog.bundled_directory).load("card-centric-classifier")
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
        if coverage[concept.concept_id]["status"] == "uncovered"
    )


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
    """A thorough S4c/S6 result replaces an unresolved S4a fallback row."""
    classified_ids = {item.note_id for item in classifications}
    return tuple(
        note_id for note_id in sorted(set(fallback_note_ids)) if note_id not in classified_ids
    )


def _all_card_classifications(context: StageContext) -> tuple[CardClassification, ...]:
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
        eligible = (
            selection_eligible_v2(item, source) if is_v2 else selection_eligible(item, source)
        )
        if eligible:
            for concept_id in item.covered_concept_ids:
                if concept_id in coverage:
                    coverage[concept_id]["status"] = "covered"
                    coverage[concept_id]["evidence"].append(
                        {
                            "note_id": item.note_id,
                            "supporting_passage_ids": list(item.supporting_passage_ids),
                        }
                    )
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
    """Give every generated row a unique dedupe identity without changing it."""
    stage_model = context.job.resolved_model_config.gap_fill_s7
    return GapCardProposal(
        # DeduplicationService uses this field for its in-batch identifier.  A
        # card ID suffix avoids collisions for two split facts in one concept.
        concept_id=f"{item.concept_id}::{item.card_id}",
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
    fallback = set(fallback_note_ids)
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
                    }
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
        _effective_v2_fallback_note_ids(fallback_note_ids, tuple(thorough_by_id.values()))
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
    "card-centric-gap-v2": "gap_cards_v2",
}


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


def _card_reconciliation_error(report: ReconciliationReport) -> str | None:
    if report.can_render_envelope:
        return None
    findings = " | ".join(f"{finding.assertion_id}: {finding.message}" for finding in report.failed)
    return "Card-centric reconciliation failed: " + findings


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
