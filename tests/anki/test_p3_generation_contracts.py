import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from oms_hub.anki.card_centric_contracts import (
    DedupeAdvisoryCandidate,
    QualitySelectionResult,
    SemanticDedupeReview,
)
from oms_hub.anki.correction_contracts import (
    CanonicalJsonObject,
    DuplicateIdentity,
    EvidenceQuality,
    FactForbiddenClozeMap,
    FactForbiddenClozeTargets,
    MarginalValueReason,
    PinnedLectureMetadata,
    SelectionMetadata,
    SelectionTier,
)
from oms_hub.anki.domain import SourceKind
from oms_hub.anki.gaps import (
    GapBatchV2,
    GapValidationError,
    V2GapGenerationRequest,
    V2GapGenerationService,
)
from oms_hub.anki.lcl import LectureConcept, LedgerSourceRef
from oms_hub.anki.sources import SourcePassage
from oms_hub.anki.v2_contracts import (
    GeneratedGapCardV2,
    LegacySplitIndexRecomputationRequired,
    MissingFactV2,
    UnresolvedGapV2,
    adapt_legacy_split_indices,
)
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.structured import StructuredJSONResult


class QueueStructured:
    def __init__(self, values: Sequence[BaseModel]) -> None:
        self.values = list(values)
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
        self.requests.append((instruction, input_text))
        value = self.values.pop(0)
        return StructuredJSONResult(
            value=value,
            raw_text=value.model_dump_json(),
            provider=provider,
            model=model,
            request_id="request-1",
            input_tokens=1,
            output_tokens=1,
            cost_microusd=1,
        )


def _evidence() -> SourcePassage:
    return SourcePassage.create(
        revision_id=1,
        lecture_id=1,
        artifact_id="source",
        source_kind=SourceKind.SLIDE,
        locator="slide:1",
        text="Alpha, beta, and gamma are source-supported facts.",
        slide_number=1,
    )


def _request() -> V2GapGenerationRequest:
    evidence = _evidence()
    return V2GapGenerationRequest(
        concept=LectureConcept(
            concept_id="C01",
            source_refs=(LedgerSourceRef(passage_id=evidence.passage_id),),
            statement="Alpha, beta, and gamma are source-supported facts.",
            hypothetical_card="Alpha is {{c1::important}}.",
            paraphrases=("alpha fact", "beta fact", "gamma fact"),
            importance="high",
            primary_entity="alpha",
            aliases=("a",),
            depth="deep",
            emphasis_flag=True,
            source_passage_ids=(evidence.source_id,),
        ),
        missing_facts=(
            MissingFactV2(fact_id="C01-M1", statement="Alpha.", passage_ids=(evidence.source_id,)),
            MissingFactV2(fact_id="C01-M2", statement="Beta.", passage_ids=(evidence.source_id,)),
            MissingFactV2(fact_id="C01-M3", statement="Gamma.", passage_ids=(evidence.source_id,)),
        ),
        evidence=(evidence,),
        lecture_title="Pinned title seam",
        lecture_entity_count=1,
        forbidden_cloze_targets_by_fact=FactForbiddenClozeMap(
            facts=(
                FactForbiddenClozeTargets(fact_id="C01-M1", targets=("alpha",)),
                FactForbiddenClozeTargets(fact_id="C01-M2", targets=("beta",)),
                FactForbiddenClozeTargets(fact_id="C01-M3", targets=("gamma",)),
            )
        ),
        existing_supports=(),
        initial_tags=("OMS::Generated",),
    )


def _generated(
    fact_id: str,
    source_id: str,
    *,
    text: str = "{{c1::<b>delta</b>}} is source-supported.",
    split: bool = False,
    split_index: int | None = None,
) -> GeneratedGapCardV2:
    return GeneratedGapCardV2(
        fact_id=fact_id,
        text=text,
        extra="Grounded explanation.",
        note_type="AnKingOverhaul (AnKing Step Deck / AnKingMed)",
        source_passage_ids=(source_id,),
        split=split,
        split_index=split_index,
        image_needed=None,
    )


