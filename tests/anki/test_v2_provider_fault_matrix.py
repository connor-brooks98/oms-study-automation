"""P4-B provider/semantic fault matrix for card_centric_v2."""

import asyncio
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from oms_hub.anki.card_centric import CardCentricClassifier, CardCentricValidationError
from oms_hub.anki.card_centric_contracts import (
    CardClassification,
    CardClassificationBatchOutput,
    CardConcept,
    CardConceptLedger,
    CardRecord,
    ClassifierResult,
    ClassifierTelemetry,
    FastCardClassification,
    FastClassificationResult,
    GeneratedCardResolution,
)
from oms_hub.anki.dedupe import DeduplicationService
from oms_hub.anki.domain import (
    CreateCurationJob,
    CurationStage,
    CurationState,
    PipelineContractVersion,
    SourceKind,
)
from oms_hub.anki.models import AnkiCurationJobModel
from oms_hub.anki.pipeline import (
    CurationPipeline,
    PinnedInputChanged,
    StageArtifactStore,
    StageContext,
    StageProduct,
)
from oms_hub.anki.repository import AnkiCurationRepository, InvalidCurationTransition
from oms_hub.anki.semantic.service import SemanticCoverageError
from oms_hub.anki.semantic.store import SemanticSnapshotError
from oms_hub.anki.semantic.voyage import VoyageEmbeddingError
from oms_hub.anki.sources import SourcePassage
from oms_hub.anki.stages import CurationServicesRunner
from oms_hub.anki.worker import AnkiCurationWorker, _is_retryable
from oms_hub.db import Database
from oms_hub.llm.domain import DiagnosticSource, GeneratedText, ProviderName
from oms_hub.models import LectureModel
from tests.anki.fixtures.card_centric_v2_faults import (
    RETRYABLE_PROVIDER_SOURCES,
    CountingOutageEmbeddingClient,
    FaultingEmbeddingClient,
    FaultingStructuredService,
    InvalidEmbeddingClient,
    malformed_structured_output,
    provider_fault,
)
from tests.anki.fixtures.card_centric_v2_lifecycle_data import lifecycle_pinned_lecture


def _source() -> tuple[object, CardRecord, CardConceptLedger]:
    from oms_hub.anki.card_centric import build_source_index

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
        (passage,), snapshot_id="snapshot-1", source_revision_hashes={7: "a" * 64}
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
    return source, card, ledger


