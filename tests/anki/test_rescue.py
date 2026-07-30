import asyncio
from collections.abc import Sequence

import pytest

from oms_hub.anki.domain import SourceKind
from oms_hub.anki.judgment import CoverageJudgment
from oms_hub.anki.lcl import LectureConcept, LedgerSourceRef
from oms_hub.anki.rescue import (
    LocalizationDecision,
    RescueService,
    SourceRevisionStale,
)
from oms_hub.anki.source_index import (
    SourceScope,
    SourceSearchHit,
)
from oms_hub.anki.sources import SourcePassage
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.structured import StructuredJSONResult


def _concept() -> LectureConcept:
    return LectureConcept(
        concept_id="iron-response",
        source_refs=(LedgerSourceRef(passage_id="a" * 64),),
        statement="Reticulocytes rise after iron replacement",
        hypothetical_card=(
            "After iron replacement, reticulocytes {{c1::increase}}"
        ),
        paraphrases=(
            "Marrow response following iron therapy",
            "Reticulocyte change after treating iron deficiency",
        ),
        importance="core",
    )


def _passage(
    revision_id: int,
    kind: SourceKind,
    text: str,
    locator: str,
) -> SourcePassage:
    return SourcePassage.create(
        revision_id=revision_id,
        lecture_id=12,
        artifact_id=f"upload-{revision_id}",
        source_kind=kind,
        locator=locator,
        text=text,
        slide_number=5 if kind is not SourceKind.TRANSCRIPT else None,
        start_seconds=30 if kind is SourceKind.TRANSCRIPT else None,
        end_seconds=45 if kind is SourceKind.TRANSCRIPT else None,
    )


def _hit(passage: SourcePassage, score: float = 0.03) -> SourceSearchHit:
    return SourceSearchHit(
        passage=passage,
        score=score,
        semantic_score=0.9,
        lexical_score=1.0,
        semantic_rank=1,
        lexical_rank=1,
    )


class FakeSourceIndex:
    def __init__(self, hits: Sequence[SourceSearchHit]) -> None:
        self.hits = list(hits)
        self.scopes: list[SourceScope] = []

    async def search(
        self,
        query: str,
        source_scope: SourceScope,
        *,
        limit: int,
    ) -> list[SourceSearchHit]:
        self.scopes.append(source_scope)
        return self.hits[:limit]


def _structured_result(
    decision: LocalizationDecision,
) -> StructuredJSONResult[LocalizationDecision]:
    return StructuredJSONResult(
        value=decision,
        raw_text=decision.model_dump_json(),
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        request_id="request-rescue",
        input_tokens=20,
        output_tokens=10,
        cost_microusd=5,
    )


class QueueStructured:
    def __init__(self, decisions: Sequence[LocalizationDecision]) -> None:
        self.decisions = list(decisions)

    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[LocalizationDecision],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[LocalizationDecision]:
        return _structured_result(self.decisions.pop(0))


def _service(
    hits: Sequence[SourceSearchHit],
    decision: LocalizationDecision,
) -> RescueService:
    return RescueService(
        FakeSourceIndex(hits),
        QueueStructured([decision]),
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        prompt_version="rescue-v1",
        candidate_limit=8,
    )


@pytest.mark.parametrize(
    "kind",
    [SourceKind.SLIDE, SourceKind.TRANSCRIPT],
)
def test_localizes_slide_only_or_transcript_only_evidence(
    kind: SourceKind,
) -> None:
    async def scenario() -> None:
        passage = _passage(
            7,
            kind,
            "Reticulocytes rise within days after iron replacement.",
            "slide:5" if kind is SourceKind.SLIDE else "transcript:1:30-45",
        )
        service = _service(
            [_hit(passage)],
            LocalizationDecision(
                support="supported",
                evidence_ids=(passage.passage_id,),
                rationale="The source explicitly states the response.",
            ),
        )

        localized = await service.localize(
            _concept(),
            SourceScope(revision_ids=(7,)),
        )
        queries = service.build_queries(localized)

        assert localized.support == "supported"
        assert localized.evidence == (passage,)
        assert len(queries) == 3
        assert {
            query.kind for query in queries
        } == {
            "source_statement",
            "terminology",
            "clinical_rephrase",
        }
        assert all(
            query.evidence_ids == (passage.passage_id,)
            for query in queries
        )

    asyncio.run(scenario())


