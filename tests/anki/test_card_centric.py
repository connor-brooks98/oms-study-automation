import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from oms_hub.anki.card_centric import (
    CardCentricClassifier,
    CardCentricLedgerService,
    CardCentricValidationError,
    _redacted_invalid_response,
    build_snapshot_census,
    build_source_index,
    resolve_card_centric_scope,
    s2_generation_parameters,
    scope_cards,
    select_high_yield,
    select_high_yield_v2,
    selection_eligible,
    selection_eligible_v2,
)
from oms_hub.anki.card_centric_contracts import (
    CardClassification,
    CardClassificationBatchOutput,
    CardConcept,
    CardConceptLedger,
    CardFieldReview,
    CardRecord,
    CensusTrust,
    CoveredFactEvidence,
    FastCardClassification,
    GeneratedCardResolution,
    SnapshotCensus,
    serialize_card_centric_ledger,
    stable_fact_key,
)
from oms_hub.anki.domain import SourceKind
from oms_hub.anki.sources import SourcePassage
from oms_hub.llm.domain import (
    DiagnosticSource,
    GeneratedText,
    LLMRequestError,
    ProviderName,
)
from oms_hub.llm.structured import StructuredOutputError, StructuredTextService


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


def test_v1_ledger_serialization_matches_pinned_base_document_and_hash() -> None:
    # This literal is the card_centric_v1 shape at e1b44653880751e24ce0309ca8af39a1e201f2fb.
    ledger = CardConceptLedger(
        lecture_entity_count=1,
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="Iron deficiency causes low ferritin.",
                primary_entity="iron deficiency",
                aliases=("low ferritin",),
                depth="deep",
                emphasis_flag=False,
                importance="high",
            ),
        ),
        forbidden_cloze_targets=("iron deficiency",),
    )
    document = serialize_card_centric_ledger(
        ledger,
        pipeline_contract_version="card_centric_v1",
    )

    assert document == {
        "contract_version": 1,
        "concepts": [
            {
                "contract_version": 1,
                "concept_id": "C01",
                "canonical_statement": "Iron deficiency causes low ferritin.",
                "primary_entity": "iron deficiency",
                "aliases": ["low ferritin"],
                "depth": "deep",
                "emphasis_flag": False,
                "importance": "high",
            }
        ],
        "lecture_entity_count": 1,
        "forbidden_cloze_targets": ["iron deficiency"],
    }
    assert (
        hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        == "9dcc821768f5fbb6c600d69be3740ca4e357523f741ab8bc67b1fca6ef8a5747"
    )


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


def test_v2_selection_uses_grounded_fast_coverage_and_t6_only_below_minimum() -> None:
    source = build_source_index(
        [_passage(SourceKind.SUMMARY, "summary:1", "summary evidence")],
        snapshot_id="snapshot-1",
        source_revision_hashes={7: "a" * 64},
    )
    ledger = CardConceptLedger(
        lecture_entity_count=1,
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="fact",
                primary_entity="Disease",
                depth="deep",
                emphasis_flag=True,
                importance="high",
            ),
        ),
    )
    maybe = CardClassification(
        note_id=2, verdict="MAYBE", primary_subject="fixture", reason="uncertain"
    )
    fast = FastCardClassification(
        note_id=1,
        verdict="LIKELY_YES",
        grounded_concept_ids=("C01",),
        supporting_passage_ids=(source.passages[0].passage_id,),
        reason="grounded",
    )

    assert selection_eligible_v2(
        CardClassification(
            note_id=4,
            verdict="YES",
            primary_subject="fixture",
            reason="summary",
            supporting_passage_ids=(source.passages[0].passage_id,),
        ),
        source,
    )
    result = select_high_yield_v2(
        (maybe,),
        fast_classifications=(fast,),
        fast_fallback_note_ids=(3,),
        ledger=ledger,
        source_index=source,
        generated_cards=(),
        target=65,
        cap=70,
        minimum=60,
    )

    assert result.selected_existing_note_ids == (1,)
    assert result.excluded_existing_note_ids == (2, 3)
    assert result.selected_generated_card_ids == ()
    assert result.below_warning_floor is True


def test_v2_selection_keeps_all_mandatory_high_existing_cards_above_soft_cap() -> None:
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
                canonical_statement="critical",
                primary_entity="Disease",
                depth="deep",
                emphasis_flag=True,
                importance="high",
            ),
        ),
    )
    classifications = tuple(
        CardClassification(
            note_id=note_id,
            verdict="YES",
            primary_subject="fixture",
            reason="grounded",
            covered_concept_ids=("C01",),
            supporting_passage_ids=(source.passages[0].passage_id,),
        )
        for note_id in range(1, 72)
    )

    result = select_high_yield_v2(
        classifications,
        fast_classifications=(),
        ledger=ledger,
        source_index=source,
        generated_cards=(),
        target=65,
        cap=70,
        minimum=60,
    )

    assert set(result.selected_existing_note_ids) == set(range(1, 72))
    assert result.excluded_existing_note_ids == ()