@pytest.mark.parametrize(
    ("fault", "retryable"),
    [
        (TimeoutError("fixture timeout"), True),
        *((provider_fault(source), True) for source in RETRYABLE_PROVIDER_SOURCES),
        (malformed_structured_output(provider=ProviderName.OPENAI, model="fixture"), True),
        (provider_fault(DiagnosticSource.REQUEST), False),
    ],
    ids=("timeout", "network", "quota-rate-limit", "service", "malformed", "other"),
)
def test_s2_real_handler_propagates_provider_fault_to_worker(
    fault: Exception, retryable: bool
) -> None:
    """Real S2 provider exceptions retain their worker retryability mapping."""
    from oms_hub.anki.card_centric import build_source_index
    from oms_hub.anki.prompts import AnkiPromptLibrary

    summary = SourcePassage.create(
        revision_id=8,
        lecture_id=12,
        artifact_id="summary-8",
        source_kind=SourceKind.SUMMARY,
        locator="summary:1",
        text="Heme synthesis begins in mitochondria.",
    )
    source = build_source_index(
        (summary,), snapshot_id="snapshot-1", source_revision_hashes={8: "a" * 64}
    )
    prompt = AnkiPromptLibrary().load("card-centric-ledger-v2")
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = FaultingStructuredService(fault=fault)
    context = SimpleNamespace(
        job=SimpleNamespace(
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
            resolved_model_config=SimpleNamespace(
                ledger_s2=SimpleNamespace(provider="openai", model="fixture")
            ),
        ),
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": prompt.metadata.id,
                        "version": prompt.metadata.version,
                        "prompt_hash": prompt.prompt_hash,
                        "content": prompt.content,
                        "metadata": prompt.metadata.model_dump(mode="json", by_alias=True),
                    }
                ]
            },
            CurationStage.SOURCE_INDEX: {"source_index": source.model_dump(mode="json")},
        },
        # Direct handler tests have no pipeline lease/repository.  Preserve the
        # production S2 invocation (including bounded malformed-output repair)
        # while making its append-only attempt hook explicit and inert here.
        record_card_ledger_attempt=lambda _attempt: None,
    )

    with pytest.raises(type(fault)) as raised:
        asyncio.run(runner._card_ledger(context))

    assert raised.value is fault
    assert _is_retryable(raised.value) is retryable


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ((), "exactly partition"),
        (
            (
                CardClassification(
                    note_id=2,
                    verdict="NO",
                    primary_subject="fixture",
                    reason="invented identity",
                ),
            ),
            "exactly partition",
        ),
        (
            (
                CardClassification(
                    note_id=1,
                    verdict="NO",
                    primary_subject="fixture",
                    reason="first duplicate",
                ),
                CardClassification(
                    note_id=1,
                    verdict="NO",
                    primary_subject="fixture",
                    reason="second duplicate",
                ),
            ),
            "exactly partition",
        ),
        (
            (
                CardClassification(
                    note_id=1,
                    verdict="YES",
                    primary_subject="fixture",
                    reason="ungrounded",
                ),
            ),
            "ungrounded YES",
        ),
        (
            (
                CardClassification(
                    note_id=1,
                    verdict="NO",
                    primary_subject="fixture",
                    reason="invented concept",
                    covered_concept_ids=("C99",),
                ),
            ),
            "invented a concept ID",
        ),
        (
            (
                CardClassification(
                    note_id=1,
                    verdict="NO",
                    primary_subject="fixture",
                    reason="invented passage",
                    supporting_passage_ids=("P99",),
                ),
            ),
            "invented a supporting passage ID",
        ),
    ],
)
def test_s4c_s6_schema_valid_invalid_rows_block_before_eligibility(
    rows: tuple[CardClassification, ...], message: str
) -> None:
    source, card, _ledger = _source()
    classifier = CardCentricClassifier(
        SimpleNamespace(),
        instruction="fixture",
        capabilities=SimpleNamespace(prompt_prefix_caching=False),
    )

    with pytest.raises(CardCentricValidationError, match=message):
        classifier.validate_output(
            CardClassificationBatchOutput(results=rows),
            cards=(card,),
            source_index=source,
            concept_ids=("C01",),
        )


@pytest.mark.parametrize("stage", ("S4c", "S6"))
@pytest.mark.parametrize(
    ("fault", "retryable"),
    [
        (TimeoutError("fixture timeout"), True),
        *((provider_fault(source), True) for source in RETRYABLE_PROVIDER_SOURCES),
        (malformed_structured_output(provider=ProviderName.OPENAI, model="fixture"), True),
        (provider_fault(DiagnosticSource.REQUEST), False),
    ],
    ids=("timeout", "network", "quota-rate-limit", "service", "malformed", "other"),
)
def test_s4c_s6_classifier_adapter_propagates_provider_fault_to_worker(
    stage: str, fault: Exception, retryable: bool
) -> None:
    """Both thorough paths invoke the shared production classifier adapter."""
    source, card, _ledger = _source()
    classifier = CardCentricClassifier(
        FaultingStructuredService(fault=fault),
        instruction=f"fixture {stage}",
        capabilities=SimpleNamespace(prompt_prefix_caching=False),
    )

    with pytest.raises(type(fault)) as raised:
        asyncio.run(
            classifier.classify(
                (card,),
                source_index=source,
                concept_ids=("C01",),
                provider=ProviderName.OPENAI,
                model="fixture",
            )
        )

    assert raised.value is fault
    assert _is_retryable(raised.value) is retryable


