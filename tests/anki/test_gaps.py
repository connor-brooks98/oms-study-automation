from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import BaseModel

from oms_hub.anki.domain import SourceKind
from oms_hub.anki.gaps import (
    CardDraft,
    EntailmentDecision,
    GapCardService,
    GapValidationError,
    SupportedGap,
)
from oms_hub.anki.lcl import LectureConcept, LedgerSourceRef
from oms_hub.anki.sources import SourcePassage
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.structured import StructuredJSONResult


def _concept() -> LectureConcept:
    return LectureConcept(
        concept_id="reticulocyte-response",
        source_refs=(LedgerSourceRef(passage_id="a" * 64),),
        statement="Reticulocytes rise after iron replacement",
        hypothetical_card=(
            "After iron replacement, reticulocytes {{c1::increase}}"
        ),
        paraphrases=(
            "Marrow response after iron therapy",
            "Reticulocyte change after treating deficiency",
        ),
        importance="core",
    )


def _evidence() -> SourcePassage:
    return SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="upload-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:5",
        text=(
            "Reticulocytes rise within several days after iron "
            "replacement, reflecting marrow response."
        ),
        slide_number=5,
    )


def _draft(
    evidence_id: str,
    *,
    text: str = (
        "After iron replacement, {{c1::reticulocytes rise}} "
        "within several days."
    ),
    extra: str = "This reflects the marrow response.",
) -> CardDraft:
    return CardDraft(
        note_type="Cloze",
        text=text,
        extra=extra,
        evidence_ids=(evidence_id,),
        confidence=0.94,
    )


class QueueStructured:
    def __init__(self, values: Sequence[BaseModel | Exception]) -> None:
        self.values = list(values)
        self.calls: list[type[BaseModel]] = []

    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[BaseModel],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[Any]:
        self.calls.append(output_model)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return StructuredJSONResult(
            value=value,
            raw_text=value.model_dump_json(),
            provider=provider,
            model=model,
            request_id=f"request-{len(self.calls)}",
            input_tokens=20,
            output_tokens=10,
            cost_microusd=5,
        )


def _service(*values: BaseModel | Exception) -> GapCardService:
    return GapCardService(
        QueueStructured(values),  # type: ignore[arg-type]
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        prompt_version="gap-v1",
    )


def _gap() -> SupportedGap:
    evidence = _evidence()
    return SupportedGap(
        concept=_concept(),
        evidence=(evidence,),
        initial_tags=("OMS::Generated", "OMS::Lecture_5"),
    )


def test_valid_card_has_complete_grounded_provenance() -> None:
    gap = _gap()
    service = _service(
        _draft(gap.evidence[0].passage_id),
        EntailmentDecision(
            status="supported",
            rationale="Every claim appears in the cited slide.",
        ),
    )

    result = service.generate(gap)

    assert result.status == "proposed"
    assert result.proposal is not None
    assert result.proposal.note_type == "Cloze"
    assert result.proposal.evidence_ids == (
        gap.evidence[0].passage_id,
    )
    assert result.proposal.initial_tags == gap.initial_tags
    assert result.proposal.provider is ProviderName.OPENAI
    assert len(result.proposal.content_hash) == 64
    assert result.proposal.source_refs[0].locator == "slide:5"


@pytest.mark.parametrize("status", ["not_supported", "contradicted"])
def test_unsupported_or_contradicted_answer_is_rejected(
    status: str,
) -> None:
    gap = _gap()
    result = _service(
        _draft(gap.evidence[0].passage_id),
        EntailmentDecision(
            status=status,  # type: ignore[arg-type]
            rationale="The answer adds a claim absent from the slide.",
        ),
    ).generate(gap)

    assert result.status == "rejected"
    assert result.proposal is None


def test_uncertain_entailment_goes_to_unresolved_review() -> None:
    gap = _gap()
    result = _service(
        _draft(gap.evidence[0].passage_id),
        EntailmentDecision(
            status="uncertain",
            rationale="The timing language is ambiguous.",
        ),
    ).generate(gap)

    assert result.status == "unresolved"
    assert result.proposal is None


def test_absent_citation_is_rejected_before_entailment() -> None:
    gap = _gap()
    service = _service(_draft("f" * 64))

    with pytest.raises(GapValidationError, match="evidence"):
        service.generate(gap)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("No cloze appears here.", "cloze"),
        ("{{c2::Starts at two}}", "number"),
        ("{{c1::Unsafe}}<script>alert(1)</script>", "HTML"),
        (
            "{{c1::Ferritin}} is low; ferritin confirms deficiency.",
            "leak",
        ),
    ],
)
def test_deterministic_card_validation_blocks_unsafe_drafts(
    text: str,
    message: str,
) -> None:
    gap = _gap()
    service = _service(
        _draft(gap.evidence[0].passage_id, text=text)
    )

    with pytest.raises(GapValidationError, match=message):
        service.generate(gap)