def test_v2_selection_stops_ordinary_cards_at_target_of_65() -> None:
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
                canonical_statement="routine",
                primary_entity="Disease",
                depth="medium",
                emphasis_flag=False,
                importance="medium",
            ),
        ),
    )
    classifications = tuple(
        CardClassification(
            note_id=note_id,
            verdict="YES",
            primary_subject="fixture",
            reason="grounded",
            covered_concept_ids=("C01",),
            supporting_passage_ids=(source.passages[0].passage_id,),
        )
        for note_id in range(1, 81)
    )

    result = select_high_yield_v2(
        classifications,
        fast_classifications=(),
        ledger=ledger,
        source_index=source,
        generated_cards=(),
        target=65,
        cap=70,
        minimum=60,
    )

    assert len(result.selected_existing_note_ids) == 65
    assert set(result.selected_existing_note_ids).isdisjoint(result.excluded_existing_note_ids)
    assert set(result.selected_existing_note_ids) | set(
        result.excluded_existing_note_ids
    ) == set(range(1, 81))
    assert result.selected_generated_card_ids == ()


def test_v2_selection_allows_mandatory_high_cards_through_70_without_overflow() -> None:
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
                canonical_statement="critical",
                primary_entity="Disease",
                depth="deep",
                emphasis_flag=True,
                importance="high",
            ),
        ),
    )
    classifications = tuple(
        CardClassification(
            note_id=note_id,
            verdict="YES",
            primary_subject="fixture",
            reason="grounded",
            covered_concept_ids=("C01",),
            supporting_passage_ids=(source.passages[0].passage_id,),
        )
        for note_id in range(1, 71)
    )

    result = select_high_yield_v2(
        classifications,
        fast_classifications=(),
        ledger=ledger,
        source_index=source,
        generated_cards=(),
        target=65,
        cap=70,
        minimum=60,
    )

    assert set(result.selected_existing_note_ids) == set(range(1, 71))


def test_v2_selection_places_mandatory_existing_before_medium_generated_rows() -> None:
    source = build_source_index(
        [_passage(SourceKind.SLIDE, "slide:1", "evidence")],
        snapshot_id="snapshot-1",
        source_revision_hashes={7: "a" * 64},
    )
    ledger = CardConceptLedger(
        lecture_entity_count=2,
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="critical",
                primary_entity="Critical",
                depth="deep",
                emphasis_flag=True,
                importance="high",
            ),
            CardConcept(
                concept_id="C02",
                canonical_statement="ordinary",
                primary_entity="Ordinary",
                depth="medium",
                emphasis_flag=False,
                importance="medium",
            ),
        ),
    )
    mandatory = tuple(
        CardClassification(
            note_id=note_id,
            verdict="YES",
            primary_subject="fixture",
            reason="grounded",
            covered_concept_ids=("C01",),
            supporting_passage_ids=(source.passages[0].passage_id,),
        )
        for note_id in range(1, 11)
    )
    ordinary = tuple(
        GeneratedCardResolution(
            card_id=f"G{index:02d}",
            concept_id="C02",
            fact_id=f"C02-M{index}",
            text=f"Ordinary {{c1::fact {index}}}.",
            extra="",
            source_passage_ids=(source.passages[0].passage_id,),
            evidence_ids=(f"E{index}",),
        )
        for index in range(1, 65)
    )

    result = select_high_yield_v2(
        mandatory,
        fast_classifications=(),
        ledger=ledger,
        source_index=source,
        generated_cards=ordinary,
        target=65,
        cap=70,
        minimum=60,
    )

    assert set(result.selected_existing_note_ids) == set(range(1, 11))
    assert result.selected_generated_card_ids == tuple(
        f"G{index:02d}" for index in range(1, 65)
    )
    assert len(result.selected_existing_note_ids) + len(result.selected_generated_card_ids) == 74


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
    downgraded = classifier.validate_output(
        opaque,
        cards=(card,),
        source_index=source,
        concept_ids=(),
    )
    assert downgraded[0].verdict == "MAYBE"
    assert downgraded[0].supporting_passage_ids == ()
    assert not selection_eligible(downgraded[0], source)