def _service(structured: QueueStructured) -> V2GapGenerationService:
    return V2GapGenerationService(
        structured,  # type: ignore[arg-type]
        provider=ProviderName.OPENAI,
        model="gpt-5.6-terra",
        prompt_version="gap-card-generation",
        prompt_text="quality-first test prompt",
        prompt_hash="abcdef123456",
    )


def test_three_fact_generation_uses_per_fact_targets_and_sequential_splits() -> None:
    request = _request()
    source_id = request.evidence[0].source_id
    batch = GapBatchV2(
        resolutions=(
            _generated(
                "C01-M1",
                source_id,
                text="{{c1::<b>beta</b>}} remains allowed for M1.",
                split=True,
                split_index=1,
            ),
            _generated("C01-M1", source_id, split=True, split_index=2),
            _generated("C01-M2", source_id),
            UnresolvedGapV2(fact_id="C01-M3", reason="No atomic grounded card."),
        )
    )
    structured = QueueStructured((batch,))

    result = _service(structured).generate(request)

    payload = json.loads(structured.requests[0][1])
    assert [fact["fact_id"] for fact in payload["missing_facts"]] == [
        "C01-M1",
        "C01-M2",
        "C01-M3",
    ]
    assert "forbidden_cloze_targets" not in payload
    assert payload["forbidden_cloze_targets_by_fact"] == [
        {"fact_id": "C01-M1", "targets": ["alpha"]},
        {"fact_id": "C01-M2", "targets": ["beta"]},
        {"fact_id": "C01-M3", "targets": ["gamma"]},
    ]
    assert [proposal.split_index for proposal in result.proposals] == [1, 2, None]

    invalid = batch.model_copy(
        update={
            "resolutions": (
                _generated("C01-M1", source_id, split=True, split_index=1),
                _generated("C01-M1", source_id, split=True, split_index=3),
                _generated("C01-M2", source_id),
                UnresolvedGapV2(fact_id="C01-M3", reason="No atomic grounded card."),
            )
        }
    )
    with pytest.raises(GapValidationError, match="sequential split_index"):
        V2GapGenerationService._validate(invalid, request)


def test_legacy_split_adapter_is_stable_and_rejects_ambiguous_partial_indices() -> None:
    source_id = _evidence().source_id
    legacy = (
        _generated("C01-M1", source_id, split=True),
        _generated("C01-M1", source_id, split=True),
    )

    assert [card.split_index for card in adapt_legacy_split_indices(legacy)] == [1, 2]
    with pytest.raises(LegacySplitIndexRecomputationRequired, match="partial"):
        adapt_legacy_split_indices(
            (
                _generated("C01-M1", source_id, split=True, split_index=1),
                _generated("C01-M1", source_id, split=True),
            )
        )
    with pytest.raises(LegacySplitIndexRecomputationRequired, match="unsplit"):
        adapt_legacy_split_indices(
            (
                _generated("C01-M2", source_id),
                _generated("C01-M2", source_id),
            )
        )


def test_pinned_metadata_seam_rejects_mutable_title_or_lecture_identity() -> None:
    request = _request()
    metadata = CanonicalJsonObject.from_mapping({"exam": "block-1"})
    payload = {
        "lecture_id": 1,
        "title": request.lecture_title,
        "metadata": metadata.as_dict(),
    }
    pinned = PinnedLectureMetadata(
        lecture_id=1,
        title=request.lecture_title,
        metadata=metadata,
        metadata_sha256=hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )

    assert replace(request, lecture_id=1, pinned_lecture_metadata=pinned).lecture_id == 1
    with pytest.raises(ValueError, match="request lecture identity"):
        replace(request, lecture_id=2, pinned_lecture_metadata=pinned)
    with pytest.raises(ValueError, match="request lecture title"):
        replace(
            request,
            lecture_id=1,
            lecture_title="Edited after S1",
            pinned_lecture_metadata=pinned,
        )


