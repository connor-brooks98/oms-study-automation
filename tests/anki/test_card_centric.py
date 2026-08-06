import asyncio
import json

import pytest

from oms_hub.anki.card_centric import (
    CardCentricClassifier,
    CardCentricLedgerService,
    CardCentricValidationError,
    build_snapshot_census,
    build_source_index,
    resolve_card_centric_scope,
    scope_cards,
    select_high_yield,
    selection_eligible,
)
from oms_hub.anki.card_centric_contracts import (
    CardClassification,
    CardClassificationBatchOutput,
    CardConcept,
    CardConceptLedger,
    CardRecord,
    CensusTrust,
    GeneratedCardResolution,
    SnapshotCensus,
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
    assert first.passages[0].passage_id.startswith("SUM:12:CORE:01:P:")
    assert all(
        passage.passage_id.startswith(("SLD:", "TRX:", "SUM:")) for passage in first.passages
    )
    assert 'id="SUM:12:CORE:01:P:' in first.prefix


def test_card_centric_scope_uses_one_bounded_subject_alias_before_title_fallback() -> None:
    assert resolve_card_centric_scope(
        tag_allowlist=(),
        subject="Hematology",
        topic="Cardiology overview",
    ) == ("heme",)


def test_card_centric_scope_rejects_ambiguous_or_unknown_metadata() -> None:
    with pytest.raises(CardCentricValidationError, match="Existing-card tag scope"):
        resolve_card_centric_scope(tag_allowlist=(), subject="Heme Cardio", topic="")
    with pytest.raises(CardCentricValidationError, match="Existing-card tag scope"):
        resolve_card_centric_scope(tag_allowlist=(), subject="Foundations", topic="Research")


def test_census_accounts_for_every_note_and_refuses_unsafe_untagged_rate() -> None:
    census = build_snapshot_census(
        [
            _card(1),
            _card(2, ()),
            _card(
                3,
                ("#AK_Step::Heme",),
            ),
        ],
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


def test_census_excludes_other_recognized_systems_from_untagged_count() -> None:
    cards = [
        *[_card(note_id) for note_id in range(1, 98)],
        _card(98, ("#AK_Step::Cardio",)),
        _card(99, ("#AK_Step::GI",)),
        _card(100, ()),
    ]

    census = build_snapshot_census(
        cards,
        deck_allowlist=("AnKing",),
        scope_tokens=("heme",),
    )

    assert census.denominator_count == 100
    assert census.tagged_count == 97
    assert census.other_system_tagged_count == 2
    assert census.untagged_count == 1
    assert census.trust.untagged_rate == 0.01
    assert census.trust.decision == "trusted"
    assert census.mapping[98] == "other_system_excluded"
    assert census.mapping[99] == "other_system_excluded"
    assert census.mapping[100] == "untagged"


def test_census_rejects_inconsistent_rate_decision_and_zero_denominator() -> None:
    mapping = {1: "target_tagged"}
    with pytest.raises(ValueError, match="trust"):
        SnapshotCensus(
            snapshot_id="snapshot-1",
            denominator_count=1,
            tagged_count=1,
            other_system_tagged_count=0,
            untagged_count=0,
            deck_excluded_count=0,
            excluded_count=0,
            mapping=mapping,
            filters_sha256="a" * 64,
            trust=CensusTrust(
                decision="blocked",
                reason="bad",
                untagged_rate=0.01,
                safe_untagged_rate=0.03,
            ),
        )

    zero = build_snapshot_census(
        [_card(1, ("#AK_Step::Cardio",))],
        deck_allowlist=("Different deck",),
        scope_tokens=("heme",),
    )
    assert zero.denominator_count == 0
    assert zero.trust.untagged_rate == 0.0
    assert zero.trust.decision == "blocked"

    with pytest.raises(ValueError, match="three percent"):
        CensusTrust(
            decision="trusted",
            reason="bad threshold",
            untagged_rate=0.0,
            safe_untagged_rate=0.04,
        )


def test_census_blocks_exactly_at_the_three_percent_boundary() -> None:
    census = build_snapshot_census(
        [
            *[_card(note_id) for note_id in range(1, 98)],
            *[_card(note_id, ()) for note_id in range(98, 101)],
        ],
        deck_allowlist=("AnKing",),
        scope_tokens=("heme",),
    )

    assert census.trust.untagged_rate == 0.03
    assert census.trust.decision == "blocked"


def test_unresolved_generated_card_resolution_allows_empty_card_text() -> None:
    resolution = GeneratedCardResolution(
        card_id="CC-unresolved",
        concept_id="C01",
        fact_id="C01-M1",
        text="",
        source_passage_ids=("UNRESOLVED",),
        status="unresolved",
        reason="source evidence is insufficient",
    )

    assert resolution.status == "unresolved"
    assert resolution.text == ""


def test_tag_scope_is_a_complete_deterministic_partition() -> None:
    cards = [_card(3), _card(1), _card(2, ("#AK_Step::Cardio",))]
    census = build_snapshot_census(
        cards,
        deck_allowlist=("AnKing",),
        scope_tokens=("heme",),
    )
    scoped = scope_cards(cards, census=census, scope_tokens=("heme",))

    assert scoped.scoped_note_ids == (1, 3)
    assert scoped.unscoped_note_ids == (2,)
    assert set(scoped.scoped_note_ids) | set(scoped.unscoped_note_ids) == {1, 2, 3}


def test_high_yield_selection_is_stable_protects_mandatory_and_never_pads() -> None:
    source = build_source_index(
        [_passage(SourceKind.SLIDE, "slide:1", "evidence")],
        snapshot_id="snapshot-1",
        source_revision_hashes={7: "a" * 64},
    )
    ledger = CardConceptLedger(
        lecture_entity_count=1,
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="high yield fact",
                primary_entity="Disease",
                aliases=(),
                depth="deep",
                emphasis_flag=True,
                importance="high",
            ),
            CardConcept(
                concept_id="C02",
                canonical_statement="surface fact",
                primary_entity="Finding",
                aliases=(),
                depth="surface",
                emphasis_flag=False,
                importance="low",
            ),
        ),
    )
    classifications = tuple(
        CardClassification(
            note_id=note_id,
            verdict="YES",
            primary_subject="fixture",
            reason="grounded",
            covered_concept_ids=("C01" if note_id == 2 else "C02",),
            supporting_passage_ids=(source.passages[0].passage_id,),
        )
        for note_id in (3, 2)
    )
    selected, excluded, generated = select_high_yield(
        classifications,
        ledger=ledger,
        source_index=source,
        target=65,
        cap=70,
    )
    assert selected == (2, 3)
    assert excluded == ()
    assert generated == ()


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
                CardClassification(
                    note_id=9,
                    verdict="NO",
                    primary_subject="x",
                    reason="not taught",
                ),
            )
        ),
        CardClassificationBatchOutput(
            results=(
                CardClassification(
                    note_id=1,
                    verdict="YES",
                    primary_subject="x",
                    reason="taught",
                ),
                CardClassification(
                    note_id=2,
                    verdict="NO",
                    primary_subject="x",
                    reason="not taught",
                ),
            )
        ),
        CardClassificationBatchOutput(
            results=(
                CardClassification(
                    note_id=1,
                    verdict="NO",
                    primary_subject="x",
                    reason="not taught",
                ),
            )
        ),
    ):
        with pytest.raises(CardCentricValidationError):
            classifier.validate_output(
                output,
                cards=cards,
                source_index=source,
                concept_ids=(),
            )