def test_v2_fact_coverage_requires_normalized_card_field_evidence() -> None:
    source = build_source_index(
        [_passage(SourceKind.SLIDE, "slide:1", "CD40 is expressed on B cells")],
        snapshot_id="snapshot-1",
        source_revision_hashes={7: "a" * 64},
    )
    card = CardRecord(
        note_id=1,
        content_sha256="1" * 64,
        text="<b>CD40L</b> is expressed on {{c1::T cells}}.",
        extra="CD40 is expressed on B cells.",
        tags=(),
        deck_names=("AnKing",),
    )
    classifier = CardCentricClassifier(StructuredTextService(_Generator([])))
    passage_id = source.passages[0].passage_id
    valid = CardClassificationBatchOutput(
        results=(
            CardClassification(
                note_id=1,
                verdict="YES",
                primary_subject="CD40L",
                reason="The card directly states the location.",
                covered_concept_ids=("C01",),
                covered_fact_ids=("C01-M1",),
                covered_fact_evidence=(
                    CoveredFactEvidence(
                        fact_id="C01-M1",
                        field="text",
                        span="CD40L is expressed on T cells",
                    ),
                ),
                supporting_passage_ids=(passage_id,),
                field_reviews=(
                    CardFieldReview(
                        field="extra",
                        disposition="exclude_from_fact_evidence",
                        reason="Extra is not used for this fact.",
                    ),
                ),
            ),
        )
    )

    validated = classifier.validate_output(
        valid,
        cards=(card,),
        source_index=source,
        concept_ids=("C01",),
        fact_ids_by_concept={"C01": ("C01-M1",)},
    )[0]
    assert validated.covered_fact_ids == ("C01-M1",)
    assert selection_eligible_v2(validated, source)

    missing = valid.model_copy(
        update={
            "results": (
                valid.results[0].model_copy(update={"covered_fact_evidence": ()}),
            )
        }
    )
    with pytest.raises(CardCentricValidationError, match="exact card-field evidence"):
        classifier.validate_output(
            missing,
            cards=(card,),
            source_index=source,
            concept_ids=("C01",),
            fact_ids_by_concept={"C01": ("C01-M1",)},
        )

    lecture_only = valid.model_copy(
        update={
            "results": (
                valid.results[0].model_copy(
                    update={
                        "covered_fact_evidence": (
                            CoveredFactEvidence(
                                fact_id="C01-M1",
                                field="text",
                                span="CD40 is expressed on B cells",
                            ),
                        )
                    }
                ),
            )
        }
    )
    with pytest.raises(CardCentricValidationError, match="does not occur"):
        classifier.validate_output(
            lecture_only,
            cards=(card,),
            source_index=source,
            concept_ids=("C01",),
            fact_ids_by_concept={"C01": ("C01-M1",)},
        )


def test_fact_identity_survives_reordering_and_depth_metadata_is_rejected() -> None:
    assert stable_fact_key(" CD40 ", "Expressed  on B cells") == stable_fact_key(
        "cd40", "expressed on b cells"
    )
    first = CardConcept(
        concept_id="C01",
        canonical_statement="CD40 is expressed on B cells.",
        primary_entity="CD40",
        depth="deep",
        emphasis_flag=False,
        importance="high",
    )
    reordered = first.model_copy(update={"concept_id": "C02"})
    assert first.stable_fact_keys == reordered.stable_fact_keys

    with pytest.raises(ValueError, match="depth metadata"):
        CardConceptLedger(
            lecture_entity_count=1,
            concepts=(
                CardConcept(
                    concept_id="C01",
                    canonical_statement="Six diseases received deep coverage.",
                    primary_entity="Lecture coverage",
                    depth="deep",
                    emphasis_flag=False,
                    importance="high",
                ),
            ),
        )
    with pytest.raises(ValueError, match="placeholders"):
        CardConceptLedger(
            lecture_entity_count=1,
            concepts=(
                CardConcept(
                    concept_id="C01",
                    canonical_statement=(
                        "The named clinical checklist is used to recognize disease."
                    ),
                    primary_entity="Warning signs",
                    depth="medium",
                    emphasis_flag=False,
                    importance="medium",
                ),
            ),
        )
    with pytest.raises(ValueError, match="C01-M1: SCID pathogenesis"):
        CardConceptLedger(
            lecture_entity_count=1,
            concepts=(
                CardConcept(
                    concept_id="C01",
                    canonical_statement=(
                        "SCID pathogenesis was covered in molecular detail in lecture."
                    ),
                    primary_entity="SCID",
                    depth="deep",
                    emphasis_flag=False,
                    importance="high",
                ),
            ),
        )
    with pytest.raises(ValueError, match="semicolon"):
        CardConcept(
            concept_id="C01",
            canonical_statement="Two facts.",
            primary_entity="CD40",
            depth="deep",
            emphasis_flag=False,
            importance="high",
            fact_descriptions=(
                "CD40L is on T cells; CD40 is on B cells.",
            ),
        )
    with pytest.raises(ValueError, match="multiple locations"):
        CardConcept(
            concept_id="C01",
            canonical_statement="Two locations.",
            primary_entity="CD40 signaling",
            depth="deep",
            emphasis_flag=False,
            importance="high",
            fact_descriptions=(
                "CD40L is expressed on T cells while CD40 is expressed on B cells.",
            ),
        )
    with pytest.raises(ValueError, match="depth metadata"):
        CardConceptLedger(
            lecture_entity_count=1,
            concepts=(
                CardConcept(
                    concept_id="C01",
                    canonical_statement="XLP was covered at only a basic level.",
                    primary_entity="XLP",
                    depth="surface",
                    emphasis_flag=False,
                    importance="low",
                ),
            ),
        )


