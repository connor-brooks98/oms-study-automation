import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from oms_hub.anki.audit import AuditRunResult, CardAuditService
from oms_hub.anki.card_budget import (
    ANKING_CARD_TARGET,
    CUSTOM_CARD_TARGET,
    apply_existing_card_target,
    candidate_concept_ids,
    prioritize_gap_facts,
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
from oms_hub.anki.pipeline import (
    PinnedInputChanged,
    StageContext,
    StageProduct,
)
from oms_hub.anki.prompt_catalog import AnkiPromptCatalogService
from oms_hub.anki.prompts import PromptSynchronizer, StaticPromptSynchronizer
from oms_hub.anki.reconciliation import (
    AuditResolution,
    ConceptResolution,
    GeneratedResolution,
    ReconciliationInput,
    reconcile,
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
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.structured import StructuredTextService

SourceIndexFactory = Callable[[UUID], LectureSourceIndex]

_CONCEPT_CONCURRENCY = 4


async def _bounded_map[Input, Output](
    values: Sequence[Input],
    operation: Callable[[Input], Awaitable[Output]],
    *,
    limit: int = _CONCEPT_CONCURRENCY,
) -> tuple[Output, ...]:
    if limit < 1:
        raise ValueError("concurrency limit must be positive")
    semaphore = asyncio.Semaphore(limit)

    async def run(value: Input) -> Output:
        async with semaphore:
            return await operation(value)

    return tuple(await asyncio.gather(*(run(value) for value in values)))


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
        if set(job.source_revision_hashes) != set(
            job.source_revision_ids
        ):
            raise PinnedInputChanged(
                "Selected source revisions are missing immutable hashes; "
                "start a new curation job"
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
                    f"Selected source revision {revision_id} belongs to "
                    "another lecture"
                )
            if (
                revision_fingerprint(revision)
                != job.source_revision_hashes[revision_id]
            ):
                raise PinnedInputChanged(
                    f"Selected source revision {revision_id} changed after "
                    "the job was queued"
                )
            if not revision.immutable_source_path.is_file():
                raise PinnedInputChanged(
                    f"Selected source revision {revision_id} file is "
                    "unavailable"
                )

        if job.summary_outline_id is not None:
            if job.summary_outline_sha256 is None or self.outlines is None:
                raise PinnedInputChanged(
                    "The job has an incomplete summary pin; start a new "
                    "curation job"
                )
            outline = self.outlines.outline(job.summary_outline_id)
            if outline is None:
                raise PinnedInputChanged("Pinned NotebookLM summary is unavailable")
            if outline.lecture_id != job.lecture_id:
                raise PinnedInputChanged(
                    "Pinned NotebookLM summary belongs to another lecture"
                )
            if not outline.current:
                raise PinnedInputChanged(
                    "Pinned NotebookLM summary is no longer current"
                )
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
                "The job has no pinned companion-index generation; "
                "start a new curation job"
            )
        if companion_generation != job.companion_generation:
            raise PinnedInputChanged(
                f"Pinned companion generation {job.companion_generation} "
                "is no longer active"
            )
        semantic = self.semantic_store.load(
            expected_model=self.semantic_model,
            expected_dimensions=self.semantic_dimensions,
        )
        if job.semantic_generation is None:
            raise PinnedInputChanged(
                "The job has no pinned semantic generation; "
                "start a new curation job"
            )
        if str(semantic.manifest.generation) != job.semantic_generation:
            raise PinnedInputChanged(
                f"Pinned semantic generation {job.semantic_generation} "
                "is no longer active"
            )
        if job.source_index_generation is not None:
            try:
                generation = self.source_indexes(job.id).current_generation()
            except (FileNotFoundError, ValueError) as exc:
                raise PinnedInputChanged(
                    "The job's lecture source index is unavailable"
                ) from exc
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
        prompts: AnkiPromptCatalogService | None = None,
        prompt_sync: PromptSynchronizer | None = None,
    ) -> None:
        self.runtime = runtime
        self.repository = repository
        self.source_extractor = source_extractor
        self.source_indexes = source_indexes
        self.companion = companion
        self.structured = structured
        self.retrieval = RetrievalService(
            companion,
            semantic,
            per_concept_limit=focused_retrieval_limit,
            global_limit=global_retrieval_limit,
        )
        self.embedder = embedder
        self.prompts = prompts or AnkiPromptCatalogService()
        self.prompt_sync = prompt_sync or StaticPromptSynchronizer()

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
            CurationStage.DEDUPE: self._finalize_outcomes,
            CurationStage.GAPS: self._generate_gaps,
            CurationStage.RECONCILIATION: self._reconciliation,
        }
        return await handlers[context.stage](context)

    async def _preflight(self, context: StageContext) -> StageProduct:
        result = await self.runtime.ensure_running()
        if (
            not result.reachable
            or not result.collection_accessible
            or not result.sync_available
        ):
            raise RuntimeError(
                result.blocking_reason or "Local Anki preflight failed"
            )
        sync_result = await asyncio.to_thread(self.prompt_sync.sync)
        prompt_snapshot = await asyncio.to_thread(
            self.prompts.load_job_snapshot,
            lcl_id=context.job.lcl_prompt_version,
            coverage_id=context.job.judgment_rubric_version,
            gap_id=context.job.gap_prompt_version,
        )
        return StageProduct(
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
                        "source_paths": [
                            str(path) for path in prompt.source_paths
                        ],
                        "metadata": prompt.metadata.model_dump(
                            mode="json",
                            by_alias=True,
                        ),
                    }
                    for prompt in prompt_snapshot.prompts
                ],
                "prompt_sync_stale": sync_result.stale,
                "prompt_sync_detail": sync_result.detail,
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
        if any(
            passage.lecture_id != context.job.lecture_id
            for passage in passages
        ):
            raise ValueError(
                "selected source revisions contain another lecture"
            )
        generation = await self.source_indexes(
            context.job.id
        ).refresh(passages)
        return StageProduct(
            kind="lecture_source_index",
            payload={
                "generation": str(generation.generation),
                "passage_count": generation.passage_count,
                "indexed_count": generation.indexed_count,
                "passages": [
                    _passage_payload(passage) for passage in passages
                ],
            },
            job_pins={
                "source_index_generation": str(generation.generation)
            },
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
            raise PinnedInputChanged(
                "Pinned LCL prompt schema is unsupported"
            )
        lcl_schema = cast(Literal["lcl_v1", "lcl_v2"], schema_name)
        service = LCLService(
            self.structured,
            provider=_provider(context),
            model=context.job.model,
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
        scope = _retrieval_scope(context)

        async def retrieve(
            concept: LectureConcept,
        ) -> tuple[str, list[dict[str, Any]]]:
            candidates = await self.retrieval.retrieve_pass_1(
                concept,
                scope,
            )
            return concept.concept_id, [
                _candidate_payload(candidate)
                for candidate in candidates
            ]

        groups = dict(
            await _bounded_map(_ledger(context).concepts, retrieve)
        )
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
            model=context.job.model,
            prompt_version=context.job.judgment_rubric_version,
        )
        source_scope = SourceScope(
            revision_ids=context.job.source_revision_ids,
            source_kinds=tuple(SourceKind),
        )

        async def localize(
            concept: LectureConcept,
        ) -> tuple[str, dict[str, Any] | None, tuple[SourceEvidence, ...]]:
            judgment = _coverage_judgment(
                context,
                CurationStage.JUDGMENT_PASS_1,
                concept.concept_id,
            )
            if judgment.status == "covered":
                return concept.concept_id, None, ()
            localization = await service.localize(concept, source_scope)
            queries = (
                service.build_queries(localization)
                if localization.support != "unsupported"
                and localization.evidence
                else ()
            )
            return concept.concept_id, {
                "support": localization.support,
                "rationale": localization.rationale,
                "evidence": [
                    _passage_payload(passage)
                    for passage in localization.evidence
                ],
                "queries": [
                    query.model_dump(mode="json") for query in queries
                ],
            }, tuple(_evidence_records(localization))

        localization_results = await _bounded_map(
            ledger.concepts,
            localize,
        )
        localizations = {
            concept_id: payload
            for concept_id, payload, _ in localization_results
            if payload is not None
        }
        evidence_records = tuple(
            record
            for _, _, records in localization_results
            for record in records
        )
        return StageProduct(
            kind="source_rescue",
            payload={"localizations": localizations},
            source_evidence=evidence_records,
        )

    async def _retrieval_pass_2(
        self,
        context: StageContext,
    ) -> StageProduct:
        ledger_by_id = {
            concept.concept_id: concept
            for concept in _ledger(context).concepts
        }
        rescue = _payload(context, CurationStage.RESCUE)
        localizations = cast(
            dict[str, dict[str, Any]],
            rescue.get("localizations", {}),
        )
        scope = _retrieval_scope(context)

        async def retrieve(
            item: tuple[str, dict[str, Any]],
        ) -> tuple[str, list[dict[str, Any]]]:
            concept_id, localization = item
            raw_queries = localization.get("queries", [])
            queries = [
                RescueQuery.model_validate(query)
                for query in raw_queries
            ]
            if not queries:
                return concept_id, []
            candidates = await self.retrieval.retrieve_pass_2(
                ledger_by_id[concept_id],
                queries,
                scope,
            )
            return concept_id, [
                _candidate_payload(candidate)
                for candidate in candidates
            ]

        groups = dict(
            await _bounded_map(tuple(localizations.items()), retrieve)
        )
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
        merged = _merge_candidates(
            (*pass_1, *(product.candidates or ()))
        )
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
        if pass_number > 3:
            ordered_states = tuple(
                states[concept.concept_id] for concept in ledger.concepts
            )
            manual_review = tuple(
                state.concept_id
                for state in ordered_states
                if pass_number == 5 and not state.converged
            )
            previous_stage = {
                4: CurationStage.CONVERGENCE_PASS_3,
                5: CurationStage.CONVERGENCE_PASS_4,
            }[pass_number]
            schema_name = _payload(context, previous_stage).get(
                "schema_name",
                "coverage_v1",
            )
            return StageProduct(
                kind=f"convergence_pass_{pass_number}",
                payload={
                    "pass_number": pass_number,
                    "schema_name": schema_name,
                    "concepts": [
                        state.model_dump(mode="json")
                        for state in ordered_states
                    ],
                    "active_concept_ids": [],
                    "groups": {},
                    "expanded_paraphrases": expanded_by_id,
                    "expansions": {},
                    "judgments": {},
                    "needs_manual_review": bool(manual_review),
                    "manual_review_concept_ids": list(manual_review),
                    "compatibility_skipped_concept_ids": [],
                    "optimization_skipped": True,
                    "optimization_reason": (
                        "Stopped after one convergence pass because later "
                        "passes were low-yield in measured lecture runs."
                    ),
                },
                candidates=tuple(
                    self.repository.list_candidates(context.job.id)
                ),
            )
        concepts = {concept.concept_id: concept for concept in ledger.concepts}
        next_states: dict[str, ConvergenceState] = dict(states)
        compatibility_skipped: list[str] = []
        active: list[
            tuple[str, ConvergenceState, CoverageJudgment]
        ] = []
        for concept_id, state in states.items():
            if state.converged:
                continue
            judgment = _coverage_judgment(
                context,
                _final_judgment_stage(context, concept_id),
                concept_id,
            )
            if not judgment.missing_facts:
                next_states[concept_id] = state.model_copy(
                    update={"converged": True}
                )
            elif not concepts[concept_id].primary_entity:
                compatibility_skipped.append(concept_id)
                next_states[concept_id] = state.model_copy(
                    update={"converged": True}
                )
            else:
                active.append((concept_id, state, judgment))
        existing = tuple(self.repository.list_candidates(context.job.id))
        if not active:
            ordered_states = tuple(
                next_states[concept.concept_id]
                for concept in ledger.concepts
            )
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
                    "concepts": [
                        state.model_dump(mode="json")
                        for state in ordered_states
                    ],
                    "active_concept_ids": [],
                    "groups": {},
                    "expanded_paraphrases": expanded_by_id,
                    "expansions": {},
                    "judgments": {},
                    "needs_manual_review": False,
                    "manual_review_concept_ids": [],
                    "compatibility_skipped_concept_ids": (
                        compatibility_skipped
                    ),
                },
                candidates=existing,
            )
        prompt_text, prompt_hash = _resolved_prompt(
            context,
            "paraphrase-expansion",
        )
        if _resolved_prompt_schema(
            context,
            "paraphrase-expansion",
        ) != "paraphrase_v2":
            raise PinnedInputChanged(
                "Pinned paraphrase-expansion schema is unsupported"
            )
        expansion_service = ParaphraseExpansionService(
            self.structured,
            provider=_provider(context),
            model=context.job.model,
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
            raise PinnedInputChanged(
                "Pinned coverage prompt schema is unsupported"
            )
        coverage_service = JudgmentService(
            self.structured,
            self.repository,
            self.companion,
            provider=_provider(context),
            model=context.job.model,
            prompt_version=context.job.judgment_rubric_version,
            prompt_text=coverage_text,
            prompt_hash=coverage_hash,
            schema_name=cast(
                Literal["coverage_v1", "coverage_v2"],
                coverage_schema_name,
            ),
        )
        candidates_by_id = {
            candidate.note_id: candidate for candidate in existing
        }
        passages = _source_passages(context)
        scope = _retrieval_scope(context)
        groups: dict[str, list[dict[str, Any]]] = {}
        judgments: dict[str, dict[str, Any]] = {}
        expansions: dict[str, dict[str, Any]] = {}
        active_ids: list[str] = []
        expansion_results: list[ExpansionResult] = []
        judgment_results: list[JudgmentResult] = []
        projected: list[Candidate] = []

        async def converge_concept(
            item: tuple[str, ConvergenceState, CoverageJudgment],
        ) -> tuple[
            str,
            ConvergenceState,
            ExpansionResult,
            tuple[str, ...],
            tuple[Candidate, ...],
            JudgmentResult | None,
            tuple[Candidate, ...],
        ]:
            concept_id, prior_state, prior_judgment = item
            concept = concepts[concept_id]
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
            queries = expansion.expansion.paraphrases
            retrieved = await self.retrieval.retrieve_convergence(
                concept,
                queries,
                scope,
                pass_number=pass_number,
            )
            growth = update_growth(
                seen_note_ids=prior_state.seen_note_ids,
                retrieved_note_ids=tuple(
                    candidate.note_id for candidate in retrieved
                ),
            )
            new_ids = set(growth.new_note_ids)
            new_candidates = tuple(
                candidate
                for candidate in retrieved
                if candidate.note_id in new_ids
            )
            coverage_complete = not prior_judgment.missing_facts
            result: JudgmentResult | None = None
            concept_projected: tuple[Candidate, ...] = ()
            if new_candidates:
                support_ids = _combined_support_ids(context, concept_id)
                concept_candidates = {
                    **candidates_by_id,
                    **{
                        candidate.note_id: candidate
                        for candidate in new_candidates
                    },
                }
                unknown = support_ids - set(concept_candidates)
                if unknown:
                    raise PinnedInputChanged(
                        "Coverage support is absent from current candidates"
                    )
                judge_ids = support_ids | new_ids
                result = await asyncio.to_thread(
                    coverage_service.judge,
                    concept,
                    [concept_candidates[nid] for nid in sorted(judge_ids)],
                    passages=passages,
                )
                runtime = (
                    runtime_judgment_from_v2(result.judgment)
                    if isinstance(result.judgment, CoverageJudgmentV2)
                    else result.judgment
                )
                supporting = set(runtime.supporting_note_ids)
                concept_projected = tuple(
                    _judged_candidate(
                        concept_candidates[nid],
                        runtime,
                        selected=True,
                    )
                    for nid in sorted(supporting)
                )
                coverage_complete = not runtime.missing_facts
            return (
                concept_id,
                ConvergenceState(
                    concept_id=concept_id,
                    passes_run=pass_number,
                    seen_note_ids=growth.seen_note_ids,
                    growth=(*prior_state.growth, growth.growth),
                    converged=growth.converged or coverage_complete,
                ),
                expansion,
                queries,
                new_candidates,
                result,
                concept_projected,
            )

        converged_results = await _bounded_map(active, converge_concept)
        for (
            concept_id,
            state,
            expansion,
            queries,
            new_candidates,
            result,
            concept_projected,
        ) in converged_results:
            active_ids.append(concept_id)
            next_states[concept_id] = state
            expansion_results.append(expansion)
            expanded_by_id.setdefault(concept_id, []).extend(queries)
            expansions[concept_id] = {
                **expansion.expansion.model_dump(mode="json"),
                "request_ids": list(expansion.request_ids),
            }
            groups[concept_id] = [
                _candidate_payload(candidate) for candidate in new_candidates
            ]
            projected.extend(concept_projected)
            if result is not None:
                judgment_results.append(result)
                judgments[concept_id] = {
                    "judgment": result.judgment.model_dump(mode="json"),
                    "cache_key": result.cache_key,
                    "cache_hit": result.cache_hit,
                    "provider": result.provider.value,
                    "model": result.model,
                    "request_id": result.request_id,
                }
        ordered_states = tuple(
            next_states[concept.concept_id] for concept in ledger.concepts
        )
        manual_review = tuple(
            state.concept_id
            for state in ordered_states
            if pass_number == 5 and not state.converged
        )
        candidates = _merge_candidates((*existing, *projected))
        return StageProduct(
            kind=f"convergence_pass_{pass_number}",
            payload={
                "pass_number": pass_number,
                "schema_name": coverage_schema_name,
                "concepts": [
                    state.model_dump(mode="json") for state in ordered_states
                ],
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
            raise PinnedInputChanged(
                "Pinned card-audit prompt schema is unsupported"
            )
        batch_size = metadata.get("batch_size", 30)
        if not isinstance(batch_size, int) or batch_size < 1:
            raise PinnedInputChanged(
                "Pinned card-audit batch size is malformed"
            )
        service = CardAuditService(
            self.structured,
            self.repository,
            self.companion,
            provider=_provider(context),
            model=context.job.model,
            prompt_text=prompt_text,
            prompt_hash=prompt_hash,
            batch_size=batch_size,
        )
        result = await asyncio.to_thread(
            service.audit,
            lecture_id=context.job.lecture_id,
            lecture_title=self.repository.lecture_title(
                context.job.lecture_id
            ),
            lecture_entity_count=_ledger(context).lecture_entity_count,
            candidates=candidates,
            passages=_source_passages(context),
        )
        verdict_by_id = {verdict.nid: verdict for verdict in result.verdicts}
        audited = tuple(
            _audited_candidate(candidate, verdict_by_id[candidate.note_id])
            for candidate in candidates
        )
        selection = apply_existing_card_target(
            audited,
            _ledger(context).concepts,
            target=ANKING_CARD_TARGET,
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
                "verdicts": [
                    verdict.model_dump(mode="json")
                    for verdict in result.verdicts
                ],
                "counts": counts,
                "selection_budget": {
                    "mode": "soft_target",
                    "target": selection.target,
                    "eligible_existing_cards": selection.eligible_count,
                    "selected_existing_cards": selection.selected_count,
                    "concept_coverage_floor": selection.coverage_floor,
                    "overflow_cards": selection.overflow_count,
                },
            },
            candidates=selection.candidates,
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
            raise PinnedInputChanged(
                "Pinned coverage prompt schema is unsupported"
            )
        coverage_schema = cast(
            Literal["coverage_v1", "coverage_v2"],
            schema_name,
        )
        service = JudgmentService(
            self.structured,
            self.repository,
            self.companion,
            provider=_provider(context),
            model=context.job.model,
            prompt_version=context.job.judgment_rubric_version,
            prompt_text=prompt_text,
            prompt_hash=prompt_hash,
            schema_name=coverage_schema,
        )
        audit_payload = _payload(context, CurationStage.CARD_AUDIT)
        raw_verdicts = audit_payload.get("verdicts")
        if not isinstance(raw_verdicts, list):
            raise PinnedInputChanged("Card-audit artifact is malformed")
        verdicts = tuple(
            AuditVerdictV2.model_validate(value) for value in raw_verdicts
        )
        candidates_by_id = {
            candidate.note_id: candidate
            for candidate in self.repository.list_candidates(context.job.id)
        }
        if set(candidates_by_id) != {verdict.nid for verdict in verdicts}:
            raise PinnedInputChanged(
                "Card-audit artifact does not partition current candidates"
            )
        keep_ids = {
            verdict.nid
            for verdict in verdicts
            if verdict.verdict == "keep"
            and candidates_by_id[verdict.nid].selected
        }
        passages = _source_passages(context)

        async def recompute(
            concept: LectureConcept,
        ) -> tuple[str, dict[str, Any], JudgmentResult | None]:
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
                raise PinnedInputChanged(
                    "Coverage support is absent from current candidates"
                )
            surviving_ids = tuple(
                sorted(prior_support_ids & keep_ids)
            )
            if (
                surviving_ids == tuple(sorted(prior_support_ids))
                and prior_support_ids == set(prior.supporting_note_ids)
            ):
                return concept.concept_id, {
                    **_judgment_record(
                        context,
                        prior_stage,
                        concept.concept_id,
                    ),
                    "recomputed": False,
                }, None
            result = await asyncio.to_thread(
                service.judge,
                concept,
                [candidates_by_id[nid] for nid in surviving_ids],
                passages=passages,
            )
            return concept.concept_id, {
                "judgment": result.judgment.model_dump(mode="json"),
                "cache_key": result.cache_key,
                "cache_hit": result.cache_hit,
                "provider": result.provider.value,
                "model": result.model,
                "request_id": result.request_id,
                "recomputed": True,
            }, result

        recomputed = await _bounded_map(ledger.concepts, recompute)
        results = {
            concept_id: record for concept_id, record, _ in recomputed
        }
        usages = [result for _, _, result in recomputed if result is not None]
        return StageProduct(
            kind="audited_coverage_judgments",
            payload={
                "schema_name": schema_name,
                "judgments": results,
            },
            usage=_judgment_usage("coverage_recompute", usages),
            cache_hits=sum(result.cache_hit for result in usages),
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
            raise PinnedInputChanged(
                "Pinned gap-generation prompt schema is unsupported"
            )
        return await self._generate_gaps_v1(context)

    async def _generate_gaps_v1(
        self,
        context: StageContext,
    ) -> StageProduct:
        ledger_by_id = {
            concept.concept_id: concept
            for concept in _ledger(context).concepts
        }
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
            model=context.job.model,
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
            for candidate in self.repository.list_candidates(
                context.job.id
            )
            if (note := self.companion.get_note(candidate.note_id))
            is not None
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
                        "valid"
                        if classification.disposition == "unique"
                        else "overlap"
                    ),
                    source_refs=proposal.source_refs,
                    evidence_ids=proposal.evidence_ids,
                    provenance={
                        **proposal.provenance,
                        "provider": proposal.provider.value,
                        "model": proposal.model,
                        "prompt_version": proposal.prompt_version,
                        "confidence": proposal.confidence,
                        "dedupe_disposition": (
                            classification.disposition
                        ),
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
            model=context.job.model,
            prompt_version=context.job.gap_prompt_version,
            prompt_text=prompt_text,
            prompt_hash=prompt_hash,
        )
        all_candidates = tuple(
            self.repository.list_candidates(context.job.id)
        )
        candidate_by_id = {
            candidate.note_id: candidate for candidate in all_candidates
        }
        proposals: list[GapCardProposal] = []
        unresolved: list[dict[str, Any]] = []
        results: list[V2GapGenerationResult] = []
        expected_fact_ids: set[str] = set()
        evidence_records = {
            record.evidence_id: record
            for record in self.repository.list_source_evidence(
                context.job.id
            )
        }
        missing_by_concept: dict[str, tuple[MissingFactV2, ...]] = {}
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
            missing_by_concept[concept.concept_id] = missing_facts
            expected_fact_ids.update(fact.fact_id for fact in missing_facts)

        selected_existing_concepts = {
            concept_id
            for candidate in all_candidates
            if candidate.selected
            for concept_id in candidate_concept_ids(candidate)
        }
        gap_selection = prioritize_gap_facts(
            ledger.concepts,
            missing_by_concept,
            selected_existing_concepts,
            target=CUSTOM_CARD_TARGET,
        )
        for concept_id, facts in gap_selection.deferred_by_concept.items():
            unresolved.extend(
                {
                    "concept_id": concept_id,
                    "fact_id": fact.fact_id,
                    "status": "unresolved",
                    "reason": "deferred_by_soft_lecture_card_target",
                }
                for fact in facts
            )

        generation_requests: list[
            tuple[str, V2GapGenerationRequest]
        ] = []
        for concept in ledger.concepts:
            judgment = _coverage_judgment(
                context,
                CurationStage.COVERAGE_RECOMPUTE,
                concept.concept_id,
            )
            missing_facts = gap_selection.selected_by_concept[
                concept.concept_id
            ]
            if not missing_facts:
                continue
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
                    rationale=(
                        "Audited missing facts are grounded in primary sources."
                    ),
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
            generation_requests.append(
                (
                    concept.concept_id,
                    V2GapGenerationRequest(
                        concept=concept,
                        missing_facts=missing_facts,
                        evidence=evidence,
                        lecture_title=self.repository.lecture_title(
                            context.job.lecture_id
                        ),
                        lecture_entity_count=ledger.lecture_entity_count,
                        forbidden_cloze_targets=_forbidden_cloze_targets(
                            lecture_title=self.repository.lecture_title(
                                context.job.lecture_id
                            ),
                            concept=concept,
                            lecture_entity_count=ledger.lecture_entity_count,
                        ),
                        existing_supports=tuple(supporting_notes),
                        initial_tags=("OMS::Generated",),
                    ),
                )
            )

        async def generate(
            item: tuple[str, V2GapGenerationRequest],
        ) -> tuple[str, V2GapGenerationResult]:
            concept_id, request = item
            return concept_id, await asyncio.to_thread(
                service.generate,
                request,
            )

        generated_results = await _bounded_map(
            generation_requests,
            generate,
        )
        for concept_id, result in generated_results:
            results.append(result)
            proposals.extend(result.proposals)
            unresolved.extend(
                {
                    "concept_id": concept_id,
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
            str(item.get("fact_id", ""))
            for item in unresolved
            if item.get("fact_id")
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
                "selection_budget": {
                    "mode": "soft_target",
                    "target": gap_selection.target,
                    "selected_missing_facts": gap_selection.selected_count,
                    "deferred_missing_facts": sum(
                        len(facts)
                        for facts in gap_selection.deferred_by_concept.values()
                    ),
                    "overflow_facts": gap_selection.overflow_count,
                    "generated_cards": len(cards),
                },
                "forbidden_cloze_targets": sorted(
                    {
                        target
                        for concept in ledger.concepts
                        for target in _forbidden_cloze_targets(
                            lecture_title=self.repository.lecture_title(
                                context.job.lecture_id
                            ),
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
                            "message": (
                                "Legacy V1 run cannot provide V2 reconciliation"
                            ),
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
                "Reconciliation failed: " + ", ".join(failed_ids)
                if failed_ids
                else None
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
        ledger_by_id = {
            concept.concept_id: concept
            for concept in _ledger(context).concepts
        }
        prompt_text, prompt_hash = _resolved_prompt(
            context,
            context.job.judgment_rubric_version,
        )
        schema_name = _resolved_prompt_schema(
            context,
            context.job.judgment_rubric_version,
        )
        if schema_name not in {"coverage_v1", "coverage_v2"}:
            raise PinnedInputChanged(
                "Pinned coverage prompt schema is unsupported"
            )
        coverage_schema = cast(
            Literal["coverage_v1", "coverage_v2"],
            schema_name,
        )
        service = JudgmentService(
            self.structured,
            self.repository,
            self.companion,
            provider=_provider(context),
            model=context.job.model,
            prompt_version=context.job.judgment_rubric_version,
            prompt_text=prompt_text,
            prompt_hash=prompt_hash,
            schema_name=coverage_schema,
        )
        passages = _source_passages(context)

        async def judge_group(
            item: tuple[str, list[dict[str, Any]]],
        ) -> tuple[
            str,
            dict[str, Any],
            tuple[Candidate, ...],
            tuple[JudgmentResult, ...],
        ]:
            concept_id, values = item
            candidates = [
                _candidate_from_payload(value) for value in values
            ]
            concept_usages: list[JudgmentResult] = []
            for deck_candidates in _priority_candidate_groups(candidates):
                result = await asyncio.to_thread(
                    service.judge,
                    ledger_by_id[concept_id],
                    deck_candidates,
                    passages=passages,
                )
                concept_usages.append(result)
                record = {
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
                concept_candidates = tuple(
                    _judged_candidate(
                        candidate,
                        runtime,
                        selected=True,
                    )
                    for candidate in deck_candidates
                    if candidate.note_id in supporting
                )
                if supporting:
                    return (
                        concept_id,
                        record,
                        concept_candidates,
                        tuple(concept_usages),
                    )
            return concept_id, record, (), tuple(concept_usages)

        judged = await _bounded_map(tuple(raw_groups.items()), judge_group)
        results = {concept_id: record for concept_id, record, _, _ in judged}
        projected = [
            candidate
            for _, _, candidates, _ in judged
            for candidate in candidates
        ]
        usages = [result for _, _, _, values in judged for result in values]
        merged = _merge_candidates(projected)
        return StageProduct(
            kind=kind,
            payload={
                "schema_name": schema_name,
                "judgments": results,
                "projected_candidates": [
                    _candidate_payload(candidate)
                    for candidate in merged
                ],
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
    raise PinnedInputChanged(
        f"Pinned prompt {prompt_id} is unavailable; start a new curation job"
    )


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
    raise PinnedInputChanged(
        f"Pinned prompt {prompt_id} is unavailable; start a new curation job"
    )


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
    raise PinnedInputChanged(
        f"Pinned prompt {prompt_id} is unavailable; start a new curation job"
    )


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
        raise PinnedInputChanged(
            f"Committed {stage.value} artifact is unavailable"
        ) from None


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
        ledger = LectureConceptLedgerV2.model_validate(
            payload.get("ledger")
        )
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
        return runtime_judgment_from_v2(
            CoverageJudgmentV2.model_validate(raw["judgment"])
        )
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
    raise PinnedInputChanged(
        "Coverage judgment artifact is absent for a lecture concept"
    )


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
            first_ids = _group_note_ids(
                first_groups.get(concept.concept_id, [])
            )
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
                retrieved_note_ids=_group_note_ids(
                    second_groups.get(concept.concept_id, [])
                ),
            )
            states[concept.concept_id] = ConvergenceState(
                concept_id=concept.concept_id,
                passes_run=2,
                seen_note_ids=second_update.seen_note_ids,
                growth=(
                    1.0 if first_ids else 0.0,
                    second_update.growth,
                ),
                converged=(
                    not judgment.missing_facts or second_update.converged
                ),
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
        raise PinnedInputChanged(
            "Convergence artifact does not partition lecture concepts"
        )
    raw_expanded = payload.get("expanded_paraphrases", {})
    if not isinstance(raw_expanded, dict):
        raise PinnedInputChanged("Convergence paraphrases are malformed")
    expanded: dict[str, list[str]] = {}
    for concept_id in expected:
        values = raw_expanded.get(concept_id, [])
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip()
            for value in values
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
    return tuple(
        sorted({_candidate_from_payload(value).note_id for value in values})
    )


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
                None
                if value.get("slide_number") is None
                else int(value["slide_number"])
            ),
            start_seconds=(
                None
                if value.get("start_seconds") is None
                else float(value["start_seconds"])
            ),
            end_seconds=(
                None
                if value.get("end_seconds") is None
                else float(value["end_seconds"])
            ),
            summary_backrefs=tuple(
                str(item) for item in value.get("summary_backrefs", [])
            ),
            summary_section=cast(Any, value.get("summary_section")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PinnedInputChanged(
            "Source passage artifact is malformed"
        ) from exc


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
        scores={
            str(key): float(score)
            for key, score in dict(value["scores"]).items()
        },
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
            "include"
            if selected
            else "uncertain"
            if judgment.status == "partial"
            else "drop"
        ),
        confidence=(
            1.0
            if judgment.status == "covered"
            else 0.7
            if judgment.status == "partial"
            else 0.0
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
        confidence={"keep": 1.0, "drop": 0.0, "uncertain": 0.5}[
            audit.verdict
        ],
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
    return tuple(
        _candidate_from_payload(value)
        for value in values
        if isinstance(value, dict)
    )


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
        evidence=tuple(
            _passage_from_payload(passage)
            for passage in raw_evidence
        ),
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
            if (passage := by_id[reference.passage_id]).source_kind
            is not SourceKind.SUMMARY
        )
    except KeyError as exc:
        raise PinnedInputChanged(
            "Concept evidence is absent from the source artifact"
        ) from exc
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
    requested_ids.update(
        passage_id
        for fact in missing_facts
        for passage_id in fact.passage_ids
    )
    missing_ids = requested_ids - set(passage_by_source_id)
    if missing_ids:
        raise PinnedInputChanged(
            "Gap-generation evidence is absent from the source artifact"
        )
    evidence = tuple(
        passage_by_source_id[source_id]
        for source_id in sorted(requested_ids)
    )
    if not any(
        passage.source_kind is not SourceKind.SUMMARY for passage in evidence
    ):
        raise PinnedInputChanged(
            "Audited missing facts have no primary-source evidence"
        )
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
        missing_fact_ids = tuple(
            fact.fact_id for fact in judgment.missing_fact_records
        )
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
    intentionally_uncited = {
        item.passage_id for item in ledger.intentionally_uncited
    }
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
        expected_audit_nids=tuple(
            candidate.note_id for candidate in candidates
        ),
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
    audit_uncertain = sum(
        item.verdict == "uncertain" for item in snapshot.audit_verdicts
    )
    audit_total = len(snapshot.audit_verdicts)
    unresolved = set(snapshot.unresolved_fact_ids)
    cited = {
        passage_id
        for concept in snapshot.concepts
        for passage_id in concept.cited_passage_ids
    }
    return {
        "audit_keep": audit_keep,
        "audit_drop": audit_drop,
        "audit_uncertain": audit_uncertain,
        "audit_drop_rate": audit_drop / audit_total if audit_total else 0.0,
        "unresolved_concepts": sum(
            bool(set(concept.missing_fact_ids) & unresolved)
            for concept in snapshot.concepts
        ),
        "uncited_passage_ids": sorted(
            set(snapshot.source_passage_ids) - cited
        ),
        "prompt_sync_stale": snapshot.prompt_sync_stale,
    }


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
        request_id=(
            f"{kind}:"
            f"{hashlib.sha256(request_identity.encode()).hexdigest()[:24]}"
        ),
        input_tokens=sum(result.input_tokens for result in results),
        output_tokens=sum(result.output_tokens for result in results),
        cost_microusd=sum(result.cost_microusd for result in results),
    )


def _convergence_usage(
    pass_number: int,
    expansions: Sequence[ExpansionResult],
    judgments: Sequence[JudgmentResult],
) -> StageUsage | None:
    request_ids = [
        request_id
        for expansion in expansions
        for request_id in expansion.request_ids
    ]
    request_ids.extend(
        result.request_id or result.cache_key for result in judgments
    )
    if not request_ids:
        return None
    identity = json.dumps(
        request_ids,
        sort_keys=True,
        separators=(",", ":"),
    )
    return StageUsage(
        request_id=(
            f"convergence_pass_{pass_number}:"
            f"{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
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
        request_id=(
            "card_audit:"
            f"{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
        ),
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
        str(proposal.provenance.get("generation_request_id", "unknown"))
        for proposal in proposals
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
        request_id=(
            "gaps_v2:"
            f"{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
        ),
        input_tokens=sum(attempt.input_tokens for attempt in attempts),
        output_tokens=sum(attempt.output_tokens for attempt in attempts),
        cost_microusd=sum(attempt.cost_microusd for attempt in attempts),
    )
