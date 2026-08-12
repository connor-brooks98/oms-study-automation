"""Permanent, failure-first reconstructions for the approved A0 history.

These tests deliberately use deterministic provider scripts.  The real
``111dfaf`` raw provider bodies were not retained, so the two partition cases
preserve the observed fail-closed behavior without pretending to replay them.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
import respx

from oms_hub.anki.card_centric import CardCentricClassifier, build_source_index
from oms_hub.anki.card_centric_contracts import (
    CardClassificationBatchOutput,
    CardConcept,
    CardConceptLedger,
    CardRecord,
    SemanticPreFilterResult,
    TagScopeResult,
)
from oms_hub.anki.domain import (
    CurationStage,
    PipelineContractVersion,
    ResolvedModelConfiguration,
    SourceKind,
)
from oms_hub.anki.provider_attempts import ProviderAttemptBinding, bind_provider_attempts
from oms_hub.anki.rehearsal.capsule import CapsuleIntegrityError
from oms_hub.anki.rehearsal.materialize import _resolve_logical_path
from oms_hub.anki.rehearsal.regressions import (
    historical_regression_catalog,
    historical_regression_ids,
    resolve_historical_regression_assertions,
)
from oms_hub.anki.sources import SourcePassage
from oms_hub.anki.stages import CurationServicesRunner, _card_residual_v2_semantic_audit
from oms_hub.llm.anthropic import AnthropicProvider
from oms_hub.llm.domain import GeneratedText, GenerationOptions, ProviderName, ThinkingMode
from oms_hub.llm.structured import StructuredTextService
from tests.anki.test_existing_artifact_import import _build_imported_bundle

_CANDIDATE_111DFAF_JOB = UUID("7502ac16-3792-4c69-85b6-02a4596e21a4")
_EXACT_111DFAF_S4B_NOTE_IDS = (
    1476661104838,
    1476661110050,
    1476669552837,
    1476669559297,
    1476669572157,
    1476669579159,
    1478915707022,
    1478915789517,
    1478973207060,
    1486519322563,
    1486522390591,
    1486522395177,
    1486522403872,
    1486522422740,
    1486522534230,
    1486522543509,
    1486522549639,
    1486522556268,
    1486522563815,
    1489975262057,
    1520730669566,
    1525899000205,
    1525899048575,
)
_GENERIC_PARTITION_NOTE_IDS = tuple(range(1, 24))
_ALL_CATALOG_IDS = frozenset(
    {"A0-H01", "A0-H02", "A0-H03", "A0-H04", "A0-H05", "A0-H06", "A0-H07", "A0-H08"}
)


class _StructuredScript:
    """Small deterministic generator that keeps the real structured boundary."""

    def __init__(self, responses: Sequence[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def generate_text(
        self,
        instruction: str,
        input_text: str,
        *,
        output_schema: dict[str, object],
        provider: ProviderName,
        model: str,
        options: GenerationOptions,
    ) -> GeneratedText:
        del output_schema, options
        if not self.responses:
            raise AssertionError("deterministic regression script is exhausted")
        self.calls.append(
            {
                "instruction": instruction,
                "input_text": input_text,
                "provider": provider,
                "model": model,
            }
        )
        return GeneratedText(
            text=json.dumps(self.responses.pop(0), sort_keys=True, separators=(",", ":")),
            provider=provider,
            model=model,
            request_id=f"reconstruction-{len(self.calls)}",
            input_tokens=1,
            output_tokens=1,
            cost_microusd=1,
        )


def _source_and_cards(
    note_ids: tuple[int, ...],
) -> tuple[object, tuple[CardRecord, ...], CardConceptLedger]:
    passage = SourcePassage.create(
        revision_id=111,
        lecture_id=1,
        artifact_id="111dfaf-reconstruction",
        source_kind=SourceKind.SLIDE,
        locator="slide:1",
        text="The preserved reconstruction has one grounded concept.",
        slide_number=1,
    )
    source = build_source_index(
        (passage,), snapshot_id="111dfaf-reconstruction", source_revision_hashes={111: "a" * 64}
    )
    cards = tuple(
        CardRecord(
            note_id=note_id,
            content_sha256=f"{note_id:064x}",
            text=f"reconstructed card {note_id}",
            extra="",
            tags=("rehearsal",),
            deck_names=("AnKing",),
        )
        for note_id in note_ids
    )
    ledger = CardConceptLedger(
        lecture_entity_count=1,
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="The preserved reconstruction has one grounded concept.",
                primary_entity="reconstruction",
                depth="deep",
                emphasis_flag=False,
                importance="high",
            ),
        ),
    )
    return source, cards, ledger


def _prompt_snapshot(prompt_id: str) -> dict[str, object]:
    from oms_hub.anki.prompts import AnkiPromptLibrary

    prompt = AnkiPromptLibrary().load(prompt_id)
    return {
        "id": prompt.metadata.id,
        "version": prompt.metadata.version,
        "prompt_hash": prompt.prompt_hash,
        "content": prompt.content,
        "metadata": prompt.metadata.model_dump(mode="json", by_alias=True),
    }


def _s4_context(source: object, cards: tuple[CardRecord, ...], ledger: CardConceptLedger) -> object:
    return SimpleNamespace(
        job=SimpleNamespace(
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
            resolved_model_config=ResolvedModelConfiguration.card_centric_v2_default(
                "openai", "gpt-5"
            ),
        ),
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    _prompt_snapshot("card-centric-fast-classifier"),
                    _prompt_snapshot("card-centric-classifier"),
                ]
            },
            CurationStage.SOURCE_INDEX: {
                "source_index": source.model_dump(mode="json"),
                "cards": [card.model_dump(mode="json") for card in cards],
            },
            CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
            CurationStage.CARD_PREFILTER: SemanticPreFilterResult(
                pre_filtered_note_ids=_EXACT_111DFAF_S4B_NOTE_IDS,
                pre_excluded_note_ids=(),
                threshold=0.5,
                similarity_stats={"min": 0.5, "max": 0.5, "mean": 0.5, "median": 0.5},
            ).model_dump(mode="json"),
            CurationStage.CARD_TAG_SCOPE: {
                "scope": TagScopeResult(
                    snapshot_id="111dfaf-reconstruction",
                    filters_sha256="b" * 64,
                    scoped_note_ids=_EXACT_111DFAF_S4B_NOTE_IDS,
                    unscoped_note_ids=(),
                ).model_dump(mode="json")
            },
        },
    )


def _semantic_call_identity(event: object) -> tuple[str, int | None, str, int, str, str]:
    """Return the scheduler-independent provider call identity.

    ``call_index`` is an opaque deterministic audit slot now, so these
    historical reconstructions intentionally identify calls by the frozen
    stage/batch/call-kind/subcall/request contract instead of a sequence.
    """
    attempt = event.event
    return (
        attempt.identity.stage.value,
        attempt.identity.batch_index,
        attempt.identity.kind,
        attempt.identity.subcall_ordinal,
        attempt.request_sha256,
        attempt.identity.batch_note_ids_sha256,
    )


def _events_for_semantic_call(
    events: list[object], identity: tuple[str, int | None, str, int, str, str]
) -> list[object]:
    return [event for event in events if _semantic_call_identity(event) == identity]


def test_catalog_is_complete_versioned_and_uses_only_declared_evidence_quality() -> None:
    catalog = historical_regression_catalog()
    failures = catalog["failures"]
    assert catalog["schema_version"] == 2
    assert isinstance(failures, list)
    assert {entry["id"] for entry in failures} == _ALL_CATALOG_IDS == historical_regression_ids()
    assert {entry["evidence_quality"] for entry in failures} == {
        "historical_artifact",
        "behavioral_reconstruction",
    }
    recorded = next(entry for entry in failures if entry["id"] == "A0-H07")
    assert recorded["identifiers"] == {
        "job_id": str(_CANDIDATE_111DFAF_JOB),
        "s4b_partition_degraded_note_count": 23,
        "s4b_partition_degraded_note_ids": list(_EXACT_111DFAF_S4B_NOTE_IDS),
        "raw_provider_response": "unavailable",
        "s6_exact_selected_batch": "native_pending_capsule_backed_semantic_reconstruction",
        "s6_raw_provider_response": "unavailable",
    }


def test_catalog_assertions_resolve_to_the_preserved_regression_tests() -> None:
    assert resolve_historical_regression_assertions() == {
        entry["executable_assertion"] for entry in historical_regression_catalog()["failures"]
    }


def test_existing_artifact_import_uses_supported_service_path(tmp_path) -> None:
    bundle = _build_imported_bundle(tmp_path, adopt_derived_pdf=False)
    try:
        assert bundle.import_id
        assert bundle.immutable_transcript_path.is_file()
        assert bundle.immutable_outline_path.is_file()
    finally:
        bundle.database.close()


def test_windows_path_materialization_rejects_unknown_root() -> None:
    root, relative = _resolve_logical_path(
        r"C:\\ProgramData\\OMS\\lecture.pdf", {"a0": r"c:\\programdata\\oms"}
    )
    assert (root, str(relative)) == ("a0", "lecture.pdf")
    with pytest.raises(CapsuleIntegrityError, match="outside registered"):
        _resolve_logical_path(r"D:\\escape.pdf", {"a0": r"C:\\ProgramData\\OMS"})


def test_derived_pdf_adoption_preserves_provenance(tmp_path) -> None:
    bundle = _build_imported_bundle(tmp_path, adopt_derived_pdf=True)
    try:
        from oms_hub.ingestion.repository import IngestionRepository

        revision = IngestionRepository(bundle.database).get_study_revision(bundle.slide_revision_id)
        assert revision.provenance_kind == "imported_derived"
        assert revision.import_id == bundle.import_id
        assert revision.immutable_derived_path is not None
    finally:
        bundle.database.close()


def test_s2_importance_depth_conflict_fails_validation() -> None:
    with pytest.raises(ValueError, match="importance conflicts"):
        CardConcept(
            concept_id="C01",
            canonical_statement="conflicting fixture",
            primary_entity="fixture",
            depth="deep",
            emphasis_flag=False,
            importance="medium",
        )


@respx.mock
def test_anthropic_unsupported_temperature_is_not_transported() -> None:
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            headers={"request-id": "reconstruction-temperature"},
            json={
                "id": "message-reconstruction",
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": "{}"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )
    )
    AnthropicProvider().generate_text(
        "Return JSON.",
        "fixture",
        api_key="fixture",
        model="claude-sonnet-5",
        output_schema={"type": "object"},
        options=GenerationOptions(thinking=ThinkingMode.DISABLED, temperature=0),
    )
    assert "temperature" not in json.loads(route.calls.last.request.content)


def test_semantic_blank_note_1629377933055_is_ineligible() -> None:
    cards = {
        1629377933055: CardRecord(
            note_id=1629377933055,
            content_sha256="c" * 64,
            text=" ",
            extra="\n",
            tags=(),
            deck_names=(),
        ),
        2: CardRecord(
            note_id=2,
            content_sha256="d" * 64,
            text="eligible text",
            extra="",
            tags=(),
            deck_names=(),
        ),
    }
    searchable, audit = _card_residual_v2_semantic_audit(cards)
    assert searchable == {2}
    assert audit["embedding_unavailable_blank_note_ids"] == [1629377933055]


def test_candidate_111dfaf_s4b_partition_reconstruction_degrades_all_23_to_s4c() -> None:
    """Behavioral reconstruction: no historical raw S4b provider body is claimed."""
    source, cards, ledger = _source_and_cards(_EXACT_111DFAF_S4B_NOTE_IDS)
    partial_fast = {
        "results": [
            {"note_id": note_id, "verdict": "NEEDS_REVIEW", "reason": "reconstructed partial batch"}
            for note_id in _EXACT_111DFAF_S4B_NOTE_IDS[:-1]
        ]
    }
    terminal_s4c = {
        "results": [
            {
                "note_id": note_id,
                "verdict": "NO",
                "primary_subject": "reconstruction",
                "reason": "terminal S4c reconstruction",
            }
            for note_id in _EXACT_111DFAF_S4B_NOTE_IDS
        ]
    }
    script = _StructuredScript((partial_fast, terminal_s4c))
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = StructuredTextService(script)
    runner.prompts = SimpleNamespace()
    context = _s4_context(source, cards, ledger)
    s4b_events: list[object] = []
    s4b_binding = ProviderAttemptBinding(
        job_id=_CANDIDATE_111DFAF_JOB,
        stage=CurationStage.CARD_FAST_CLASSIFY,
        stage_attempt=1,
        mode="canonical",
        recorder=s4b_events.append,
    )
    with bind_provider_attempts(s4b_binding):
        fast = asyncio.run(runner._card_fast_classify(context))
    context.prior_payloads[CurationStage.CARD_FAST_CLASSIFY] = fast.payload
    s4c_events: list[object] = []
    s4c_binding = ProviderAttemptBinding(
        job_id=_CANDIDATE_111DFAF_JOB,
        stage=CurationStage.CARD_CLASSIFY,
        stage_attempt=1,
        mode="canonical",
        recorder=s4c_events.append,
    )
    with bind_provider_attempts(s4c_binding):
        classified = asyncio.run(runner._card_classify(context))

    assert [row["note_id"] for row in fast.payload["fast_classifier"]["results"]] == list(
        _EXACT_111DFAF_S4B_NOTE_IDS
    )
    assert {row["verdict"] for row in fast.payload["fast_classifier"]["results"]} == {
        "NEEDS_REVIEW"
    }
    assert fast.payload["degraded_batches"] == [
        {
            "batch_index": 0,
            "note_ids": list(_EXACT_111DFAF_S4B_NOTE_IDS),
            "reason_code": "partition_mismatch",
        }
    ]
    assert fast.payload["degraded_note_count"] == 23
    s4c_input = json.loads(script.calls[1]["input_text"])
    assert [card["note_id"] for card in s4c_input["cards"]] == list(_EXACT_111DFAF_S4B_NOTE_IDS)
    assert [row["note_id"] for row in classified.payload["classifier"]["results"]] == list(
        _EXACT_111DFAF_S4B_NOTE_IDS
    )
    fast_identity = _semantic_call_identity(s4b_events[0])
    assert fast_identity[:4] == (CurationStage.CARD_FAST_CLASSIFY.value, 0, "primary", 0)
    assert len(fast_identity[4]) == len(fast_identity[5]) == 64
    fast_events = _events_for_semantic_call(s4b_events, fast_identity)
    assert [event.event.event for event in fast_events] == [
        "begun",
        "dispatched",
        "response_received",
        "accepted",
        "contract_failed",
    ]
    delta = fast_events[-1].event
    assert delta.identity.batch_note_ids == _EXACT_111DFAF_S4B_NOTE_IDS
    assert (
        delta.missing_note_ids,
        delta.extra_note_ids,
        delta.duplicate_note_ids,
    ) == ((_EXACT_111DFAF_S4B_NOTE_IDS[-1],), (), ())
    assert s4c_events[-1].event.event == "accepted"
    assert s4c_events[-1].event.identity.batch_note_ids == _EXACT_111DFAF_S4B_NOTE_IDS


def test_terminal_s6_partition_mismatch_fails_closed_with_attempt_delta() -> None:
    """Generic permanent regression; exact 111dfaf S6 batch remains capsule-pending."""
    source, cards, _ledger = _source_and_cards(_GENERIC_PARTITION_NOTE_IDS)
    partial = CardClassificationBatchOutput.model_validate(
        {
            "results": [
                {
                    "note_id": note_id,
                    "verdict": "NO",
                    "primary_subject": "reconstruction",
                    "reason": "reconstructed partial S6 batch",
                }
                for note_id in _GENERIC_PARTITION_NOTE_IDS[:-1]
            ]
        }
    ).model_dump(mode="json")
    script = _StructuredScript((partial, partial))
    classifier = CardCentricClassifier(
        StructuredTextService(script),
        batch_size=30,
        concurrency=1,
        retry_attempts=2,
        thinking_budget_tokens=1024,
        require_nonblank_reason=True,
    )
    events: list[object] = []
    binding = ProviderAttemptBinding(
        job_id=_CANDIDATE_111DFAF_JOB,
        stage=CurationStage.CARD_RESIDUAL,
        stage_attempt=1,
        mode="canonical",
        recorder=events.append,
    )
    successful_products: list[object] = []
    with (
        bind_provider_attempts(binding),
        pytest.raises(ValueError, match="does not exactly partition batch cards"),
    ):
        successful_products.append(
            asyncio.run(
                classifier.classify(
                    cards,
                    source_index=source,
                    concept_ids=("C01",),
                    provider=ProviderName.OPENAI,
                    model="gpt-5",
                )
            )
        )

    assert successful_products == []
    terminal_identities = [
        _semantic_call_identity(event)
        for event in events
        if event.event.event == "contract_failed"
    ]
    terminal_slots = [
        (identity[0], identity[1], identity[2], identity[3])
        for identity in terminal_identities
    ]
    assert terminal_slots == [
        (CurationStage.CARD_RESIDUAL.value, 0, "primary", 0),
        (CurationStage.CARD_RESIDUAL.value, 0, "repair", 0),
    ]
    assert terminal_identities[0][4] != terminal_identities[1][4]
    assert terminal_identities[0][5] == terminal_identities[1][5]
    for identity in terminal_identities:
        call_events = _events_for_semantic_call(events, identity)
        assert call_events[-1].event.event == "contract_failed"
        delta = call_events[-1].event
        assert delta.identity.batch_note_ids == _GENERIC_PARTITION_NOTE_IDS
        assert (
            delta.missing_note_ids,
            delta.extra_note_ids,
            delta.duplicate_note_ids,
        ) == ((23,), (), ())