@pytest.mark.parametrize(
    ("mode", "expected_reason", "retryable"),
    [
        ("malformed", "structured_output_invalid", True),
        ("partial", "partition_mismatch", None),
        ("extra", "partition_mismatch", None),
        ("ungrounded", "ungrounded_likely_yes", None),
        ("timeout", None, True),
        ("network", None, True),
        ("quota-rate-limit", None, True),
        ("service", None, True),
        ("other", None, False),
    ],
)
def test_s4b_optional_faults_degrade_the_complete_batch(
    tmp_path: Path, mode: str, expected_reason: str | None, retryable: bool | None
) -> None:
    """Malformed structured output degrades only the complete real S4b batch."""
    from oms_hub.anki.card_centric_contracts import SemanticPreFilterResult, TagScopeResult
    from oms_hub.anki.prompt_catalog import AnkiPromptCatalogService
    from oms_hub.anki.prompts import AnkiPromptLibrary

    source, card, ledger = _source()
    fast_prompt = AnkiPromptLibrary().load("card-centric-fast-classifier")
    normal = FastCardClassification(
        note_id=1,
        verdict="LIKELY_YES",
        grounded_concept_ids=("C01",),
        supporting_passage_ids=(source.passages[0].passage_id,),
        reason="grounded",
    )
    if mode == "malformed":
        service = FaultingStructuredService(
            fault=malformed_structured_output(provider=ProviderName.OPENAI, model="fixture")
        )
    elif mode in {"timeout", "network", "quota-rate-limit", "service", "other"}:
        fault = (
            TimeoutError("fixture timeout")
            if mode == "timeout"
            else provider_fault(
                {
                    "network": DiagnosticSource.NETWORK,
                    "quota-rate-limit": DiagnosticSource.QUOTA,
                    "service": DiagnosticSource.SERVICE,
                    "other": DiagnosticSource.REQUEST,
                }[mode]
            )
        )
        service = FaultingStructuredService(fault=fault)
    else:
        rows = (normal,)
        if mode == "partial":
            rows = ()
        elif mode == "extra":
            rows = (normal, normal.model_copy(update={"note_id": 2}))
        elif mode == "ungrounded":
            rows = (
                normal.model_copy(
                    update={"grounded_concept_ids": (), "supporting_passage_ids": ()}
                ),
            )
        service = FaultingStructuredService(value=FastClassificationResult(results=rows))
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = service
    runner.prompts = AnkiPromptCatalogService(bundled_directory=tmp_path)
    context = SimpleNamespace(
        job=SimpleNamespace(
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
            resolved_model_config=SimpleNamespace(
                fast_classify_s4b=SimpleNamespace(provider="openai", model="fixture"),
                canonical_document=lambda: {},
            ),
        ),
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": fast_prompt.metadata.id,
                        "version": fast_prompt.metadata.version,
                        "prompt_hash": fast_prompt.prompt_hash,
                        "content": fast_prompt.content,
                        "metadata": fast_prompt.metadata.model_dump(mode="json", by_alias=True),
                    }
                ]
            },
            CurationStage.SOURCE_INDEX: {
                "source_index": source.model_dump(mode="json"),
                "cards": [card.model_dump(mode="json")],
            },
            CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
            CurationStage.CARD_PREFILTER: SemanticPreFilterResult(
                pre_filtered_note_ids=(1,),
                pre_excluded_note_ids=(),
                threshold=0.5,
                similarity_stats={"min": 0.5, "max": 0.5, "mean": 0.5, "median": 0.5},
            ).model_dump(mode="json"),
            CurationStage.CARD_TAG_SCOPE: {
                "scope": TagScopeResult(
                    snapshot_id="snapshot-1",
                    filters_sha256="c" * 64,
                    scoped_note_ids=(1,),
                    unscoped_note_ids=(),
                ).model_dump(mode="json")
            },
        },
    )

    if expected_reason is None and retryable is not None:
        with pytest.raises(type(service.fault)) as raised:
            asyncio.run(runner._card_fast_classify(context))
        assert raised.value is service.fault
        assert _is_retryable(raised.value) is retryable
        return

    product = asyncio.run(runner._card_fast_classify(context))

    assert product.payload["degraded_batches"] == [
        {"batch_index": 0, "note_ids": [1], "reason_code": expected_reason}
    ]
    assert product.payload["fast_classifier"]["results"][0]["verdict"] == "NEEDS_REVIEW"


def test_l2_s4a_to_s4b_rejects_disjoint_but_incomplete_partition() -> None:
    """L-2: S4b rejects a disjoint S4a partition which omits a scoped candidate."""
    from oms_hub.anki.card_centric_contracts import SemanticPreFilterResult, TagScopeResult

    source, card, ledger = _source()
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    context = SimpleNamespace(
        job=SimpleNamespace(
            resolved_model_config=SimpleNamespace(fast_classify_s4b=None),
        ),
        prior_payloads={
            CurationStage.SOURCE_INDEX: {
                "source_index": source.model_dump(mode="json"),
                "cards": [card.model_dump(mode="json")],
            },
            CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
            CurationStage.CARD_PREFILTER: SemanticPreFilterResult(
                pre_filtered_note_ids=(),
                pre_excluded_note_ids=(),
                threshold=0.5,
                similarity_stats={"min": 0.0, "max": 0.0, "mean": 0.0, "median": 0.0},
            ).model_dump(mode="json"),
            CurationStage.CARD_TAG_SCOPE: {
                "scope": TagScopeResult(
                    snapshot_id="snapshot-1",
                    filters_sha256="c" * 64,
                    scoped_note_ids=(1,),
                    unscoped_note_ids=(),
                ).model_dump(mode="json")
            },
        },
    )

    with pytest.raises(PinnedInputChanged, match="does not partition scoped notes"):
        asyncio.run(runner._card_fast_classify(context))


