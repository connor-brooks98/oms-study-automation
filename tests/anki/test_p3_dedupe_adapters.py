import asyncio
from collections.abc import Sequence

import numpy as np
import pytest

from oms_hub.anki.card_centric_contracts import SemanticDedupeReview
from oms_hub.anki.dedupe import (
    DeduplicationService,
    SemanticDedupeIntegrityError,
)
from oms_hub.anki.gaps import GapCardProposal
from oms_hub.anki.normalize import NormalizedNote
from oms_hub.anki.semantic.domain import FloatMatrix, InputType
from oms_hub.anki.semantic.voyage import VoyageEmbeddingError
from oms_hub.llm.domain import ProviderName


class FixedEmbedder:
    def __init__(self, vectors: object) -> None:
        self.vectors = vectors
        self.calls = 0

    async def embed(
        self,
        texts: Sequence[str],
        *,
        input_type: InputType,
    ) -> FloatMatrix:
        self.calls += 1
        assert input_type == "document"
        return self.vectors  # type: ignore[return-value]


class FailingEmbedder:
    def __init__(self, error: VoyageEmbeddingError) -> None:
        self.error = error

    async def embed(
        self,
        texts: Sequence[str],
        *,
        input_type: InputType,
    ) -> FloatMatrix:
        raise self.error


def _proposal(
    concept_id: str,
    text: str,
    *,
    card_id: str | None = None,
) -> GapCardProposal:
    provenance = {} if card_id is None else {"card_centric_generated_card_id": card_id}
    return GapCardProposal(
        concept_id=concept_id,
        note_type="Cloze",
        fields={"Text": text, "Extra": ""},
        source_refs=(),
        evidence_ids=("a" * 64,),
        initial_tags=("OMS::Generated",),
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        prompt_version="gap-v1",
        confidence=0.9,
        content_hash="b" * 64,
        provenance=provenance,
    )


def _note(note_id: int, text: str) -> NormalizedNote:
    return NormalizedNote(
        note_id=note_id,
        model_name="AnKingOverhaul",
        text=text,
        extra="",
        raw_fields={"Text": text, "Extra": ""},
        tags=(),
        card_ids=(),
        media=(),
        token_signature="",
        content_sha256=f"{note_id:064x}",
    )


@pytest.mark.parametrize(
    ("vectors", "message"),
    [
        (np.asarray([1.0, 0.0], dtype=np.float32), "rank two"),
        (np.asarray([[1.0, 0.0]], dtype=np.float32), "wrong row count"),
        (np.asarray([[1.0, 0.0], [np.nan, 1.0]], dtype=np.float32), "finite"),
        (np.asarray([[1.0, 0.0], [0.0, 0.0]], dtype=np.float32), "zero vectors"),
        ([[1.0, 0.0], [1.0]], "numeric rectangular matrix"),
        ([[1.0, "not-a-number"], [1.0, 0.0]], "numeric rectangular matrix"),
    ],
)
def test_invalid_semantic_vectors_are_explicit_integrity_failures(
    vectors: object,
    message: str,
) -> None:
    async def scenario() -> None:
        with pytest.raises(SemanticDedupeIntegrityError, match=message):
            await DeduplicationService(FixedEmbedder(vectors)).classify(
                _proposal("C01", "alpha beta"),
                [_note(1, "gamma delta")],
                [],
            )

    asyncio.run(scenario())


def test_provider_error_propagates_without_lexical_fallback() -> None:
    async def scenario() -> None:
        error = VoyageEmbeddingError("temporary provider outage")
        with pytest.raises(VoyageEmbeddingError) as raised:
            await DeduplicationService(FailingEmbedder(error)).classify(
                _proposal("C01", "alpha beta"),
                [_note(1, "gamma delta")],
                [],
            )
        assert raised.value is error

    asyncio.run(scenario())


def test_exact_duplicate_skips_embedding_and_keeps_existing_identity() -> None:
    async def scenario() -> None:
        embedder = FixedEmbedder(np.asarray([[0.0]], dtype=np.float32))
        result = await DeduplicationService(embedder).classify(
            _proposal("C01", "{{c1::Alpha}} beta"),
            [_note(42, "Alpha beta")],
            [],
        )
        assert result.disposition == "duplicate"
        assert result.nearest_matches[0].identifier == "note:42"
        assert embedder.calls == 0

    asyncio.run(scenario())


def test_semantic_duplicate_preserves_stable_generated_card_identity() -> None:
    async def scenario() -> None:
        first = _proposal("C01", "gamma delta", card_id="card-C01-M1-1")
        second = _proposal("C01", "alpha beta", card_id="card-C01-M1-2")
        result = await DeduplicationService(
            FixedEmbedder(np.asarray([[1.0, 0.0], [1.0, 0.0]], dtype=np.float32))
        ).classify(second, [], [first, second])
        assert result.disposition == "duplicate"
        assert result.nearest_matches[0].identifier == "proposal:card-C01-M1-1"

    asyncio.run(scenario())


def test_lexical_advisory_is_deterministic_and_never_declares_uniqueness() -> None:
    service = DeduplicationService(FixedEmbedder(np.empty((0, 0), dtype=np.float32)))
    advisory = service.lexical_advisory(
        _proposal("C01", "alpha beta gamma"),
        [_note(8, "alpha beta delta")],
        [_proposal("C02", "alpha beta epsilon", card_id="card-C02-M1-1")],
    )

    assert advisory.automatic_unique is False
    assert [candidate.identifier for candidate in advisory.candidates] == [
        "note:8",
        "proposal:card-C02-M1-1",
    ]
    assert [candidate.score for candidate in advisory.candidates] == [0.5, 0.5]


def test_exhausted_retry_adapter_requires_review_without_terminal_uniqueness() -> None:
    service = DeduplicationService(FixedEmbedder(np.empty((0, 0), dtype=np.float32)))
    advisory = service.lexical_advisory(
        _proposal("C01", "alpha beta"),
        [_note(8, "alpha gamma")],
        [],
    )

    review = service.exhausted_retry_review(
        card_id="card-C01-M1-1",
        fact_id="C01-M1",
        retry_exhausted=True,
        advisory=advisory,
    )

    assert review.retry_exhausted is True
    assert review.automatic_unique is False
    assert review.card_id == "card-C01-M1-1"
    assert review.fact_id == "C01-M1"
    assert isinstance(review, SemanticDedupeReview)
    assert review.lexical_candidates[0].identity.existing_note_id == 8
