from collections.abc import Sequence

import pytest

import oms_hub.anki.lcl as lcl_module
from oms_hub.anki.domain import SourceKind
from oms_hub.anki.lcl import (
    LCLGenerationError,
    LCLService,
    LectureConcept,
    LectureConceptLedger,
    LedgerSourceRef,
)
from oms_hub.anki.sources import SourcePassage
from oms_hub.anki.v2_contracts import (
    LectureConceptLedgerV2,
    LectureConceptV2,
)
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.structured import StructuredJSONResult


def _passage(
    passage_id_seed: str,
    text: str,
    *,
    kind: SourceKind = SourceKind.SLIDE,
) -> SourcePassage:
    passage = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="upload-7",
        source_kind=kind,
        locator=f"{kind.value}:{passage_id_seed}",
        text=text,
        slide_number=3 if kind is not SourceKind.TRANSCRIPT else None,
        start_seconds=12 if kind is SourceKind.TRANSCRIPT else None,
        end_seconds=24 if kind is SourceKind.TRANSCRIPT else None,
    )
    return passage


def _concept(
    passage: SourcePassage,
    *,
    concept_id: str = "iron-stores",
    statement: str = "Low ferritin indicates depleted iron stores.",
    paraphrases: tuple[str, str] = (
        "Which laboratory value falls early in iron deficiency?",
        "How do iron stores affect ferritin concentration?",
    ),
) -> LectureConcept:
    return LectureConcept(
        concept_id=concept_id,
        source_refs=(
            LedgerSourceRef(passage_id=passage.passage_id),
        ),
        statement=statement,
        hypothetical_card=(
            "In iron deficiency, ferritin is {{c1::low}}."
        ),
        paraphrases=paraphrases,
        importance="core",
    )


def _result(
    ledger: LectureConceptLedger,
    *,
    raw_text: str = '{"concepts":[]}',
) -> StructuredJSONResult[LectureConceptLedger]:
    return StructuredJSONResult(
        value=ledger,
        raw_text=raw_text,
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        request_id="request-1",
        input_tokens=20,
        output_tokens=10,
        cost_microusd=5,
    )


class QueueStructuredService:
    def __init__(
        self,
        results: Sequence[StructuredJSONResult[LectureConceptLedger]],
    ) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, str]] = []

    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[LectureConceptLedger],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[LectureConceptLedger]:
        assert output_model is LectureConceptLedger
        self.calls.append((instruction, input_text))
        return self.results.pop(0)


class V2StructuredService:
    def __init__(self, ledger: LectureConceptLedgerV2) -> None:
        self.ledger = ledger
        self.calls: list[tuple[str, str]] = []

    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[LectureConceptLedgerV2],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[LectureConceptLedgerV2]:
        assert output_model is LectureConceptLedgerV2
        self.calls.append((instruction, input_text))
        return StructuredJSONResult(
            value=self.ledger,
            raw_text=self.ledger.model_dump_json(),
            provider=provider,
            model=model,
            request_id="request-v2",
            input_tokens=40,
            output_tokens=20,
            cost_microusd=9,
        )


def _v2_passages() -> list[SourcePassage]:
    return [
        SourcePassage.create(
            revision_id=7,
            lecture_id=12,
            artifact_id="slides-7",
            source_kind=SourceKind.SLIDE,
            locator="slide:3",
            text="Iron deficiency causes low ferritin.",
            slide_number=3,
        ),
        SourcePassage.create(
            revision_id=8,
            lecture_id=12,
            artifact_id="transcript-8",
            source_kind=SourceKind.TRANSCRIPT,
            locator="transcript:1:12-24",
            text="Iron deficiency depletes stores before microcytosis.",
            start_seconds=12,
            end_seconds=24,
        ),
        SourcePassage.create(
            revision_id=9,
            lecture_id=12,
            artifact_id="outline:9",
            source_kind=SourceKind.SUMMARY,
            locator="summary:depth:1",
            text="DEEP: iron deficiency laboratory sequence [3]",
            source_id="SUM:12:DEPTH:D1",
            summary_backrefs=("3",),
            summary_section="depth",
        ),
        SourcePassage.create(
            revision_id=9,
            lecture_id=12,
            artifact_id="outline:9",
            source_kind=SourceKind.SUMMARY,
            locator="summary:emphasis:1",
            text="Repeated: iron deficiency lowers ferritin [4]",
            source_id="SUM:12:EMPH:E1",
            summary_backrefs=("4",),
            summary_section="emphasis",
        ),
    ]


def _v2_ledger(passages: Sequence[SourcePassage]) -> LectureConceptLedgerV2:
    return LectureConceptLedgerV2(
        lecture_entity_count=2,
        concepts=(
            LectureConceptV2(
                concept_id="C01",
                canonical_statement="Iron deficiency causes low ferritin.",
                hypothetical_card=(
                    "Iron deficiency causes {{c1::low ferritin}}."
                ),
                primary_entity="iron deficiency",
                aliases=("low ferritin", "depleted iron stores"),
                paraphrases=(
                    "iron deficiency low ferritin",
                    "iron deficiency depleted stores",
                    "iron deficiency laboratory sequence",
                ),
                depth="deep",
                emphasis_flag=True,
                importance="high",
                passage_ids=tuple(passage.source_id for passage in passages),
            ),
        ),
        intentionally_uncited=(),
    )