def _s4a_context(
    *, semantic: object, ledger: CardConceptLedger | None = None
) -> tuple[CurationServicesRunner, SimpleNamespace]:
    """Build the smallest real S4a context with one scoped pinned note."""
    from oms_hub.anki.card_centric_contracts import TagScopeResult

    source, card, default_ledger = _source()
    ledger = ledger or default_ledger
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.semantic = semantic
    return runner, SimpleNamespace(
        job=SimpleNamespace(
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
            semantic_generation="generation-fixture",
        ),
        prior_payloads={
            CurationStage.SOURCE_INDEX: {
                "source_index": source.model_dump(mode="json"),
                "cards": [card.model_dump(mode="json")],
            },
            CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
            CurationStage.CARD_TAG_SCOPE: {
                "scope": TagScopeResult(
                    snapshot_id="snapshot-1",
                    filters_sha256="d" * 64,
                    scoped_note_ids=(1,),
                    unscoped_note_ids=(),
                ).model_dump(mode="json")
            },
        },
    )


@pytest.mark.parametrize(
    ("fault", "retryable"),
    [
        (PinnedInputChanged("pinned precondition"), False),
        (SemanticCoverageError("scoped note is unavailable"), False),
        (VoyageEmbeddingError("embedding provider unavailable"), True),
        (SemanticSnapshotError("snapshot integrity failure"), True),
    ],
    ids=("pinned-precondition", "coverage", "voyage", "snapshot-integrity"),
)
def test_m6_d8_s4a_classifies_embedding_and_snapshot_failures_at_worker_boundary(
    fault: Exception, retryable: bool
) -> None:
    """M-6/D8: S4a preserves failure identity so worker retry policy distinguishes it."""

    class FaultingSemantic:
        async def pinned_similarity(self, *_args: object, **_kwargs: object) -> dict[int, float]:
            raise fault

        async def pinned_centroid_similarity(self, *_args: object, **_kwargs: object) -> object:
            raise fault

    runner, context = _s4a_context(semantic=FaultingSemantic())
    with pytest.raises(type(fault)) as raised:
        asyncio.run(runner._card_prefilter(context))

    assert raised.value is fault
    assert _is_retryable(raised.value) is retryable


def test_expected_red_p2_m6_d8_s4a_marks_only_unavailable_cards_for_s4b_pass_through() -> None:
    """P2 M-6/D8: a per-card unavailable vector must pass through while intact notes continue."""

    class PartiallyUnavailableSemantic:
        async def pinned_similarity(self, *_args: object, **_kwargs: object) -> dict[int, float]:
            raise VoyageEmbeddingError("note 1 embedding unavailable")

        async def pinned_centroid_similarity(self, *_args: object, **_kwargs: object) -> object:
            return SimpleNamespace(scores={}, unavailable_note_ids=(1,))

    runner, context = _s4a_context(semantic=PartiallyUnavailableSemantic())
    product = asyncio.run(runner._card_prefilter(context))

    assert product.payload["pre_filtered_note_ids"] == [1], (
        "P2 M-6/D8: S4a must preserve the unavailable card as an explicit S4b "
        "pass-through disposition while allowing intact scoped cards to continue"
    )
    assert product.payload["embedding_unavailable_note_ids"] == [1]