def test_localization_can_fuse_slide_and_transcript_evidence() -> None:
    async def scenario() -> None:
        slide = _passage(
            7,
            SourceKind.SLIDE,
            "Reticulocytes rise after iron treatment.",
            "slide:5",
        )
        transcript = _passage(
            8,
            SourceKind.TRANSCRIPT,
            "The marrow response is a reticulocyte increase.",
            "transcript:1:30-45",
        )
        service = _service(
            [_hit(slide), _hit(transcript)],
            LocalizationDecision(
                support="supported",
                evidence_ids=(
                    slide.passage_id,
                    transcript.passage_id,
                ),
                rationale="Both sources corroborate the concept.",
            ),
        )

        localized = await service.localize(
            _concept(),
            SourceScope(revision_ids=(7, 8)),
        )

        assert {
            evidence.source_kind for evidence in localized.evidence
        } == {SourceKind.SLIDE, SourceKind.TRANSCRIPT}

    asyncio.run(scenario())


def test_unsupported_and_partial_localizations_have_honest_outcomes() -> None:
    async def scenario() -> None:
        passage = _passage(
            7,
            SourceKind.SLIDE,
            "Iron replacement is discussed without timing.",
            "slide:5",
        )
        partial_service = _service(
            [_hit(passage)],
            LocalizationDecision(
                support="partial",
                evidence_ids=(passage.passage_id,),
                rationale="Treatment appears, but response timing does not.",
            ),
        )
        partial = await partial_service.localize(
            _concept(),
            SourceScope(revision_ids=(7,)),
        )

        unsupported = await RescueService(
            FakeSourceIndex([]),
            QueueStructured([]),
            provider=ProviderName.OPENAI,
            model="gpt-5.2",
            prompt_version="rescue-v1",
        ).localize(_concept(), SourceScope(revision_ids=(7,)))

        assert partial.support == "partial"
        assert partial_service.finalize(
            partial,
            CoverageJudgment(
                status="missing",
                supporting_note_ids=(),
                missing_facts=("Timing is absent.",),
                rationale="No candidate covers timing.",
            ),
        ) == "unresolved_partial"
        assert unsupported.support == "unsupported"
        assert unsupported.evidence == ()
        assert partial_service.finalize(unsupported, None) == "unsupported"

    asyncio.run(scenario())


def test_stale_source_revision_is_rejected() -> None:
    async def scenario() -> None:
        stale = _passage(
            99,
            SourceKind.SLIDE,
            "Reticulocytes rise after treatment.",
            "slide:5",
        )
        service = _service(
            [_hit(stale)],
            LocalizationDecision(
                support="supported",
                evidence_ids=(stale.passage_id,),
                rationale="Explicit evidence.",
            ),
        )

        with pytest.raises(SourceRevisionStale):
            await service.localize(
                _concept(),
                SourceScope(revision_ids=(7,)),
            )

    asyncio.run(scenario())


def test_final_outcomes_distinguish_recovered_and_supported_gap() -> None:
    passage = _passage(
        7,
        SourceKind.SLIDE,
        "Reticulocytes rise after treatment.",
        "slide:5",
    )
    service = _service(
        [_hit(passage)],
        LocalizationDecision(
            support="supported",
            evidence_ids=(passage.passage_id,),
            rationale="Explicit evidence.",
        ),
    )

    async def scenario() -> None:
        localization = await service.localize(
            _concept(),
            SourceScope(revision_ids=(7,)),
        )
        assert service.finalize(
            localization,
            CoverageJudgment(
                status="covered",
                supporting_note_ids=(1,),
                missing_facts=(),
                rationale="Pass 2 recovered a covering note.",
            ),
        ) == "recovered"
        assert service.finalize(
            localization,
            CoverageJudgment(
                status="missing",
                supporting_note_ids=(),
                missing_facts=("No covering note exists.",),
                rationale="Candidates remain insufficient.",
            ),
        ) == "gap_supported"

    asyncio.run(scenario())
