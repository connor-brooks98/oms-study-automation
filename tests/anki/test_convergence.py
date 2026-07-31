from collections.abc import Sequence

import pytest

from oms_hub.anki.convergence import (
    ConvergenceValidationError,
    ParaphraseExpansionService,
    update_growth,
)
from oms_hub.anki.lcl import LectureConcept, LedgerSourceRef
from oms_hub.anki.normalize import NormalizedNote
from oms_hub.anki.v2_contracts import ParaphraseExpansionV2
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.structured import StructuredJSONResult


def _concept() -> LectureConcept:
    return LectureConcept(
        concept_id="C01",
        source_refs=(LedgerSourceRef(passage_id="1" * 64),),
        statement="Hereditary spherocytosis increases MCHC.",
        hypothetical_card="HS shows {{c1::increased MCHC}}.",
        paraphrases=(
            "hereditary spherocytosis MCHC",
            "hereditary spherocytosis CBC finding",
            "hereditary spherocytosis increased MCHC",
        ),
        importance="high",
        primary_entity="hereditary spherocytosis",
        aliases=("HS",),
        depth="deep",
        source_passage_ids=("SLD:07:0031",),
    )


def _note(note_id: int) -> NormalizedNote:
    return NormalizedNote(
        note_id=note_id,
        model_name="AnKingOverhaul",
        text=f"Card {note_id}: hereditary spherocytosis finding.",
        extra="Lecture-supported detail.",
        raw_fields={"Text": "hereditary spherocytosis finding"},
        tags=("#Pathoma",),
        card_ids=(note_id + 100,),
        media=(),
        token_signature="hereditary spherocytosis finding",
        content_sha256=f"{note_id:064x}",
    )


def _expansion(*queries: str) -> ParaphraseExpansionV2:
    return ParaphraseExpansionV2(
        concept_id="C01",
        paraphrases=queries,
        targeting="Residual diagnostic and treatment facts.",
    )


class ExpansionQueue:
    def __init__(self, values: Sequence[ParaphraseExpansionV2]) -> None:
        self.values = list(values)
        self.requests: list[tuple[str, str]] = []

    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[ParaphraseExpansionV2],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[ParaphraseExpansionV2]:
        assert output_model is ParaphraseExpansionV2
        self.requests.append((instruction, input_text))
        value = self.values.pop(0)
        return StructuredJSONResult(
            value=value,
            raw_text=value.model_dump_json(),
            provider=provider,
            model=model,
            request_id=f"expand-{len(self.requests)}",
            input_tokens=20,
            output_tokens=10,
            cost_microusd=5,
        )


def _service(structured: ExpansionQueue) -> ParaphraseExpansionService:
    return ParaphraseExpansionService(
        structured,
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        prompt_text="# Paraphrase expansion",
        prompt_hash="123456789abc",
    )


def test_expansion_targets_residual_facts_and_retains_primary_entity() -> None:
    generated = _expansion(
        "hereditary spherocytosis osmotic fragility",
        "hereditary spherocytosis splenectomy indication",
        "hereditary spherocytosis EMA binding test",
    )
    structured = ExpansionQueue((generated,))

    result = _service(structured).expand(
        _concept(),
        used_paraphrases=_concept().paraphrases,
        found_notes=(_note(1), _note(2)),
        missing_facts=("Splenectomy is reserved for severe disease.",),
    )

    assert result.expansion == generated
    assert result.request_ids == ("expand-1",)
    request = structured.requests[0][1]
    assert "Splenectomy is reserved for severe disease" in request
    assert "Card 1: hereditary spherocytosis finding" in request


def test_expansion_repairs_entity_drift_once_then_rejects_repeat_failure() -> None:
    invalid = _expansion(
        "osmotic fragility test",
        "splenectomy indication",
        "EMA binding test",
    )
    valid = _expansion(
        "hereditary spherocytosis osmotic fragility",
        "hereditary spherocytosis splenectomy indication",
        "hereditary spherocytosis EMA binding test",
    )
    repaired = _service(ExpansionQueue((invalid, valid))).expand(
        _concept(),
        used_paraphrases=_concept().paraphrases,
        found_notes=(_note(1),),
        missing_facts=("EMA binding is diagnostic.",),
    )

    assert repaired.expansion == valid
    assert repaired.request_ids == ("expand-1", "expand-2")

    with pytest.raises(ConvergenceValidationError, match="primary entity"):
        _service(ExpansionQueue((invalid, invalid))).expand(
            _concept(),
            used_paraphrases=_concept().paraphrases,
            found_notes=(_note(1),),
            missing_facts=("EMA binding is diagnostic.",),
        )


def test_growth_uses_new_unique_notes_over_cumulative_seen_notes() -> None:
    update = update_growth(
        seen_note_ids=tuple(range(1, 53)),
        retrieved_note_ids=tuple(range(53, 101)),
    )

    assert update.new_note_ids == tuple(range(53, 101))
    assert len(update.seen_note_ids) == 100
    assert update.growth == 0.48
    assert update.converged is False

    stable = update_growth(
        seen_note_ids=update.seen_note_ids,
        retrieved_note_ids=(100, 101, 102, 103),
    )

    assert stable.new_note_ids == (101, 102, 103)
    assert stable.growth == pytest.approx(3 / 103)
    assert stable.converged is True