def test_expected_red_p2_m14_d21_s4a_uses_normalized_multivector_centroid_query_shape() -> None:
    """P2 M-14/D21: S4a must send primary entity and aliases as normalized centroid vectors."""

    from oms_hub.anki.semantic.service import normalize_semantic_text

    class RecordingSemantic:
        def __init__(self) -> None:
            self.queries: tuple[str, ...] = ()
            self.concept_terms: tuple[tuple[str, ...], ...] = ()

        async def pinned_similarity(
            self, queries: tuple[str, ...], **_kwargs: object
        ) -> dict[int, float]:
            self.queries = queries
            return {1: 0.90}

        async def pinned_centroid_similarity(
            self, concept_terms: tuple[tuple[str, ...], ...], **_kwargs: object
        ) -> object:
            self.concept_terms = concept_terms
            return SimpleNamespace(scores={1: 0.90}, unavailable_note_ids=())

    semantic = RecordingSemantic()
    _source_index, _card, base_ledger = _source()
    centroid_ledger = base_ledger.model_copy(
        update={"concepts": (base_ledger.concepts[0].model_copy(update={"aliases": ("mito",)}),)}
    )
    runner, context = _s4a_context(semantic=semantic, ledger=centroid_ledger)
    product = asyncio.run(runner._card_prefilter(context))

    assert product.payload["pre_filtered_note_ids"] == [1]
    expected_components = tuple(
        normalize_semantic_text(value) for value in ("heme synthesis", "mito")
    )
    assert semantic.concept_terms == (expected_components,), (
        "P2 M-14/D21: S4a must submit normalized primary-entity and alias vectors for "
        "deterministic centroid scoring, not one concatenated-text query"
    )


def test_expected_red_p3_h10_exhausted_semantic_outage_requires_manual_review(
    tmp_path: Path,
) -> None:
    """P3 H-10: worker backoff/exhaustion must block/manual-review semantic dedupe."""
    outage = VoyageEmbeddingError("fixture outage after retries")
    outage_client = CountingOutageEmbeddingClient(outage)

    class SemanticDedupeRunner:
        """Pipeline adapter which invokes the owning production S8 handler."""

        def __init__(self) -> None:
            source, card, ledger = _source()
            self.runner = CurationServicesRunner.__new__(CurationServicesRunner)
            self.runner.embedder = outage_client
            empty = ClassifierResult(
                results=(
                    CardClassification(
                        note_id=card.note_id,
                        verdict="YES",
                        primary_subject="fixture",
                        reason="Eligible existing fixture.",
                        covered_concept_ids=("C01",),
                        supporting_passage_ids=(source.passages[0].passage_id,),
                    ),
                ),
                telemetry=ClassifierTelemetry(
                    batch_count=0,
                    cache_prefix_sha256="a" * 64,
                    cache_mode="ordinary_prefix",
                    provider="openai",
                    model="fixture",
                    request_ids=(),
                    batches=(),
                ),
            )
            self.prior_payloads = {
                CurationStage.SOURCE_INDEX: {
                    "source_index": source.model_dump(mode="json"),
                    "cards": [card.model_dump(mode="json")],
                },
                CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
                CurationStage.CARD_CLASSIFY: {"classifier": empty.model_dump(mode="json")},
                CurationStage.CARD_RESIDUAL: {"classifier": None},
                CurationStage.CARD_FAST_CLASSIFY: {
                    "fast_classifier": FastClassificationResult(results=()).model_dump(mode="json"),
                    "fallback_note_ids": [],
                },
                CurationStage.CARD_GAP_FILL: {
                    "resolutions": [
                        GeneratedCardResolution(
                            card_id="semantic-outage",
                            concept_id="C01",
                            fact_id="C01-M1",
                            text="Generated {{c1::outage}}.",
                            source_passage_ids=(source.passages[0].passage_id,),
                            evidence_ids=("E-outage",),
                        ).model_dump(mode="json")
                    ]
                },
            }

        async def run(self, context: StageContext) -> StageProduct:
            assert context.stage is CurationStage.DEDUPE
            dedupe_context = StageContext(
                job=context.job,
                stage=CurationStage.DEDUPE,
                input_sha256=context.input_sha256,
                prior_artifacts=(),
                prior_payloads=self.prior_payloads,
            )
            return await self.runner._card_dedupe(dedupe_context)

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
                    lcl_prompt_version="ledger",
                    judgment_rubric_version="rubric",
                    gap_prompt_version="gap",
                    provider="openai",
                    model="fixture",
                    pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
                )
            )
            with database.session() as session:
                stored = session.get(AnkiCurationJobModel, str(job.id))
                assert stored is not None
                stored.state = CurationState.CARD_DEDUPING.value
            current = [datetime(2026, 8, 8, tzinfo=UTC)]
            worker = AnkiCurationWorker(
                repository,
                CurationPipeline(
                    repository, StageArtifactStore(tmp_path / "artifacts"), SemanticDedupeRunner()
                ),
                worker_id="p4-h10",
                lease_seconds=30,
                poll_seconds=0.01,
                max_stage_attempts=3,
                now=lambda: current[0],
            )

            for expected_attempt in (1, 2):
                assert await worker.run_once()
                stage = repository.get_stage(job.id, CurationStage.DEDUPE)
                assert stage is not None and stage.attempt_count == expected_attempt
                deferred = repository.require_job(job.id)
                assert deferred.state is CurationState.CARD_DEDUPING
                assert (
                    deferred.available_at
                    == (
                        current[0] + timedelta(seconds=5 * (2 ** (expected_attempt - 1)))
                    ).isoformat()
                )
                current[0] += timedelta(seconds=10)

            assert await worker.run_once()
            stage = repository.get_stage(job.id, CurationStage.DEDUPE)
            assert stage is not None and stage.attempt_count == 3
            assert outage_client.calls == 3
            held = repository.require_job(job.id)
            assert held.state is CurationState.READY_FOR_REVIEW, (
                "P3 H-10: exhausted semantic dedupe must reach an approved manual-review/block "
                "outcome instead of failing the worker stage"
            )
            assert held.error is not None and held.error.startswith(
                "Semantic dedupe retry required: "
            )
            with pytest.raises(InvalidCurationTransition):
                repository.hold_semantic_dedupe_for_review(
                    job.id,
                    "stale-worker",
                    "stale semantic outage",
                    now=current[0],
                )
            resumed = repository.retry_job(job.id)
            assert resumed.state is CurationState.CARD_DEDUPING
            assert resumed.error is None
        finally:
            database.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("fault", "retryable"),
    [
        (TimeoutError("fixture timeout"), True),
        (VoyageEmbeddingError("fixture service outage"), True),
        (provider_fault(DiagnosticSource.QUOTA), True),
        (provider_fault(DiagnosticSource.SERVICE), True),
        (provider_fault(DiagnosticSource.REQUEST), False),
    ],
    ids=("timeout", "voyage-service", "quota-rate-limit", "service", "other"),
)
def test_s8_dedupe_adapter_propagates_semantic_provider_fault_to_worker(
    fault: Exception, retryable: bool
) -> None:
    proposal = _proposal("C01", "proposed")
    comparison = _proposal("C02", "existing")
    deduper = DeduplicationService(FaultingEmbeddingClient(fault))

    with pytest.raises(type(fault)) as raised:
        asyncio.run(deduper.classify(proposal, (), (proposal, comparison)))

    assert raised.value is fault
    assert _is_retryable(raised.value) is retryable