def test_classifier_validates_only_canonical_source_prefixed_passage_ids() -> None:
    raw = _passage(SourceKind.SLIDE, "slide:1", "evidence")
    source = build_source_index(
        [raw],
        snapshot_id="snapshot-1",
        source_revision_hashes={7: "a" * 64},
    )
    card = _card(1)
    classifier = CardCentricClassifier(StructuredTextService(_Generator([])))
    canonical = source.passages[0].passage_id
    accepted = CardClassificationBatchOutput(
        results=(
            CardClassification(
                note_id=1,
                verdict="YES",
                primary_subject="anemia",
                reason="slide supports the tested fact",
                supporting_passage_ids=(canonical,),
            ),
        )
    )

    result = classifier.validate_output(
        accepted,
        cards=(card,),
        source_index=source,
        concept_ids=(),
    )
    assert selection_eligible(result[0], source)

    opaque = accepted.model_copy(
        update={
            "results": (
                accepted.results[0].model_copy(
                    update={"supporting_passage_ids": (raw.passage_id,)}
                ),
            )
        }
    )
    with pytest.raises(CardCentricValidationError, match="passage"):
        classifier.validate_output(
            opaque,
            cards=(card,),
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


def test_ledger_s2_round_trip_caches_only_the_summary_prefix() -> None:
    source = build_source_index(
        [
            _passage(SourceKind.SLIDE, "slide:1", "slide-only phrase"),
            _passage(SourceKind.TRANSCRIPT, "transcript:1", "transcript-only phrase"),
            SourcePassage.create(
                revision_id=9,
                lecture_id=12,
                artifact_id="outline:9",
                source_kind=SourceKind.SUMMARY,
                locator="summary:core:1",
                text="summary-only phrase",
                source_id="SUM:12:CORE:01",
                summary_section="core",
            ),
        ],
        snapshot_id="snapshot-1",
        source_revision_hashes={7: "a" * 64},
    )
    generator = _LedgerGenerator()
    result = CardCentricLedgerService(
        StructuredTextService(generator), "dedicated S2 ledger instruction"
    ).generate(source_index=source, provider=ProviderName.ANTHROPIC, model="sonnet")

    assert result.ledger.concepts[0].concept_id == "C01"
    assert generator.instruction == "dedicated S2 ledger instruction"
    assert "summary-only phrase" in generator.options.cacheable_source_prefix
    assert "slide-only phrase" not in generator.options.cacheable_source_prefix
    assert "transcript-only phrase" not in generator.options.cacheable_source_prefix


class _LedgerGenerator:
    def __init__(self) -> None:
        self.instruction = ""
        self.options = None

    def generate_text(self, instruction, input_text, *, output_schema, provider, model, options):
        del input_text, output_schema, provider, model
        self.instruction = instruction
        self.options = options
        return GeneratedText(
            text=CardConceptLedger(
                lecture_entity_count=1,
                concepts=(
                    CardConcept(
                        concept_id="C01", canonical_statement="fact", primary_entity="fact",
                        aliases=(), depth="deep", emphasis_flag=False, importance="high",
                    ),
                ),
            ).model_dump_json(),
            provider=ProviderName.ANTHROPIC,
            model="sonnet",
            request_id="request",
            input_tokens=10,
            output_tokens=5,
            cost_microusd=1,
        )


def test_classifier_contract_requires_one_line_reason_and_architecture_flags() -> None:
    classified = CardClassification(
        note_id=1,
        verdict="MAYBE",
        primary_subject="anemia",
        reason="lecture context is ambiguous",
        flags=("context_trap", "enumeration", "stat_cloze", "over_cloze"),
    )

    assert classified.flags[-1] == "over_cloze"
    with pytest.raises(ValueError, match="reason"):
        CardClassification(
            note_id=1,
            verdict="NO",
            primary_subject="anemia",
            reason="line one\nline two",
        )


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
                    CardClassification(
                        note_id=item["note_id"],
                        verdict="NO",
                        primary_subject="x",
                        reason="not taught",
                    )
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
