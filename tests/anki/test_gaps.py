import json
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import BaseModel

from oms_hub.anki.domain import SourceKind
from oms_hub.anki.gaps import (
    CardDraft,
    EntailmentDecision,
    GapBatchV2,
    GapCardService,
    GapValidationError,
    SupportedGap,
    V2GapGenerationRequest,
    V2GapGenerationService,
)
from oms_hub.anki.lcl import LectureConcept, LedgerSourceRef
from oms_hub.anki.sources import SourcePassage
from oms_hub.anki.v2_contracts import (
    GeneratedGapCardV2,
    MissingFactV2,
    UnresolvedGapV2,
)
from oms_hub.llm.domain import GeneratedText, ProviderName
from oms_hub.llm.openai import openai_output_schema
from oms_hub.llm.structured import (
    StructuredJSONResult,
    StructuredOutputError,
)


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
        self.requests: list[tuple[str, str]] = []

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
        self.requests.append((instruction, input_text))
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


def _v2_concept() -> LectureConcept:
    evidence = _evidence()
    return LectureConcept(
        concept_id="C01",
        source_refs=(LedgerSourceRef(passage_id=evidence.passage_id),),
        statement="Reticulocytes rise after iron replacement",
        hypothetical_card="After iron replacement, reticulocytes rise",
        paraphrases=(
            "iron replacement reticulocyte response",
            "iron therapy marrow response",
            "iron deficiency treatment reticulocytes",
        ),
        importance="high",
        primary_entity="iron deficiency",
        aliases=("IDA",),
        depth="deep",
        emphasis_flag=True,
        source_passage_ids=(evidence.source_id,),
    )


def _v2_request() -> V2GapGenerationRequest:
    evidence = _evidence()
    return V2GapGenerationRequest(
        concept=_v2_concept(),
        missing_facts=(
            MissingFactV2(
                fact_id="C01-M1",
                statement="Reticulocytes rise after iron replacement.",
                passage_ids=(evidence.source_id,),
            ),
            MissingFactV2(
                fact_id="C01-M2",
                statement="The rise reflects marrow response.",
                passage_ids=(evidence.source_id,),
            ),
        ),
        evidence=(evidence,),
        lecture_title="Iron Deficiency Anemia",
        lecture_entity_count=1,
        forbidden_cloze_targets=("Iron Deficiency Anemia", "iron deficiency"),
        existing_supports=(),
        initial_tags=("OMS::Generated",),
    )


def _generated_v2(fact_id: str, source_id: str) -> GeneratedGapCardV2:
    return GeneratedGapCardV2(
        fact_id=fact_id,
        status="generated",
        text="After iron replacement, {{c1::<b>reticulocytes rise</b>}}.",
        extra="This change reflects marrow response.",
        note_type="AnKingOverhaul (AnKing Step Deck / AnKingMed)",
        source_passage_ids=(source_id,),
        split=False,
        image_needed=None,
    )


