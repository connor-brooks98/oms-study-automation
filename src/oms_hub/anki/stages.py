import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any, Literal, cast
from uuid import UUID

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
    GapCardProposal,
    GapCardService,
    SupportedGap,
)
from oms_hub.anki.index import AnkiIndex, CompanionFilters
from oms_hub.anki.judgment import (
    CoverageJudgment,
    JudgmentResult,
    JudgmentService,
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
from oms_hub.anki.prompts import (
    AnkiPromptLibrary,
    PromptSynchronizer,
    StaticPromptSynchronizer,
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
from oms_hub.anki.v2_contracts import LectureConceptLedgerV2
from oms_hub.ingestion.domain import StudyRevision
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.structured import StructuredTextService

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
        prompts: AnkiPromptLibrary | None = None,
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
        self.prompts = prompts or AnkiPromptLibrary()
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
            CurationStage.DEDUPE: self._finalize_outcomes,
            CurationStage.GAPS: self._generate_gaps,
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
        prompt_ids = (
            context.job.lcl_prompt_version,
            context.job.judgment_rubric_version,
            "card-relevance-audit",
            context.job.gap_prompt_version,
            "paraphrase-expansion",
        )
        prompt_snapshot = await asyncio.to_thread(
            self.prompts.load_many,
            prompt_ids,
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
        groups: dict[str, list[dict[str, Any]]] = {}
        for concept in _ledger(context).concepts:
            candidates = await self.retrieval.retrieve_pass_1(
                concept,
                _retrieval_scope(context),
            )
            groups[concept.concept_id] = [
                _candidate_payload(candidate)
                for candidate in candidates
            ]
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
        judgments = _judgment_payload(
            context,
            CurationStage.JUDGMENT_PASS_1,
        )
        service = RescueService(
            self.source_indexes(context.job.id),
            self.structured,
            provider=_provider(context),
            model=context.job.model,
            prompt_version=context.job.judgment_rubric_version,
        )
        localizations: dict[str, dict[str, Any]] = {}
        evidence_records: list[SourceEvidence] = []
        for concept in ledger.concepts:
            judgment = CoverageJudgment.model_validate(
                judgments[concept.concept_id]["judgment"]
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
                if localization.support != "unsupported"
                and localization.evidence
                else ()
            )
            localizations[concept.concept_id] = {
                "support": localization.support,
                "rationale": localization.rationale,
                "evidence": [
                    _passage_payload(passage)
                    for passage in localization.evidence
                ],
                "queries": [
                    query.model_dump(mode="json") for query in queries
                ],
            }
            evidence_records.extend(
                _evidence_records(localization)
            )
        return StageProduct(
            kind="source_rescue",
            payload={"localizations": localizations},
            source_evidence=tuple(evidence_records),
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
        groups: dict[str, list[dict[str, Any]]] = {}
        for concept_id, localization in localizations.items():
            raw_queries = localization.get("queries", [])
            queries = [
                RescueQuery.model_validate(query)
                for query in raw_queries
            ]
            if not queries:
                groups[concept_id] = []
                continue
            candidates = await self.retrieval.retrieve_pass_2(
                ledger_by_id[concept_id],
                queries,
                _retrieval_scope(context),
            )
            groups[concept_id] = [
                _candidate_payload(candidate)
                for candidate in candidates
            ]
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

    async def _finalize_outcomes(
        self,
        context: StageContext,
    ) -> StageProduct:
        pass_1 = _judgment_payload(
            context,
            CurationStage.JUDGMENT_PASS_1,
        )
        pass_2 = _judgment_payload(
            context,
            CurationStage.JUDGMENT_PASS_2,
        )
        rescue_payload = _payload(context, CurationStage.RESCUE)
        localizations = cast(
            dict[str, dict[str, Any]],
            rescue_payload.get("localizations", {}),
        )
        outcomes: dict[str, str] = {}
        for concept in _ledger(context).concepts:
            first = CoverageJudgment.model_validate(
                pass_1[concept.concept_id]["judgment"]
            )
            if first.status == "covered":
                outcomes[concept.concept_id] = "covered_pass_1"
                continue
            raw_localization = localizations.get(concept.concept_id)
            if raw_localization is None:
                outcomes[concept.concept_id] = "unsupported"
                continue
            localization = _localization(
                concept,
                raw_localization,
            )
            raw_second = pass_2.get(concept.concept_id)
            second = (
                CoverageJudgment.model_validate(
                    raw_second["judgment"]
                )
                if raw_second is not None
                else None
            )
            outcomes[concept.concept_id] = RescueService.finalize(
                localization,
                second,
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
            localization = _localization(
                ledger_by_id[concept_id],
                localizations[concept_id],
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
        service = JudgmentService(
            self.structured,
            self.repository,
            self.companion,
            provider=_provider(context),
            model=context.job.model,
            prompt_version=context.job.judgment_rubric_version,
            prompt_text=prompt_text,
            prompt_hash=prompt_hash,
        )
        results: dict[str, dict[str, Any]] = {}
        projected: list[Candidate] = []
        usages: list[JudgmentResult] = []
        for concept_id, values in raw_groups.items():
            candidates = [
                _candidate_from_payload(value) for value in values
            ]
            result = await asyncio.to_thread(
                service.judge,
                ledger_by_id[concept_id],
                candidates,
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
            supporting = set(result.judgment.supporting_note_ids)
            projected.extend(
                _judged_candidate(
                    candidate,
                    result.judgment,
                    selected=candidate.note_id in supporting,
                )
                for candidate in candidates
            )
        merged = _merge_candidates(projected)
        return StageProduct(
            kind=kind,
            payload={
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
        identity = hashlib.sha256(
            (
                f"{localization.concept.concept_id}\0"
                f"{passage.passage_id}"
            ).encode()
        ).hexdigest()
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


def _proposal_payload(proposal: GapCardProposal) -> dict[str, Any]:
    return {
        "concept_id": proposal.concept_id,
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