def test_v2_ledger_uses_readable_source_ids_and_summary_metadata() -> None:
    passages = _v2_passages()
    ledger = _v2_ledger(passages)
    structured = V2StructuredService(ledger)
    service = LCLService(
        structured,  # type: ignore[arg-type]
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        prompt_version="lecture-concept-ledger",
        prompt_text="# V2 ledger prompt",
        prompt_hash="123456789abc",
        schema_name="lcl_v2",
    )

    artifact = service.generate(passages)

    assert artifact.ledger == ledger
    payload = structured.calls[0][1]
    assert passages[0].source_id in payload
    assert passages[0].passage_id not in payload
    assert '"summary_backrefs":["3"]' in payload


def test_v2_ledger_requires_disposition_for_every_primary_passage() -> None:
    passages = _v2_passages()
    valid = _v2_ledger(passages)
    concept = valid.concepts[0].model_copy(
        update={
            "passage_ids": tuple(
                passage.source_id
                for passage in passages
                if passage.source_kind is not SourceKind.TRANSCRIPT
            )
        }
    )
    invalid = valid.model_copy(update={"concepts": (concept,)})
    structured = V2StructuredService(invalid)
    service = LCLService(
        structured,  # type: ignore[arg-type]
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        prompt_version="lecture-concept-ledger",
        prompt_text="# V2 ledger prompt",
        schema_name="lcl_v2",
    )

    with pytest.raises(LCLGenerationError, match="primary passage"):
        service.generate(passages)

    assert len(structured.calls) == 2


def test_v2_concept_cannot_be_grounded_only_by_summary() -> None:
    passages = _v2_passages()
    valid = _v2_ledger(passages)
    summary_only = valid.concepts[0].model_copy(
        update={
            "concept_id": "C02",
            "passage_ids": tuple(
                passage.source_id
                for passage in passages
                if passage.source_kind is SourceKind.SUMMARY
            ),
        }
    )
    invalid = valid.model_copy(
        update={"concepts": (*valid.concepts, summary_only)}
    )
    structured = V2StructuredService(invalid)
    service = LCLService(
        structured,  # type: ignore[arg-type]
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        prompt_version="lecture-concept-ledger",
        prompt_text="# V2 ledger prompt",
        schema_name="lcl_v2",
    )

    with pytest.raises(LCLGenerationError, match="primary-source"):
        service.generate(passages)


def test_v2_ledger_maps_every_depth_and_emphasis_summary_item() -> None:
    passages = _v2_passages()
    valid = _v2_ledger(passages)
    concept = valid.concepts[0].model_copy(
        update={
            "passage_ids": tuple(
                passage.source_id
                for passage in passages
                if passage.source_kind is not SourceKind.SUMMARY
            )
        }
    )
    invalid = valid.model_copy(update={"concepts": (concept,)})
    structured = V2StructuredService(invalid)
    service = LCLService(
        structured,  # type: ignore[arg-type]
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        prompt_version="lecture-concept-ledger",
        prompt_text="# V2 ledger prompt",
        schema_name="lcl_v2",
    )

    with pytest.raises(LCLGenerationError, match="DEPTH or EMPHASIS"):
        service.generate(passages)


def test_v2_ledger_adapts_to_runtime_without_losing_v2_fields() -> None:
    passages = _v2_passages()
    ledger = _v2_ledger(passages)

    runtime = lcl_module.runtime_ledger_from_v2(ledger, passages)

    concept = runtime.concepts[0]
    assert concept.statement == "Iron deficiency causes low ferritin."
    assert concept.primary_entity == "iron deficiency"
    assert concept.aliases == ("low ferritin", "depleted iron stores")
    assert concept.depth == "deep"
    assert concept.emphasis_flag is True
    assert concept.importance == "high"
    assert concept.source_passage_ids == tuple(
        passage.source_id for passage in passages
    )
    assert {reference.passage_id for reference in concept.source_refs} == {
        passage.passage_id for passage in passages
    }
    assert runtime.lecture_entity_count == 2


def test_v2_ledger_enforces_depth_map_classification() -> None:
    passages = _v2_passages()
    valid = _v2_ledger(passages)
    wrong_depth = valid.concepts[0].model_copy(update={"depth": "surface"})
    invalid = valid.model_copy(update={"concepts": (wrong_depth,)})
    structured = V2StructuredService(invalid)
    service = LCLService(
        structured,  # type: ignore[arg-type]
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        prompt_version="lecture-concept-ledger",
        prompt_text="# V2 ledger prompt",
        schema_name="lcl_v2",
    )

    with pytest.raises(LCLGenerationError, match="depth classification"):
        service.generate(passages)


