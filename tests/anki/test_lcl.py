from collections.abc import Sequence

import pytest

from oms_hub.anki.domain import SourceKind
from oms_hub.anki.lcl import (
    LCLGenerationError,
    LCLService,
    LectureConcept,
    LectureConceptLedger,
    LedgerSourceRef,
)
from oms_hub.anki.sources import SourcePassage
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
