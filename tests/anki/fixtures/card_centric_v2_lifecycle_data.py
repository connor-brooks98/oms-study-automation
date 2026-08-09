"""Data and minimal service seams for the P4-A real-handler lifecycle tests."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

from oms_hub.anki.card_centric import build_snapshot_census, build_source_index
from oms_hub.anki.card_centric_contracts import CardConcept, CardConceptLedger, CardRecord
from oms_hub.anki.domain import (
    CurationStage,
    PipelineContractVersion,
    ResolvedModelConfiguration,
    SourceKind,
)
from oms_hub.anki.prompts import AnkiPromptLibrary
from oms_hub.anki.semantic.domain import PinnedCentroidSimilarityResult, SemanticHit
from oms_hub.anki.sources import SourcePassage


class LifecycleSemanticService:
    """Pinned S4a scores and S6 hits, deliberately independent of runner code."""

    async def pinned_similarity(
        self, queries: tuple[str, ...], *, note_ids: tuple[int, ...], expected_generation: str
    ) -> dict[int, float]:
        del queries
        assert expected_generation == "fixture-generation"
        return {note_id: 0.90 if note_id != 2 else 0.20 for note_id in note_ids}

    async def pinned_centroid_similarity(
        self,
        concept_terms: tuple[tuple[str, ...], ...],
        *,
        note_ids: tuple[int, ...],
        expected_generation: str,
    ) -> PinnedCentroidSimilarityResult:
        del concept_terms
        assert expected_generation == "fixture-generation"
        return PinnedCentroidSimilarityResult(
            scores={note_id: 0.90 if note_id != 2 else 0.20 for note_id in note_ids}
        )

    async def search(
        self,
        queries: tuple[str, ...],
        *,
        eligible_note_ids: set[int],
        limit: int,
        expected_generation: str = "fixture-generation",
    ) -> list[list[SemanticHit]]:
        del eligible_note_ids, limit
        assert expected_generation == "fixture-generation"
        # The first score is intentionally in the disputed [0.40, 0.50) band.
        return [[SemanticHit(note_id=2, score=0.45, content_hash="2" * 64)] for _ in queries]


class LifecycleRepository:
    def lecture_title(self, lecture_id: int) -> str:
        assert lecture_id == 12
        return "Heme synthesis"

    def card_centric_yes_rate_history(self, job_id: object) -> tuple[float, ...]:
        del job_id
        return ()


def lifecycle_source_payload(*, card_count: int = 10) -> dict[str, Any]:
    passages = (
        SourcePassage.create(
            revision_id=8,
            lecture_id=12,
            artifact_id="summary-8",
            source_kind=SourceKind.SUMMARY,
            locator="summary:outline",
            text="Heme synthesis starts in mitochondria and needs glycine.",
        ),
        SourcePassage.create(
            revision_id=9,
            lecture_id=12,
            artifact_id="transcript-9",
            source_kind=SourceKind.TRANSCRIPT,
            locator="transcript:1",
            text="ALA synthase joins glycine with succinyl-CoA in mitochondria.",
        ),
        SourcePassage.create(
            revision_id=7,
            lecture_id=12,
            artifact_id="slides-7",
            source_kind=SourceKind.SLIDE,
            locator="slide:1",
            text="Heme synthesis starts with glycine and succinyl-CoA in mitochondria.",
            slide_number=1,
        ),
    )
    source = build_source_index(
        passages,
        snapshot_id="fixture-snapshot",
        source_revision_hashes={7: "a" * 64, 8: "b" * 64, 9: "c" * 64},
    )
    cards = tuple(
        CardRecord(
            note_id=note_id,
            content_sha256=f"{note_id:x}".rjust(64, "0"),
            text=(
                "Heme synthesis begins in {{c1::mitochondria}}."
                if note_id == 1
                else f"Unrelated heme review card {note_id}: {{c1::detail {note_id}}}."
            ),
            extra="Fixture card.",
            tags=("#Heme",),
            deck_names=("Medical",),
        )
        for note_id in range(1, card_count + 1)
    )
    census = build_snapshot_census(
        cards,
        deck_allowlist=("Medical",),
        scope_tokens=("heme",),
        snapshot_id="fixture-census",
    )
    return {
        "source_index": source.model_dump(mode="json"),
        "cards": [card.model_dump(mode="json") for card in cards],
        "census": census.model_dump(mode="json"),
    }


def lifecycle_ledger() -> CardConceptLedger:
    return CardConceptLedger(
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="Heme synthesis begins in mitochondria.",
                primary_entity="Heme synthesis",
                aliases=("heme",),
                depth="deep",
                emphasis_flag=True,
                importance="high",
                fact_descriptions=("Heme synthesis begins in mitochondria.",),
                forbidden_cloze_targets_by_fact=((),),
            ),
            CardConcept(
                concept_id="C02",
                canonical_statement="ALA synthase joins glycine with succinyl-CoA.",
                primary_entity="ALA synthase",
                aliases=("glycine",),
                depth="medium",
                emphasis_flag=False,
                importance="medium",
                suggested_fact_count=3,
                fact_descriptions=(
                    "ALA synthase joins glycine with succinyl-CoA.",
                    "This first heme step occurs in mitochondria.",
                    "The substrate pair is glycine and succinyl-CoA.",
                ),
                forbidden_cloze_targets_by_fact=((), (), ()),
                is_mechanism=True,
            ),
        ),
        lecture_entity_count=2,
    )


def lifecycle_job() -> Any:
    return SimpleNamespace(
        id="fixture-job",
        lecture_id=12,
        provider="anthropic",
        model="fixture-model",
        pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
        resolved_model_config=ResolvedModelConfiguration.card_centric_v2_default(
            "anthropic", "fixture-model"
        ),
        semantic_generation="fixture-generation",
        tag_allowlist=("heme",),
        deck_allowlist=("Medical",),
        gap_prompt_version="gap-v2",
    )


def lifecycle_preflight() -> dict[str, Any]:
    library = AnkiPromptLibrary()
    prompts = library.load_many(
        (
            "card-centric-ledger-v2",
            "card-centric-fast-classifier",
            "card-centric-classifier",
            "card-centric-gap-v2",
        )
    )
    return {
        "prompt_snapshot": [
            {
                "id": prompt.metadata.id,
                "version": prompt.metadata.version,
                "prompt_hash": hashlib.sha256(prompt.content.encode()).hexdigest()[:12],
                "content": prompt.content,
                "path": str(prompt.path),
                "source_paths": [str(path) for path in prompt.source_paths],
                "metadata": prompt.metadata.model_dump(mode="json", by_alias=True),
            }
            for prompt in prompts.prompts
        ]
    }


def lifecycle_empty_a11_history() -> dict[str, Any]:
    """P1-compatible empty distinct-job A11 snapshot."""
    entries: list[dict[str, object]] = []
    return {
        "entries": entries,
        "snapshot_sha256": hashlib.sha256(
            json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def lifecycle_pinned_lecture() -> dict[str, Any]:
    """P1-compatible pinned lecture metadata for the deterministic lifecycle lecture."""
    metadata = {"exam_number": 1, "lecture_number": 1, "subject": "Heme", "topic": "Synthesis"}
    canonical_metadata = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    payload = {"lecture_id": 12, "title": "Heme Exam 1 Lecture 1: Synthesis", "metadata": metadata}
    return {
        "lecture_id": 12,
        "title": payload["title"],
        "metadata": {"canonical_json": canonical_metadata},
        "metadata_sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def payloads(
    *, source: dict[str, Any], preflight: dict[str, Any]
) -> dict[CurationStage, dict[str, Any]]:
    return {CurationStage.PREFLIGHT: preflight, CurationStage.SOURCE_INDEX: source}
