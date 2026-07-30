import asyncio
from collections.abc import Sequence

import numpy as np

from oms_hub.anki.dedupe import DeduplicationService
from oms_hub.anki.gaps import GapCardProposal
from oms_hub.anki.normalize import NormalizedNote
from oms_hub.anki.semantic.domain import FloatMatrix, InputType
from oms_hub.llm.domain import ProviderName


class SimilarityEmbedder:
    async def embed(
        self,
        texts: Sequence[str],
        *,
        input_type: InputType,
    ) -> FloatMatrix:
        assert input_type == "document"
        rows = []
        for text in texts:
            lowered = text.casefold()
            if "reticulocyte" in lowered:
                rows.append([1.0, 0.0, 0.0])
            elif "reflects depleted" in lowered:
                rows.append([0.9, 0.435, 0.0])
            elif "ferritin" in lowered:
                rows.append([1.0, 0.0, 0.0])
            else:
                rows.append([0.0, 0.0, 1.0])
        return np.asarray(rows, dtype=np.float32)


def _proposal(
    concept_id: str,
    text: str,
    extra: str = "",
) -> GapCardProposal:
    return GapCardProposal(
        concept_id=concept_id,
        note_type="Cloze",
        fields={"Text": text, "Extra": extra},
        source_refs=(),
        evidence_ids=("a" * 64,),
        initial_tags=("OMS::Generated",),
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        prompt_version="gap-v1",
        confidence=0.9,
        content_hash="b" * 64,
        provenance={},
    )


def _note(note_id: int, text: str, extra: str = "") -> NormalizedNote:
    return NormalizedNote(
        note_id=note_id,
        model_name="AnKingOverhaul",
        text=text,
        extra=extra,
        raw_fields={"Text": text, "Extra": extra},
        tags=(),
        card_ids=(note_id + 100,),
        media=(),
        token_signature="",
        content_sha256=f"{note_id:064x}",
    )


def test_cloze_normalized_existing_note_duplicate() -> None:
    async def scenario() -> None:
        proposal = _proposal(
            "retic",
            "After iron, {{c1::reticulocytes rise}}.",
        )
        result = await DeduplicationService(
            SimilarityEmbedder()
        ).classify(
            proposal,
            [_note(1, "After iron, reticulocytes rise.")],
            [],
        )

        assert result.disposition == "duplicate"
        assert result.nearest_matches[0].identifier == "note:1"
        assert result.nearest_matches[0].exact is True

    asyncio.run(scenario())


def test_within_batch_duplicate_is_detected() -> None:
    async def scenario() -> None:
        first = _proposal(
            "retic-1",
            "{{c1::Reticulocytes}} rise after iron.",
        )
        second = _proposal(
            "retic-2",
            "Reticulocytes rise after iron.",
        )
        result = await DeduplicationService(
            SimilarityEmbedder()
        ).classify(second, [], [first])

        assert result.disposition == "duplicate"
        assert result.nearest_matches[0].identifier == "proposal:retic-1"

    asyncio.run(scenario())


def test_semantic_overlap_and_unique_proposals_are_distinguished() -> None:
    async def scenario() -> None:
        service = DeduplicationService(
            SimilarityEmbedder(),
            duplicate_threshold=0.98,
            overlap_threshold=0.85,
        )
        overlapping = await service.classify(
            _proposal("iron", "Ferritin is low in iron deficiency."),
            [_note(1, "Ferritin reflects depleted iron stores.")],
            [],
        )
        unique = await service.classify(
            _proposal("infection", "Staphylococcus causes abscesses."),
            [_note(1, "Ferritin reflects depleted iron stores.")],
            [],
        )

        assert overlapping.disposition == "overlap"
        assert overlapping.nearest_matches[0].score >= 0.85
        assert unique.disposition == "unique"

    asyncio.run(scenario())