@pytest.mark.parametrize(
    ("fault", "retryable"),
    [
        (TimeoutError("fixture timeout"), True),
        *((provider_fault(source), True) for source in RETRYABLE_PROVIDER_SOURCES),
        (malformed_structured_output(provider=ProviderName.OPENAI, model="fixture"), True),
        (provider_fault(DiagnosticSource.REQUEST), False),
    ],
    ids=("timeout", "network", "quota-rate-limit", "service", "malformed", "other"),
)
def test_s7_real_handler_propagates_provider_fault_to_worker(
    fault: Exception, retryable: bool
) -> None:
    """S7 invokes its real per-concept provider boundary before classification."""
    from oms_hub.anki.prompts import AnkiPromptLibrary

    source, _card, ledger = _source()
    prompt = AnkiPromptLibrary().load("card-centric-gap-v2")
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
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = FaultingStructuredService(fault=fault)
    runner.repository = SimpleNamespace(lecture_title=lambda _lecture_id: "Heme synthesis")
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
                        "id": prompt.metadata.id,
                        "version": prompt.metadata.version,
                        "prompt_hash": prompt.prompt_hash,
                        "content": prompt.content,
                        "metadata": prompt.metadata.model_dump(mode="json", by_alias=True),
                    }
                ]
            },
            CurationStage.SOURCE_INDEX: {"source_index": source.model_dump(mode="json")},
            CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
            CurationStage.CARD_COVERAGE: {
                "coverage": {"C01": {"status": "uncovered", "evidence": []}}
            },
            CurationStage.CARD_CLASSIFY: {"classifier": empty.model_dump(mode="json")},
            CurationStage.CARD_RESIDUAL: {"classifier": None},
            CurationStage.CARD_FAST_CLASSIFY: {
                "fast_classifier": FastClassificationResult(results=()).model_dump(mode="json"),
                "fallback_note_ids": [],
            },
        },
        replay_inputs={"pinned_lecture": lifecycle_pinned_lecture()},
        replay_inputs_sha256="a" * 64,
    )

    with pytest.raises(type(fault)) as raised:
        asyncio.run(runner._card_gap_fill(context))

    assert raised.value is fault
    assert _is_retryable(raised.value) is retryable