def _selection_metadata(identity: str, position: int) -> SelectionMetadata:
    return SelectionMetadata(
        identity=identity,
        selected_position=position,
        tier=SelectionTier.T1,
        evidence_quality=EvidenceQuality.PRIMARY_SOURCE,
        marginal_value_reason=(
            MarginalValueReason.ONLY_VALID_REQUIRED_FACT if position >= 66 else None
        ),
    )


def test_semantic_review_and_selection_partitions_are_non_terminal_and_exact() -> None:
    advisory = DedupeAdvisoryCandidate(
        card_id="G02",
        fact_id="C01-M2",
        identity=DuplicateIdentity(existing_note_id=9),
        lexical_score=0.7,
    )
    review = SemanticDedupeReview(
        card_id="G02",
        fact_id="C01-M2",
        lexical_candidates=(advisory,),
    )
    assert review.automatic_unique is False
    with pytest.raises(ValidationError, match="less than or equal to 1"):
        DedupeAdvisoryCandidate(
            card_id="G02",
            fact_id="C01-M2",
            identity=DuplicateIdentity(existing_note_id=9),
            lexical_score=1.01,
        )


def test_over_cap_selection_requires_the_actual_overflow_acknowledgement() -> None:
    candidate_ids = tuple(f"G{position:02d}" for position in range(1, 72))
    metadata = tuple(
        SelectionMetadata(
            identity=f"generated:{card_id}",
            selected_position=position,
            tier=SelectionTier.T1,
            evidence_quality=EvidenceQuality.PRIMARY_SOURCE,
            mandatory=position == 71,
            marginal_value_reason=(
                MarginalValueReason.ONLY_VALID_REQUIRED_FACT if position >= 66 else None
            ),
            overflow_reason="Required fact coverage." if position == 71 else None,
            manual_acknowledgement_required=position == 71,
        )
        for position, card_id in enumerate(candidate_ids, start=1)
    )
    kwargs = {
        "existing_candidate_note_ids": (),
        "generated_candidate_card_ids": candidate_ids,
        "selected_existing_note_ids": (),
        "selected_generated_card_ids": candidate_ids,
        "excluded_existing_note_ids": (),
        "excluded_generated_card_ids": (),
        "selection_metadata": metadata,
        "below_warning_floor": False,
        "target": 65,
        "cap": 70,
        "minimum_target": 60,
        "mandatory_generated_card_ids": ("G71",),
    }
    with pytest.raises(ValidationError, match="overflow acknowledgement"):
        QualitySelectionResult(**kwargs)

    result = QualitySelectionResult(
        **kwargs,
        overflow_acknowledgement=CanonicalJsonObject.from_mapping(
            {
                "acknowledged_by": "reviewer",
                "acknowledged_at": "2026-08-08T18:00:00Z",
                "reason": "required coverage",
            }
        ),
    )
    assert result.overflow_acknowledgement is not None

    result = QualitySelectionResult(
        existing_candidate_note_ids=(101, 102),
        generated_candidate_card_ids=("G01", "G02"),
        selected_existing_note_ids=(101,),
        selected_generated_card_ids=("G01",),
        excluded_existing_note_ids=(102,),
        excluded_generated_card_ids=("G02",),
        selection_metadata=(
            _selection_metadata("existing:101", 1),
            _selection_metadata("generated:G01", 2),
        ),
        below_warning_floor=True,
        target=65,
        cap=70,
        minimum_target=60,
        mandatory_note_ids=(101,),
        mandatory_generated_card_ids=("G01",),
        semantic_review_required_card_ids=("G02",),
    )
    assert result.semantic_review_required_card_ids == ("G02",)
    with pytest.raises(ValidationError, match="never selected"):
        QualitySelectionResult(
            **{
                **result.model_dump(),
                "selected_generated_card_ids": ("G01", "G02"),
                "excluded_generated_card_ids": (),
                "selection_metadata": (
                    _selection_metadata("existing:101", 1),
                    _selection_metadata("generated:G01", 2),
                    _selection_metadata("generated:G02", 3),
                ),
            }
        )