def test_fact_ceiling_requires_a_continuation_concept() -> None:
    with pytest.raises(ValueError, match="less than or equal to 5"):
        CardConcept(
            concept_id="C01",
            canonical_statement="Dense entity.",
            primary_entity="Dense entity",
            depth="deep",
            emphasis_flag=False,
            importance="high",
            suggested_fact_count=6,
            fact_descriptions=tuple(f"Fact {index}." for index in range(1, 7)),
        )

    ledger = CardConceptLedger(
        lecture_entity_count=1,
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="First five facts.",
                primary_entity="Dense entity",
                depth="deep",
                emphasis_flag=False,
                importance="high",
                suggested_fact_count=5,
                fact_descriptions=tuple(f"Fact {index}." for index in range(1, 6)),
            ),
            CardConcept(
                concept_id="C02",
                canonical_statement="Sixth fact.",
                primary_entity="Dense entity",
                depth="deep",
                emphasis_flag=False,
                importance="high",
                fact_descriptions=("Fact 6.",),
            ),
        ),
    )
    assert len(set(ledger.fact_stable_keys.values())) == 6
    assert "fact_stable_keys" not in CardConceptLedger.model_json_schema()["properties"]
    assert serialize_card_centric_ledger(
        ledger, pipeline_contract_version="card_centric_v2"
    )["fact_stable_keys"] == ledger.fact_stable_keys


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
    assert all(
        "Return exactly one result for each of these note IDs and no other note IDs:"
        in instruction
        for instruction in generator.instructions
    )
    assert all("never synthesize IDs" in instruction for instruction in generator.instructions)
    assert all(options.cacheable_source_prefix == source.prefix for options in generator.options)
    assert result.telemetry.batch_count == 3
    assert [batch.note_ids for batch in result.telemetry.batches] == [(3,), (1,), (2,)]


def test_classifier_receives_concept_definitions_and_rejects_all_unmapped_yes() -> None:
    source = build_source_index(
        [_passage(SourceKind.SLIDE, "slide:1", "IgA transfusion evidence")],
        snapshot_id="snapshot-1",
        source_revision_hashes={7: "a" * 64},
    )
    concept = CardConcept(
        concept_id="C01",
        canonical_statement="IgA deficiency can cause transfusion anaphylaxis.",
        primary_entity="IgA deficiency",
        aliases=("anti-IgA reaction",),
        depth="deep",
        emphasis_flag=True,
        importance="high",
        fact_descriptions=("Anti-IgA causes transfusion anaphylaxis.",),
    )
    unmapped = CardClassificationBatchOutput(
        results=(
            CardClassification(
                note_id=1,
                verdict="YES",
                primary_subject="IgA deficiency",
                reason="the slide supports the card",
                supporting_passage_ids=(source.passages[0].passage_id,),
            ),
        )
    )
    generator = _Generator([unmapped])
    classifier = CardCentricClassifier(StructuredTextService(generator))

    with pytest.raises(CardCentricValidationError, match="no YES cards"):
        asyncio.run(
            classifier.classify(
                [_card(1)],
                source_index=source,
                concept_ids=("C01",),
                concepts=(concept,),
                provider=ProviderName.ANTHROPIC,
                model="claude-haiku",
            )
        )

    assert generator.inputs[0]["concept_definitions"] == [
        {
            "concept_id": "C01",
            "canonical_statement": "IgA deficiency can cause transfusion anaphylaxis.",
            "primary_entity": "IgA deficiency",
            "aliases": ["anti-IgA reaction"],
            "fact_descriptions": ["Anti-IgA causes transfusion anaphylaxis."],
            "facts": [
                {
                    "fact_id": "C01-M1",
                    "statement": "Anti-IgA causes transfusion anaphylaxis.",
                }
            ],
        }
    ]


