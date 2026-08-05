import asyncio
import json

import pytest

from oms_hub.anki.card_centric import (
    CardCentricClassifier,
    CardCentricValidationError,
    build_snapshot_census,
    build_source_index,
    scope_cards,
)
from oms_hub.anki.card_centric_contracts import (
    CardClassification,
    CardClassificationBatchOutput,
    CardRecord,
)
from oms_hub.anki.domain import SourceKind
from oms_hub.anki.sources import SourcePassage
from oms_hub.llm.domain import GeneratedText, ProviderName
from oms_hub.llm.structured import StructuredTextService


def _passage(kind: SourceKind, locator: str, text: str) -> SourcePassage:
    return SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="upload-7",
        source_kind=kind,
        locator=locator,
        text=text,
        slide_number=1 if kind is SourceKind.SLIDE else None,
    )


def _card(note_id: int, tags: tuple[str, ...] = ("#AK_Step::Heme",)) -> CardRecord:
    return CardRecord(
        note_id=note_id,
        content_sha256=f"{note_id:064x}",
        text=f"Card {note_id}",
        extra="",
        tags=tags,
        deck_names=("AnKing",),
    )


def test_source_index_orders_summary_transcript_slides_and_hashes_stably() -> None:
    slides = _passage(SourceKind.SLIDE, "slide:1", "slide evidence")
    transcript = _passage(SourceKind.TRANSCRIPT, "transcript:1", "spoken evidence")
    summary = SourcePassage.create(
        revision_id=9,
        lecture_id=12,
        artifact_id="outline:9",
        source_kind=SourceKind.SUMMARY,
        locator="summary:core:1",
        text="summary evidence",
        source_id="SUM:12:CORE:01",
        summary_section="core",
    )

    first = build_source_index(
        [slides, summary, transcript],
        snapshot_id="snapshot-1",
        source_revision_hashes={7: "a" * 64},
    )
    second = build_source_index(
        [transcript, slides, summary],
        snapshot_id="snapshot-1",
        source_revision_hashes={7: "a" * 64},
    )

    assert [passage.authority for passage in first.passages] == [
        "summary",
        "transcript",
        "slide",
    ]
    assert first.prefix == second.prefix
    assert first.source_sha256 == second.source_sha256
    assert "SUM:12:CORE:01" in first.prefix


def test_census_accounts_for_every_note_and_refuses_unsafe_untagged_rate() -> None:
    census = build_snapshot_census(
        [_card(1), _card(2, ()), _card(3, ("#AK_Step::Heme",),)],
        deck_allowlist=("AnKing",),
        scope_tokens=("heme",),
    )

    assert census.denominator_count == 3
    assert census.tagged_count == 2
    assert census.untagged_count == 1
    assert census.excluded_count == 0
    assert set(census.mapping) == {1, 2, 3}
    assert census.trust.decision == "blocked"
    assert "untagged" in census.trust.reason


def test_tag_scope_is_a_complete_deterministic_partition() -> None:
    cards = [_card(3), _card(1), _card(2, ())]
    census = build_snapshot_census(
        cards,
        deck_allowlist=("AnKing",),
        scope_tokens=("heme",),
        untagged_safe_rate=0.5,
    )
    scoped = scope_cards(cards, census=census, scope_tokens=("heme",))

    assert scoped.scoped_note_ids == (1, 3)
    assert scoped.unscoped_note_ids == (2,)
    assert set(scoped.scoped_note_ids) | set(scoped.unscoped_note_ids) == {1, 2, 3}


def test_classifier_rejects_invented_ids_ungrounded_yes_and_partial_batch() -> None:
    source = build_source_index(
        [_passage(SourceKind.SLIDE, "slide:1", "evidence")],
        snapshot_id="snapshot-1",
        source_revision_hashes={7: "a" * 64},
    )
    cards = (_card(1), _card(2))
    classifier = CardCentricClassifier(StructuredTextService(_Generator([])))

    for output in (
        CardClassificationBatchOutput(
            results=(
                CardClassification(note_id=9, verdict="NO", primary_subject="x"),
            )
        ),
        CardClassificationBatchOutput(
            results=(
                CardClassification(note_id=1, verdict="YES", primary_subject="x"),
                CardClassification(note_id=2, verdict="NO", primary_subject="x"),
            )
        ),
        CardClassificationBatchOutput(
            results=(CardClassification(note_id=1, verdict="NO", primary_subject="x"),)
        ),
    ):
        with pytest.raises(CardCentricValidationError):
            classifier.validate_output(
                output,
                cards=cards,
                source_index=source,
                concept_ids=(),
            )


def test_classifier_uses_cached_prefix_and_restores_parallel_batch_order() -> None:
    source = build_source_index(
        [_passage(SourceKind.SLIDE, "slide:1", "evidence")],
        snapshot_id="snapshot-1",
        source_revision_hashes={7: "a" * 64},
    )
    generator = _Generator([])
    classifier = CardCentricClassifier(
        StructuredTextService(generator), batch_size=1, concurrency=2
    )

    result = asyncio.run(
        classifier.classify(
            [_card(3), _card(1), _card(2)],
            source_index=source,
            concept_ids=(),
            provider=ProviderName.ANTHROPIC,
            model="claude-haiku",
        )
    )

    assert [item.note_id for item in result.results] == [1, 2, 3]
    assert all(options.cacheable_source_prefix == source.prefix for options in generator.options)
    assert result.telemetry.batch_count == 3
    assert [batch.note_ids for batch in result.telemetry.batches] == [(3,), (1,), (2,)]


class _Generator:
    def __init__(self, outputs: list[CardClassificationBatchOutput]) -> None:
        self.outputs = list(outputs)
        self.options = []

    def generate_text(self, instruction, input_text, *, output_schema, provider, model, options):
        del instruction, output_schema, provider, model
        self.options.append(options)
        output = (
            self.outputs.pop(0)
            if self.outputs
            else CardClassificationBatchOutput(
                results=tuple(
                    CardClassification(note_id=item["note_id"], verdict="NO", primary_subject="x")
                    for item in json.loads(input_text)["cards"]
                )
            )
        )
        return GeneratedText(
            text=output.model_dump_json(),
            provider=ProviderName.ANTHROPIC,
            model="claude-haiku",
            request_id="request",
            input_tokens=10,
            output_tokens=5,
            cost_microusd=1,
        )