@pytest.mark.parametrize(
    "matrix",
    [
        ((1.0, 0.0),),
        ((float("nan"), 0.0), (1.0, 0.0)),
        ((0.0, 0.0), (1.0, 0.0)),
    ],
)
def test_s8_invalid_vectors_are_integrity_failures(matrix: tuple[tuple[float, ...], ...]) -> None:
    deduper = DeduplicationService(InvalidEmbeddingClient(matrix))
    proposal = _proposal("C01", "proposed")
    comparison = _proposal("C02", "existing")

    with pytest.raises(ValueError, match="deduplication embeddings"):
        asyncio.run(deduper.classify(proposal, (), (proposal, comparison)))


def _proposal(concept_id: str, text: str):
    from oms_hub.anki.gaps import GapCardProposal

    return GapCardProposal(
        concept_id=concept_id,
        note_type="Cloze",
        fields={"Text": text, "Extra": ""},
        source_refs=(),
        evidence_ids=("E01",),
        initial_tags=(),
        provider=ProviderName.OPENAI,
        model="fixture",
        prompt_version="fixture",
        confidence=1.0,
        content_hash="a" * 64,
        provenance={},
    )


def test_expected_red_m12_s4c_resolved_classifier_batches_exactly_thirty_cards() -> None:
    """P2 M-12/D17: real S4c uses its pinned route in exactly 30-card provider batches."""

    class RecordingGenerator:
        def __init__(self) -> None:
            self.batches: list[tuple[int, ...]] = []

        def generate_text(self, _instruction: str, input_text: str, **kwargs: object) -> object:
            cards = json.loads(input_text)["cards"]
            note_ids = tuple(item["note_id"] for item in cards)
            self.batches.append(note_ids)
            value = CardClassificationBatchOutput(
                results=tuple(
                    CardClassification(
                        note_id=note_id,
                        verdict="NO",
                        primary_subject="fixture",
                        reason="Not relevant to the pinned lecture.",
                    )
                    for note_id in note_ids
                )
            )
            return GeneratedText(
                text=value.model_dump_json(),
                provider=kwargs["provider"],  # type: ignore[index]
                model=kwargs["model"],  # type: ignore[index]
                request_id=f"s4c-{len(self.batches)}",
                input_tokens=1,
                output_tokens=1,
                cost_microusd=1,
            )

    from oms_hub.anki.card_centric_contracts import SemanticPreFilterResult, TagScopeResult
    from oms_hub.anki.prompt_catalog import AnkiPromptCatalogService
    from tests.anki.fixtures.card_centric_v2_lifecycle_data import (
        lifecycle_job,
        lifecycle_ledger,
        lifecycle_preflight,
        lifecycle_source_payload,
        payloads,
    )

    source = lifecycle_source_payload(card_count=31)
    generator = RecordingGenerator()
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    from oms_hub.llm.structured import StructuredTextService

    runner.structured = StructuredTextService(generator)
    runner.prompts = AnkiPromptCatalogService()
    job = lifecycle_job()
    preflight = lifecycle_preflight()
    context = SimpleNamespace(
        job=job,
        prior_payloads={
            **payloads(source=source, preflight=preflight),
            CurationStage.CARD_LEDGER: {"ledger": lifecycle_ledger().model_dump(mode="json")},
            CurationStage.CARD_TAG_SCOPE: {
                "scope": TagScopeResult(
                    snapshot_id="fixture-census",
                    filters_sha256="a" * 64,
                    scoped_note_ids=tuple(range(1, 32)),
                    unscoped_note_ids=(),
                ).model_dump(mode="json")
            },
            CurationStage.CARD_PREFILTER: SemanticPreFilterResult(
                pre_filtered_note_ids=tuple(range(1, 32)),
                pre_excluded_note_ids=(),
                threshold=0.55,
                similarity_stats={"min": 1.0, "max": 1.0, "mean": 1.0, "median": 1.0},
            ).model_dump(mode="json"),
            CurationStage.CARD_FAST_CLASSIFY: {
                "fast_classifier": FastClassificationResult(
                    results=tuple(
                        FastCardClassification(
                            note_id=index, verdict="NEEDS_REVIEW", reason="fixture"
                        )
                        for index in range(1, 32)
                    )
                ).model_dump(mode="json"),
                "fallback_note_ids": [],
            },
        },
    )

    product = asyncio.run(runner._card_classify(context))

    assert product.payload["model_config"] == job.resolved_model_config.canonical_document()
    assert (
        product.payload["classifier"]["telemetry"]["model"]
        == job.resolved_model_config.classify_s4.model
    )
    assert [len(batch) for batch in generator.batches] == [30, 1]