def test_classifier_requires_fact_ids_to_match_reported_concepts() -> None:
    source = build_source_index(
        [_passage(SourceKind.SLIDE, "slide:1", "XLA has absent mature B cells")],
        snapshot_id="snapshot-1",
        source_revision_hashes={7: "a" * 64},
    )
    concept = CardConcept(
        concept_id="C02",
        canonical_statement="XLA has absent mature B cells.",
        primary_entity="XLA",
        depth="deep",
        emphasis_flag=True,
        importance="high",
        fact_descriptions=("Mature B cells are absent in XLA.",),
    )
    concept_only = CardClassificationBatchOutput(
        results=(
            CardClassification(
                note_id=1,
                verdict="YES",
                primary_subject="XLA",
                reason="the slide directly supports the card",
                covered_concept_ids=("C02",),
                supporting_passage_ids=(source.passages[0].passage_id,),
            ),
        )
    )
    generator = _Generator([concept_only])

    with pytest.raises(CardCentricValidationError, match="fact coverage"):
        asyncio.run(
            CardCentricClassifier(StructuredTextService(generator)).classify(
                [_card(1)],
                source_index=source,
                concept_ids=("C02",),
                concepts=(concept,),
                provider=ProviderName.ANTHROPIC,
                model="claude-haiku",
            )
        )


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
    assert generator.options.temperature == 0
    assert generator.options.max_tokens == 7000


def test_card_ledger_v2_prompt_pins_the_derived_importance_invariant() -> None:
    prompt = Path("src/oms_hub/anki/prompt_assets/card-centric-ledger-v2.md").read_text()

    assert "version: 2.1.3" in prompt
    assert "temperature:" not in prompt.split("---", 2)[1]
    assert "model:" not in prompt.split("---", 2)[1]
    assert (
        "`high` **if and only if** `depth` is `deep` **or** `emphasis_flag` is `true`."
        in prompt
    )
    assert (
        "`medium` **if and only if** `emphasis_flag` is `false` and `depth` is `medium`."
        in prompt
    )
    assert "`low` **if and only if** `emphasis_flag` is `false` and `depth` is `surface`." in prompt
    observed_hash = hashlib.sha256(prompt.encode()).hexdigest()
    assert observed_hash == "9e587aa5ddb0cc03b8b9cfa8ac37477eee1966d2c041bfd002469831b8c745c8"
    assert observed_hash != "1561da45dd05048dcf9d92fc709ce117f994bc0f38eb075a81bf2937bd1e2580"


@pytest.mark.parametrize(
    ("depth", "emphasis", "importance"),
    [
        ("deep", False, "high"),
        ("deep", True, "high"),
        ("medium", True, "high"),
        ("medium", False, "medium"),
        ("surface", True, "high"),
        ("surface", False, "low"),
    ],
)
def test_card_concept_importance_matrix(depth, emphasis, importance) -> None:
    concept = CardConcept(
        concept_id="C01",
        canonical_statement="fact",
        primary_entity="fact",
        depth=depth,
        emphasis_flag=emphasis,
        importance=importance,
    )
    assert concept.importance == importance


@pytest.mark.parametrize(
    ("depth", "emphasis", "importance"),
    [("deep", False, "medium"), ("medium", False, "low"), ("surface", True, "low")],
)
def test_card_concept_rejects_importance_conflicts(depth, emphasis, importance) -> None:
    with pytest.raises(ValueError, match="importance conflicts"):
        CardConcept(
            concept_id="C01",
            canonical_statement="fact",
            primary_entity="fact",
            depth=depth,
            emphasis_flag=emphasis,
            importance=importance,
        )


def test_card_ledger_valid_primary_makes_one_call_and_records_transmitted_identity() -> None:
    source = _ledger_source()
    attempts = []
    generator = _LedgerSequenceGenerator([_valid_ledger_text()])

    result = CardCentricLedgerService(StructuredTextService(generator), "S2").generate(
        source_index=source,
        provider=ProviderName.ANTHROPIC,
        model="claude-sonnet-5",
        record_attempt=attempts.append,
    )

    assert result.ledger.concepts[0].importance == "high"
    assert len(generator.calls) == 1
    assert [attempt.kind for attempt in attempts] == ["primary"]
    assert attempts[0].outcome == "accepted"
    assert attempts[0].generation_parameters["temperature"] == {
        "requested": 0,
        "transmission": "not_transmitted",
        "provider_default": "unknown_provider_default",
    }
    assert attempts[0].generation_parameters["max_tokens"] == {
        "value": 7000,
        "transmission": "transmitted",
    }
    assert attempts[0].generation_parameters["thinking"] == {
        "requested": "disabled",
        "transmission": "transmitted_disabled",
    }
    assert attempts[0].generation_parameters["cache"] == {
        "requested": "summary_prefix",
        "transmission": "anthropic_ephemeral",
    }
    assert result.generation_parameters_sha256 == attempts[0].generation_parameters_sha256
    assert result.request_ids == ("request-1",)
    assert result.request_id == "request-1"