def test_v2_gap_schema_requires_every_openai_strict_object_property() -> None:
    schema = openai_output_schema(GapBatchV2.model_json_schema())
    missing: list[str] = []

    def inspect(value: object, path: str = "$") -> None:
        if isinstance(value, dict):
            properties = value.get("properties")
            if isinstance(properties, dict):
                required = value.get("required")
                required_names = set(required) if isinstance(required, list) else set()
                missing.extend(
                    f"{path}.{name}"
                    for name in properties
                    if name not in required_names
                )
            for name, child in value.items():
                inspect(child, f"{path}.{name}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                inspect(child, f"{path}[{index}]")

    inspect(schema)

    assert missing == []


def test_v2_generation_sends_all_missing_facts_in_one_concept_call() -> None:
    request = _v2_request()
    structured = QueueStructured(
        (
            GapBatchV2(
                resolutions=(
                    _generated_v2("C01-M1", request.evidence[0].source_id),
                    UnresolvedGapV2(
                        fact_id="C01-M2",
                        status="unresolved",
                        reason="The source does not support an atomic card.",
                        duplicate_of_note_id=None,
                    ),
                )
            ),
        )
    )
    service = V2GapGenerationService(
        structured,  # type: ignore[arg-type]
        provider=ProviderName.OPENAI,
        model="gpt-5.6-terra",
        prompt_version="gap-card-generation",
        prompt_text="# Generate all audited gaps",
        prompt_hash="abcdef123456",
    )

    result = service.generate(request)

    assert len(structured.requests) == 1
    sent = json.loads(structured.requests[0][1])
    assert [item["fact_id"] for item in sent["missing_facts"]] == [
        "C01-M1",
        "C01-M2",
    ]
    assert sent["lecture_entity_count"] == 1
    assert sent["forbidden_cloze_targets"] == [
        "Iron Deficiency Anemia",
        "iron deficiency",
    ]
    assert [item.fact_id for item in result.generated] == ["C01-M1"]
    assert [item.fact_id for item in result.unresolved] == ["C01-M2"]


def test_v2_generation_repairs_a_silently_omitted_fact() -> None:
    request = _v2_request()
    structured = QueueStructured(
        (
            GapBatchV2(
                resolutions=(
                    _generated_v2("C01-M1", request.evidence[0].source_id),
                )
            ),
            GapBatchV2(
                resolutions=(
                    _generated_v2("C01-M1", request.evidence[0].source_id),
                    UnresolvedGapV2(
                        fact_id="C01-M2",
                        status="unresolved",
                        reason="Evidence is insufficient.",
                        duplicate_of_note_id=None,
                    ),
                )
            ),
        )
    )
    service = V2GapGenerationService(
        structured,  # type: ignore[arg-type]
        provider=ProviderName.OPENAI,
        model="gpt-5.6-terra",
        prompt_version="gap-card-generation",
        prompt_text="# Generate all audited gaps",
        prompt_hash="abcdef123456",
    )

    result = service.generate(request)

    assert len(structured.requests) == 2
    repair = json.loads(structured.requests[1][1])
    assert "every missing fact" in repair["validation_error"]
    assert "missing_fact_ids=[C01-M2]" in repair["validation_error"]
    assert {item.fact_id for item in (*result.generated, *result.unresolved)} == {
        "C01-M1",
        "C01-M2",
    }


def test_v2_request_rejects_missing_fact_evidence_not_in_bundle() -> None:
    request = _v2_request()
    invalid_fact = request.missing_facts[0].model_copy(
        update={"passage_ids": ("SLD:12:0099",)}
    )

    with pytest.raises(ValueError, match="missing-fact evidence"):
        V2GapGenerationRequest(
            concept=request.concept,
            missing_facts=(invalid_fact, request.missing_facts[1]),
            evidence=request.evidence,
            lecture_title=request.lecture_title,
            lecture_entity_count=request.lecture_entity_count,
            forbidden_cloze_targets=request.forbidden_cloze_targets,
            existing_supports=request.existing_supports,
            initial_tags=request.initial_tags,
        )


def test_v2_generation_rejects_forbidden_cloze_target_after_repair() -> None:
    request = _v2_request()
    trapped = GeneratedGapCardV2(
        fact_id="C01-M1",
        status="generated",
        text="{{c1::<b>iron deficiency</b>}} causes microcytic anemia.",
        extra="Iron replacement corrects the deficiency.",
        note_type="AnKingOverhaul (AnKing Step Deck / AnKingMed)",
        source_passage_ids=(request.evidence[0].source_id,),
        split=False,
        image_needed=None,
    )
    service = V2GapGenerationService(
        QueueStructured(  # type: ignore[arg-type]
            (
                GapBatchV2(
                    resolutions=(
                        trapped,
                        UnresolvedGapV2(
                            fact_id="C01-M2",
                            status="unresolved",
                            reason="Evidence is insufficient.",
                            duplicate_of_note_id=None,
                        ),
                    )
                ),
                GapBatchV2(
                    resolutions=(
                        trapped,
                        UnresolvedGapV2(
                            fact_id="C01-M2",
                            status="unresolved",
                            reason="Evidence is insufficient.",
                            duplicate_of_note_id=None,
                        ),
                    )
                ),
            )
        ),
        provider=ProviderName.OPENAI,
        model="gpt-5.6-terra",
        prompt_version="gap-card-generation",
        prompt_text="# Generate all audited gaps",
        prompt_hash="abcdef123456",
    )

    with pytest.raises(GapValidationError, match="forbidden cloze target"):
        service.generate(request)


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


def test_uses_resolved_markdown_prompt_for_card_generation() -> None:
    gap = _gap()
    structured = QueueStructured(
        (
            _draft(gap.evidence[0].passage_id),
            EntailmentDecision(
                status="supported",
                rationale="Every claim appears in the cited slide.",
            ),
        )
    )
    service = GapCardService(
        structured,
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        prompt_version="gap-card-generation",
        prompt_text="# Gap generation\n\nWrite grounded cards.",
        prompt_hash="abcdef123456",
    )

    result = service.generate(gap)

    assert result.proposal is not None
    assert result.proposal.prompt_hash == "abcdef123456"
    assert structured.requests[0][0] == (
        "# Gap generation\n\nWrite grounded cards."
    )


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
    invalid = _draft("f" * 64)
    service = _service(invalid, invalid)

    with pytest.raises(GapValidationError, match="evidence"):
        service.generate(gap)


def test_duplicate_card_evidence_ids_are_normalized() -> None:
    gap = _gap()
    evidence_id = gap.evidence[0].passage_id
    result = _service(
        CardDraft(
            note_type="Cloze",
            text="After iron replacement, {{c1::reticulocytes rise}}.",
            extra="This reflects the marrow response.",
            evidence_ids=(evidence_id, evidence_id),
            confidence=0.94,
        ),
        EntailmentDecision(
            status="supported",
            rationale="Every claim appears in the cited slide.",
        ),
    ).generate(gap)

    assert result.proposal is not None
    assert result.proposal.evidence_ids == (evidence_id,)


def test_gap_enums_are_case_and_whitespace_tolerant() -> None:
    draft = CardDraft(
        note_type=" cloze ",  # type: ignore[arg-type]
        text="{{c1::Reticulocytes}} rise.",
        extra="",
        evidence_ids=("passage-1",),
        confidence=0.9,
    )
    entailment = EntailmentDecision(
        status=" Supported ",  # type: ignore[arg-type]
        rationale="The source supports it.",
    )

    assert draft.note_type == "Cloze"
    assert entailment.status == "supported"


def test_invalid_card_draft_gets_one_repair() -> None:
    gap = _gap()
    service = _service(
        _draft("f" * 64),
        _draft(gap.evidence[0].passage_id),
        EntailmentDecision(
            status="supported",
            rationale="Every claim appears in the cited slide.",
        ),
    )

    result = service.generate(gap)

    assert result.status == "proposed"
    assert len(service.structured.requests) == 3  # type: ignore[attr-defined]
    repair_instruction, repair_input = service.structured.requests[1]  # type: ignore[attr-defined]
    assert "repair" in repair_instruction.casefold()
    assert "unavailable evidence" in repair_input


def test_malformed_entailment_gets_one_repair() -> None:
    gap = _gap()
    malformed = StructuredOutputError(
        "structured output failed JSON schema validation",
        raw_text='{"status":',
        generation=GeneratedText(
            text='{"status":',
            provider=ProviderName.OPENAI,
            model="gpt-5.2",
            request_id="request-malformed",
            input_tokens=20,
            output_tokens=4,
            cost_microusd=2,
        ),
    )
    service = _service(
        _draft(gap.evidence[0].passage_id),
        malformed,
        EntailmentDecision(
            status="supported",
            rationale="Every claim appears in the cited slide.",
        ),
    )

    result = service.generate(gap)

    assert result.status == "proposed"
    assert "repair" in service.structured.requests[2][0].casefold()  # type: ignore[attr-defined]


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
    invalid = _draft(gap.evidence[0].passage_id, text=text)
    service = _service(invalid, invalid)

    with pytest.raises(GapValidationError, match=message):
        service.generate(gap)