def test_expected_red_m16_s4b_uses_bounded_concurrency_and_deterministic_aggregation() -> None:
    """P2 M-16/D28: S4b uses 60-card batches concurrently and emits note-ID ordered output."""
    from oms_hub.anki.card_centric_contracts import SemanticPreFilterResult, TagScopeResult
    from tests.anki.fixtures.card_centric_v2_lifecycle_data import (
        lifecycle_job,
        lifecycle_ledger,
        lifecycle_preflight,
        lifecycle_source_payload,
        payloads,
    )

    class DelayedFastService:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.completed: list[int] = []
            self.batch_sizes: list[int] = []
            self._lock = threading.Lock()

        def generate_json(self, _instruction: str, input_text: str, **kwargs: object) -> object:
            from oms_hub.llm.structured import StructuredJSONResult

            cards = json.loads(input_text)["cards"]
            batch_index = (cards[0]["note_id"] - 1) // 60
            self.batch_sizes.append(len(cards))
            with self._lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            # The first batch is deliberately slower, so concurrent completion is out of order.
            if batch_index == 0:
                time.sleep(0.03)
            value = FastClassificationResult(
                results=tuple(
                    FastCardClassification(
                        note_id=item["note_id"], verdict="NEEDS_REVIEW", reason="fixture"
                    )
                    for item in cards
                )
            )
            with self._lock:
                self.completed.append(batch_index)
                self.active -= 1
            return StructuredJSONResult(
                value=value,
                raw_text=value.model_dump_json(),
                provider=kwargs["provider"],  # type: ignore[index]
                model=kwargs["model"],  # type: ignore[index]
                request_id=f"s4b-{batch_index}",
                input_tokens=1,
                output_tokens=1,
                cost_microusd=1,
            )

    source = lifecycle_source_payload(card_count=121)
    service = DelayedFastService()
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = service
    resolved = lifecycle_job().resolved_model_config
    configured_bound = 2
    job_values = dict(lifecycle_job().__dict__)
    job_values["resolved_model_config"] = SimpleNamespace(
        fast_classify_s4b=resolved.fast_classify_s4b,
        fast_classify_concurrency=configured_bound,
        canonical_document=resolved.canonical_document,
    )
    context = SimpleNamespace(
        job=SimpleNamespace(
            **job_values,
        ),
        prior_payloads={
            **payloads(source=source, preflight=lifecycle_preflight()),
            CurationStage.CARD_LEDGER: {"ledger": lifecycle_ledger().model_dump(mode="json")},
            CurationStage.CARD_TAG_SCOPE: {
                "scope": TagScopeResult(
                    snapshot_id="fixture-census",
                    filters_sha256="a" * 64,
                    scoped_note_ids=tuple(range(1, 122)),
                    unscoped_note_ids=(),
                ).model_dump(mode="json")
            },
            CurationStage.CARD_PREFILTER: SemanticPreFilterResult(
                pre_filtered_note_ids=tuple(range(1, 122)),
                pre_excluded_note_ids=(),
                threshold=0.55,
                similarity_stats={"min": 1.0, "max": 1.0, "mean": 1.0, "median": 1.0},
            ).model_dump(mode="json"),
        },
    )

    product = asyncio.run(runner._card_fast_classify(context))

    assert service.batch_sizes == [60, 60, 1]
    assert service.max_active > 1
    assert service.max_active <= context.job.resolved_model_config.fast_classify_concurrency
    assert service.completed[0] != 0
    assert [item["note_id"] for item in product.payload["fast_classifier"]["results"]] == list(
        range(1, 122)
    )