@pytest.mark.parametrize(
    ("provider", "model", "temperature", "thinking", "cache"),
    [
        (
            ProviderName.ANTHROPIC,
            "claude-sonnet-5",
            {
                "requested": 0,
                "transmission": "not_transmitted",
                "provider_default": "unknown_provider_default",
            },
            {"requested": "disabled", "transmission": "transmitted_disabled"},
            {"requested": "summary_prefix", "transmission": "anthropic_ephemeral"},
        ),
        (
            ProviderName.ANTHROPIC,
            "claude-3-7-sonnet-latest",
            {"value": 0, "transmission": "transmitted"},
            {
                "requested": "disabled",
                "transmission": "not_transmitted",
                "provider_default": "unknown_provider_default",
            },
            {"requested": "summary_prefix", "transmission": "anthropic_ephemeral"},
        ),
        (
            ProviderName.OPENAI,
            "gpt-5.2",
            {"value": 0, "transmission": "transmitted"},
            {
                "requested": "disabled",
                "transmission": "not_transmitted",
                "provider_default": "unknown_provider_default",
            },
            {"requested": "summary_prefix", "transmission": "prompt_context_only"},
        ),
        (
            ProviderName.GEMINI,
            "gemini-3.6-flash",
            {"value": 0, "transmission": "transmitted"},
            {
                "requested": "disabled",
                "transmission": "not_transmitted",
                "provider_default": "unknown_provider_default",
            },
            {"requested": "summary_prefix", "transmission": "prompt_context_only"},
        ),
        (
            ProviderName.OPENROUTER,
            "openai/gpt-4o-mini",
            {"value": 0, "transmission": "transmitted"},
            {
                "requested": "disabled",
                "transmission": "not_transmitted",
                "provider_default": "unknown_provider_default",
            },
            {"requested": "summary_prefix", "transmission": "prompt_context_only"},
        ),
    ],
)
def test_card_ledger_records_complete_truthful_s2_identity_for_each_route(
    provider: ProviderName,
    model: str,
    temperature: dict[str, object],
    thinking: dict[str, str],
    cache: dict[str, str],
) -> None:
    attempts = []
    invalid = _valid_ledger_text().replace('"importance":"high"', '"importance":"low"')
    generator = _LedgerSequenceGenerator([invalid, _valid_ledger_text()])

    CardCentricLedgerService(StructuredTextService(generator), "S2").generate(
        source_index=_ledger_source(),
        provider=provider,
        model=model,
        record_attempt=attempts.append,
    )

    expected = s2_generation_parameters(provider, model)
    assert [attempt.generation_parameters for attempt in attempts] == [expected, expected]
    assert [attempt.generation_parameters_sha256 for attempt in attempts] == [
        attempts[0].generation_parameters_sha256,
        attempts[0].generation_parameters_sha256,
    ]
    assert expected["temperature"] == temperature
    assert expected["max_tokens"] == {"value": 7000, "transmission": "transmitted"}
    assert expected["thinking"] == thinking
    assert expected["cache"] == cache
    assert all(call[2].temperature == 0 and call[2].max_tokens == 7000 for call in generator.calls)


def test_card_ledger_invalid_primary_gets_one_complete_repair_and_replaces_output() -> None:
    source = _ledger_source()
    invalid = _valid_ledger_text().replace('"importance":"high"', '"importance":"low"')
    attempts = []
    generator = _LedgerSequenceGenerator([invalid, _valid_ledger_text()])

    result = CardCentricLedgerService(StructuredTextService(generator), "S2").generate(
        source_index=source,
        provider=ProviderName.ANTHROPIC,
        model="claude-sonnet-5",
        record_attempt=attempts.append,
    )

    assert result.ledger.model_dump_json() == _valid_ledger_text()
    assert len(generator.calls) == 2
    assert [attempt.outcome for attempt in attempts] == ["validation_failed", "accepted"]
    repair_instruction, repair_input, _ = generator.calls[1]
    assert "Correct only the reported validation defects" in repair_instruction
    assert json.loads(repair_input)["invalid_response"] == invalid
    assert "importance conflicts" in json.loads(repair_input)["validation_error"]
    assert attempts[0].invalid_response_sha256 == hashlib.sha256(invalid.encode()).hexdigest()
    assert result.request_ids == ("request-1", "request-2")
    assert result.request_id.startswith("card_ledger:")
    assert (result.input_tokens, result.output_tokens, result.cost_microusd) == (20, 10, 2)