def test_v2_ledger_enforces_professor_emphasis_flag() -> None:
    passages = _v2_passages()
    valid = _v2_ledger(passages)
    no_emphasis = valid.concepts[0].model_copy(
        update={"emphasis_flag": False}
    )
    invalid = valid.model_copy(update={"concepts": (no_emphasis,)})
    structured = V2StructuredService(invalid)
    service = LCLService(
        structured,  # type: ignore[arg-type]
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        prompt_version="lecture-concept-ledger",
        prompt_text="# V2 ledger prompt",
        schema_name="lcl_v2",
    )

    with pytest.raises(LCLGenerationError, match="emphasis flag"):
        service.generate(passages)


def test_v2_statement_must_overlap_primary_evidence_not_only_summary() -> None:
    passages = _v2_passages()
    unrelated = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:4",
        text="Welcome and course objectives.",
        slide_number=4,
    )
    passages.append(unrelated)
    valid = _v2_ledger(passages)
    weak = valid.concepts[0].model_copy(
        update={
            "concept_id": "C02",
            "passage_ids": (
                unrelated.source_id,
                "SUM:12:DEPTH:D1",
                "SUM:12:EMPH:E1",
            ),
        }
    )
    invalid = valid.model_copy(update={"concepts": (*valid.concepts, weak)})
    structured = V2StructuredService(invalid)
    service = LCLService(
        structured,  # type: ignore[arg-type]
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        prompt_version="lecture-concept-ledger",
        prompt_text="# V2 ledger prompt",
        schema_name="lcl_v2",
    )

    with pytest.raises(LCLGenerationError, match="unsupported by primary"):
        service.generate(passages)


def test_generates_validated_ledger_with_four_queries_per_concept() -> None:
    passage = _passage(
        "3",
        "Low ferritin indicates depleted iron stores in iron deficiency.",
    )
    ledger = LectureConceptLedger(concepts=(_concept(passage),))
    structured = QueueStructuredService([_result(ledger), _result(ledger)])
    service = LCLService(
        structured,
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        prompt_version="lcl-v1",
    )

    artifact = service.generate([passage])

    assert artifact.ledger == ledger
    assert artifact.ledger.concepts[0].queries == (
        artifact.ledger.concepts[0].statement,
        artifact.ledger.concepts[0].hypothetical_card,
        *artifact.ledger.concepts[0].paraphrases,
    )
    assert artifact.repair_attempted is False
    assert passage.passage_id in structured.calls[0][1]


def test_uses_resolved_markdown_prompt_for_generation() -> None:
    passage = _passage("3", "Ferritin reflects iron stores.")
    ledger = LectureConceptLedger(concepts=(_concept(passage),))
    structured = QueueStructuredService([_result(ledger)])
    service = LCLService(
        structured,
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        prompt_version="lecture-concept-ledger",
        prompt_text="# Custom ledger rubric\n\nUse the supplied passages.",
        prompt_hash="123456789abc",
    )

    artifact = service.generate([passage])

    assert structured.calls[0][0] == (
        "# Custom ledger rubric\n\nUse the supplied passages."
    )
    assert artifact.prompt_hash == "123456789abc"


def test_invalid_first_ledger_gets_one_deterministic_repair() -> None:
    passage = _passage(
        "3",
        "Low ferritin indicates depleted iron stores in iron deficiency.",
    )
    duplicate_queries = LectureConceptLedger(
        concepts=(
            _concept(
                passage,
                paraphrases=(
                    "Low ferritin indicates depleted iron stores.",
                    "How do iron stores affect ferritin concentration?",
                ),
            ),
        )
    )
    repaired = LectureConceptLedger(concepts=(_concept(passage),))
    structured = QueueStructuredService(
        [_result(duplicate_queries), _result(repaired)]
    )
    service = LCLService(
        structured,
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        prompt_version="lcl-v1",
    )

    artifact = service.generate([passage])

    assert artifact.ledger == repaired
    assert artifact.repair_attempted is True
    assert len(structured.calls) == 2
    assert "repair" in structured.calls[1][0].casefold()


def test_unresolved_source_reference_fails_after_one_repair() -> None:
    passage = _passage("3", "Ferritin reflects iron stores.")
    missing_ref = LedgerSourceRef(passage_id="f" * 64)
    invalid = LectureConceptLedger(
        concepts=(
            _concept(passage).model_copy(
                update={"source_refs": (missing_ref,)}
            ),
        )
    )
    structured = QueueStructuredService(
        [_result(invalid), _result(invalid)]
    )
    service = LCLService(
        structured,
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        prompt_version="lcl-v1",
    )

    with pytest.raises(LCLGenerationError, match="source"):
        service.generate([passage])

    assert len(structured.calls) == 2


def test_duplicate_concept_ids_are_rekeyed_deterministically() -> None:
    passage = _passage("3", "Ferritin reflects iron stores.")
    ledger = LectureConceptLedger(
        concepts=(_concept(passage), _concept(passage))
    )
    structured = QueueStructuredService([_result(ledger)])
    service = LCLService(
        structured,
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        prompt_version="lcl-v1",
    )

    artifact = service.generate([passage])

    assert [
        concept.concept_id for concept in artifact.ledger.concepts
    ] == ["iron-stores", "iron-stores-2"]
    assert len(structured.calls) == 1