@pytest.mark.parametrize("invalid_primary", [False, True])
def test_card_ledger_attempts_keep_requested_route_when_response_model_is_aliased(
    invalid_primary: bool,
) -> None:
    requested_provider = ProviderName.ANTHROPIC
    requested_model = "claude-sonnet-5"
    invalid = _valid_ledger_text().replace('"importance":"high"', '"importance":"low"')
    generator = _LedgerSequenceGenerator(
        [invalid, _valid_ledger_text()] if invalid_primary else [_valid_ledger_text()],
        response_provider=ProviderName.OPENAI,
        response_model="gpt-5.2-2026-08-01",
    )
    attempts = []

    CardCentricLedgerService(StructuredTextService(generator), "S2").generate(
        source_index=_ledger_source(),
        provider=requested_provider,
        model=requested_model,
        record_attempt=attempts.append,
    )

    expected_parameters = s2_generation_parameters(requested_provider, requested_model)
    expected_hash = hashlib.sha256(
        json.dumps(expected_parameters, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert [(attempt.provider, attempt.model) for attempt in attempts] == [
        (requested_provider, requested_model)
    ] * len(attempts)
    assert [attempt.generation_parameters for attempt in attempts] == [
        expected_parameters
    ] * len(attempts)
    assert [attempt.generation_parameters_sha256 for attempt in attempts] == [
        expected_hash
    ] * len(attempts)


@pytest.mark.parametrize(
    "value, secret",
    [
        (
            '{"api_key":"sk-json-secret-value","token":"token-json-secret"}',
            "sk-json-secret-value",
        ),
        (
            "Authorization: Bearer bearer-header-secret; access_token=access-value-secret",
            "bearer-header-secret",
        ),
        (
            "client_secret : client-secret-value, password=pass-value, token: token-value",
            "client-secret-value",
        ),
        (
            "{'api_key': 'single-quoted-secret', 'token': 'single-quoted-token'}",
            "single-quoted-secret",
        ),
    ],
)
def test_card_ledger_invalid_response_redacts_common_malformed_secret_forms(
    value: str,
    secret: str,
) -> None:
    redacted = _redacted_invalid_response(value)

    assert secret not in redacted
    assert "[REDACTED]" in redacted


def test_card_ledger_invalid_response_redacts_structured_failure_and_hashes_stored_bytes() -> None:
    source = _ledger_source()
    payload = json.loads(_valid_ledger_text())
    payload["api_key"] = "sk-structured-secret"
    payload["authorization"] = "Bearer structured-bearer-secret"
    payload["concepts"][0]["importance"] = "low"
    invalid = json.dumps(payload, separators=(",", ":"))
    attempts = []
    generator = _LedgerSequenceGenerator([invalid, _valid_ledger_text()])

    CardCentricLedgerService(StructuredTextService(generator), "S2").generate(
        source_index=source,
        provider=ProviderName.ANTHROPIC,
        model="claude-sonnet-5",
        record_attempt=attempts.append,
    )

    stored = attempts[0].invalid_response
    assert stored is not None
    assert "sk-structured-secret" not in stored
    assert "structured-bearer-secret" not in stored
    assert attempts[0].invalid_response_sha256 == hashlib.sha256(stored.encode()).hexdigest()


def test_card_ledger_invalid_response_is_bounded_after_redaction() -> None:
    value = "api_key=" + ("secret-value-" * 2_000)
    redacted = _redacted_invalid_response(value)

    assert len(redacted) <= 12_000
    assert "secret-value" not in redacted
    assert redacted.endswith("[REDACTED]")


@pytest.mark.parametrize("repair_kind", ["malformed", "conflict"])
def test_card_ledger_bad_repair_fails_closed_after_two_calls(repair_kind) -> None:
    source = _ledger_source()
    invalid = _valid_ledger_text().replace('"importance":"high"', '"importance":"low"')
    attempts = []
    repair = (
        '{"concepts":'
        if repair_kind == "malformed"
        else _valid_ledger_text().replace('"importance":"high"', '"importance":"low"')
    )
    generator = _LedgerSequenceGenerator([invalid, repair])

    with pytest.raises(StructuredOutputError):
        CardCentricLedgerService(StructuredTextService(generator), "S2").generate(
            source_index=source,
            provider=ProviderName.ANTHROPIC,
            model="claude-sonnet-5",
            record_attempt=attempts.append,
        )

    assert len(generator.calls) == 2
    assert [attempt.outcome for attempt in attempts] == ["validation_failed", "validation_failed"]


def test_card_ledger_repair_transport_error_keeps_primary_and_repair_attempts() -> None:
    source = _ledger_source()
    invalid = _valid_ledger_text().replace('"importance":"high"', '"importance":"low"')
    attempts = []
    generator = _LedgerSequenceGenerator([invalid, RuntimeError("Bearer sk-secret-token-value")])

    with pytest.raises(RuntimeError, match="Bearer"):
        CardCentricLedgerService(StructuredTextService(generator), "S2").generate(
            source_index=source,
            provider=ProviderName.ANTHROPIC,
            model="claude-sonnet-5",
            record_attempt=attempts.append,
        )

    assert [attempt.outcome for attempt in attempts] == ["validation_failed", "transport_failed"]
    assert attempts[0].invalid_response is not None
    assert attempts[1].validation_error == "Bearer [REDACTED]"
    assert "sk-secret-token-value" not in attempts[1].validation_error


def test_card_ledger_transport_failure_persists_safe_provider_diagnostics_only() -> None:
    invalid = _valid_ledger_text().replace('"importance":"high"', '"importance":"low"')
    attempts = []
    generator = _LedgerSequenceGenerator(
        [
            invalid,
            LLMRequestError(
                "provider rejected Bearer repair-secret",
                source=DiagnosticSource.NETWORK,
                http_status=400,
                provider_request_id="safe-provider-request-42",
            ),
        ]
    )

    with pytest.raises(LLMRequestError):
        CardCentricLedgerService(StructuredTextService(generator), "S2").generate(
            source_index=_ledger_source(),
            provider=ProviderName.ANTHROPIC,
            model="claude-sonnet-5",
            record_attempt=attempts.append,
        )

    assert attempts[1].request_id == "safe-provider-request-42"
    assert attempts[1].diagnostic_source == "network"
    assert attempts[1].http_status == 400
    assert attempts[1].validation_error == "provider rejected Bearer [REDACTED]"
    assert "repair-secret" not in attempts[1].validation_error


def test_card_ledger_repair_transport_failure_cannot_mask_attempt_persistence_failure() -> None:
    source = _ledger_source()
    invalid = _valid_ledger_text().replace('"importance":"high"', '"importance":"low"')
    generator = _LedgerSequenceGenerator([invalid, RuntimeError("Bearer repair-secret")])
    attempts = []

    def recorder(attempt) -> None:
        attempts.append(attempt)
        if attempt.call_index == 2:
            raise RuntimeError("attempt persistence unavailable")

    with pytest.raises(RuntimeError, match="attempt persistence unavailable"):
        CardCentricLedgerService(StructuredTextService(generator), "S2").generate(
            source_index=source,
            provider=ProviderName.ANTHROPIC,
            model="claude-sonnet-5",
            record_attempt=recorder,
        )

    assert [attempt.call_index for attempt in attempts] == [1, 2]


def _ledger_source():
    return build_source_index(
        [
            SourcePassage.create(
                revision_id=9,
                lecture_id=12,
                artifact_id="outline:9",
                source_kind=SourceKind.SUMMARY,
                locator="summary:core:1",
                text="summary-only phrase",
                source_id="SUM:12:CORE:01",
                summary_section="core",
            )
        ],
        snapshot_id="snapshot-1",
        source_revision_hashes={7: "a" * 64},
    )


def _valid_ledger_text() -> str:
    return CardConceptLedger(
        lecture_entity_count=1,
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="fact",
                primary_entity="fact",
                depth="deep",
                emphasis_flag=False,
                importance="high",
            ),
        ),
    ).model_dump_json()


class _LedgerSequenceGenerator:
    def __init__(
        self,
        responses,
        *,
        response_provider: ProviderName | None = None,
        response_model: str | None = None,
    ) -> None:
        self.responses = iter(responses)
        self.calls = []
        self.response_provider = response_provider
        self.response_model = response_model

    def generate_text(self, instruction, input_text, *, output_schema, provider, model, options):
        del output_schema
        self.calls.append((instruction, input_text, options))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return GeneratedText(
            text=response,
            provider=self.response_provider or provider,
            model=self.response_model or model,
            request_id=f"request-{len(self.calls)}",
            input_tokens=10,
            output_tokens=5,
            cost_microusd=1,
        )


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
                        concept_id="C01",
                        canonical_statement="fact",
                        primary_entity="fact",
                        aliases=(),
                        depth="deep",
                        emphasis_flag=False,
                        importance="high",
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
        self.instructions = []
        self.inputs = []

    def generate_text(self, instruction, input_text, *, output_schema, provider, model, options):
        del output_schema, provider, model
        self.instructions.append(instruction)
        self.options.append(options)
        self.inputs.append(json.loads(input_text))
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
