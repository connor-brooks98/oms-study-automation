import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

from oms_hub.anki.card_centric_contracts import (
    DedupeAdvisoryCandidate as ContractDedupeAdvisoryCandidate,
)
from oms_hub.anki.card_centric_contracts import SemanticDedupeReview
from oms_hub.anki.correction_contracts import DuplicateIdentity
from oms_hub.anki.gaps import GapCardProposal
from oms_hub.anki.normalize import NormalizedNote, normalize_html
from oms_hub.anki.semantic.domain import EmbeddingClient

_CLOZE = re.compile(
    r"\{\{c\d+::(.*?)(?:::[^{}]*?)?\}\}",
    re.IGNORECASE,
)
_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class DedupeMatch:
    identifier: str
    score: float
    exact: bool


@dataclass(frozen=True, slots=True)
class DeduplicationResult:
    disposition: Literal["unique", "overlap", "duplicate"]
    nearest_matches: tuple[DedupeMatch, ...]


class SemanticDedupeIntegrityError(ValueError):
    """Semantic-dedupe embeddings violate the required vector contract.

    This is intentionally distinct from provider exceptions (including
    ``VoyageEmbeddingError``).  Callers must allow provider exceptions to
    propagate to the worker retry policy, while this deterministic integrity
    failure blocks automatic deduplication.
    """


@dataclass(frozen=True, slots=True)
class LexicalDedupeCandidate:
    """A deterministic lexical comparison that cannot resolve a card."""

    identifier: str
    score: float
    exact: bool


@dataclass(frozen=True, slots=True)
class LexicalDedupeAdvisory:
    """Non-terminal lexical evidence for a semantic-dedupe outage."""

    candidates: tuple[LexicalDedupeCandidate, ...]
    automatic_unique: Literal[False] = False


@dataclass(frozen=True, slots=True)
class _Comparison:
    identifier: str
    text: str


class DeduplicationService:
    def __init__(
        self,
        embedder: EmbeddingClient,
        *,
        duplicate_threshold: float = 0.97,
        overlap_threshold: float = 0.86,
        nearest_limit: int = 5,
    ) -> None:
        if not (
            0 <= overlap_threshold < duplicate_threshold <= 1
            and nearest_limit >= 1
        ):
            raise ValueError("deduplication thresholds are invalid")
        self.embedder = embedder
        self.duplicate_threshold = duplicate_threshold
        self.overlap_threshold = overlap_threshold
        self.nearest_limit = nearest_limit

    async def classify(
        self,
        proposal: GapCardProposal,
        existing_notes: Sequence[NormalizedNote],
        batch: Sequence[GapCardProposal],
    ) -> DeduplicationResult:
        proposed_text = _proposal_text(proposal)
        comparisons = [
            _Comparison(
                identifier=f"note:{note.note_id}",
                text=_normalize_card_text(note.text, note.extra),
            )
            for note in existing_notes
        ] + [
            _Comparison(
                identifier=_proposal_identifier(other),
                text=_proposal_text(other),
            )
            for other in batch
            if other is not proposal
        ]
        exact = [
            DedupeMatch(
                identifier=comparison.identifier,
                score=1.0,
                exact=True,
            )
            for comparison in comparisons
            if comparison.text == proposed_text
        ]
        if exact:
            return DeduplicationResult(
                disposition="duplicate",
                nearest_matches=tuple(
                    sorted(exact, key=lambda match: match.identifier)[
                        : self.nearest_limit
                    ]
                ),
            )
        if not comparisons:
            return DeduplicationResult(
                disposition="unique",
                nearest_matches=(),
            )
        # Do not catch provider exceptions here.  In particular, a
        # VoyageEmbeddingError must reach the worker unchanged so its retry
        # policy remains effective.  Lexical evidence is requested only by the
        # explicit exhausted-retry adapter below.
        embedded = await self.embedder.embed(
            [
                proposed_text,
                *(comparison.text for comparison in comparisons),
            ],
            input_type="document",
        )
        try:
            vectors = np.asarray(embedded, dtype=np.float32)
        except (TypeError, ValueError) as exc:
            raise SemanticDedupeIntegrityError(
                "deduplication embeddings must be a numeric rectangular matrix"
            ) from exc
        _validate_embedding_vectors(vectors, expected_rows=len(comparisons) + 1)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if np.any(norms == 0):
            raise SemanticDedupeIntegrityError(
                "deduplication embeddings cannot contain zero vectors"
            )
        normalized = vectors / norms
        scores = normalized[1:] @ normalized[0]
        matches = sorted(
            (
                DedupeMatch(
                    identifier=comparison.identifier,
                    score=float(scores[index]),
                    exact=False,
                )
                for index, comparison in enumerate(comparisons)
            ),
            key=lambda match: (-match.score, match.identifier),
        )[: self.nearest_limit]
        highest = matches[0].score
        disposition: Literal["unique", "overlap", "duplicate"]
        if highest >= self.duplicate_threshold:
            disposition = "duplicate"
        elif highest >= self.overlap_threshold:
            disposition = "overlap"
        else:
            disposition = "unique"
        return DeduplicationResult(
            disposition=disposition,
            nearest_matches=tuple(matches),
        )

    def lexical_advisory(
        self,
        proposal: GapCardProposal,
        existing_notes: Sequence[NormalizedNote],
        batch: Sequence[GapCardProposal],
    ) -> LexicalDedupeAdvisory:
        """Return deterministic lexical evidence without a terminal decision."""
        proposed_text = _proposal_text(proposal)
        comparisons = _comparisons(proposal, existing_notes, batch)
        candidates = sorted(
            (
                LexicalDedupeCandidate(
                    identifier=comparison.identifier,
                    score=_lexical_similarity(proposed_text, comparison.text),
                    exact=comparison.text == proposed_text,
                )
                for comparison in comparisons
            ),
            key=lambda candidate: (-candidate.score, candidate.identifier),
        )[: self.nearest_limit]
        return LexicalDedupeAdvisory(candidates=tuple(candidates))

    def exhausted_retry_review(
        self,
        *,
        card_id: str,
        fact_id: str,
        advisory: LexicalDedupeAdvisory,
        retry_exhausted: bool = True,
    ) -> SemanticDedupeReview:
        """Adapt non-terminal lexical evidence to the P1/I0 retry seam.

        P1 calls this only after its worker retry budget is exhausted.  This
        service deliberately does not activate it during ``classify``.
        """
        return as_semantic_dedupe_review(
            card_id=card_id,
            fact_id=fact_id,
            retry_exhausted=retry_exhausted,
            advisory=advisory,
        )


def _comparisons(
    proposal: GapCardProposal,
    existing_notes: Sequence[NormalizedNote],
    batch: Sequence[GapCardProposal],
) -> list[_Comparison]:
    return [
        _Comparison(
            identifier=f"note:{note.note_id}",
            text=_normalize_card_text(note.text, note.extra),
        )
        for note in existing_notes
    ] + [
        _Comparison(
            identifier=_proposal_identifier(other),
            text=_proposal_text(other),
        )
        for other in batch
        if other is not proposal
    ]


def _validate_embedding_vectors(vectors: np.ndarray, *, expected_rows: int) -> None:
    if vectors.ndim != 2:
        raise SemanticDedupeIntegrityError("deduplication embeddings must be rank two")
    if vectors.shape[0] != expected_rows:
        raise SemanticDedupeIntegrityError("deduplication embeddings have the wrong row count")
    if not np.isfinite(vectors).all():
        raise SemanticDedupeIntegrityError("deduplication embeddings must be finite")


def _proposal_identifier(proposal: GapCardProposal) -> str:
    card_id = proposal.provenance.get("card_centric_generated_card_id")
    if isinstance(card_id, str) and card_id.strip():
        # The current S8 adapter preserves compatibility by passing
        # ``<concept_id>::<card_id>`` through the legacy concept field.  For
        # newer adapters, return the explicit stable card identity directly.
        legacy_identity = f"{proposal.concept_id}::{card_id}"
        if proposal.concept_id == legacy_identity:
            return f"proposal:{legacy_identity}"
        return f"proposal:{card_id}"
    return f"proposal:{proposal.concept_id}"


def _lexical_similarity(left: str, right: str) -> float:
    left_tokens = set(_TOKEN.findall(left))
    right_tokens = set(_TOKEN.findall(right))
    if not left_tokens and not right_tokens:
        return 1.0
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def as_semantic_dedupe_review(
    *,
    card_id: str,
    fact_id: str,
    advisory: LexicalDedupeAdvisory,
    retry_exhausted: bool = True,
) -> SemanticDedupeReview:
    """Convert local lexical evidence into P3-A's immutable review contract."""
    if not card_id.strip() or not fact_id.strip():
        raise ValueError("semantic dedupe review requires card_id and fact_id")
    if retry_exhausted is not True:
        raise ValueError("semantic dedupe review requires exhausted retries")
    return SemanticDedupeReview(
        card_id=card_id,
        fact_id=fact_id,
        retry_exhausted=True,
        lexical_candidates=tuple(
            ContractDedupeAdvisoryCandidate(
                card_id=card_id,
                fact_id=fact_id,
                identity=_advisory_identity(candidate.identifier),
                lexical_score=candidate.score,
            )
            for candidate in advisory.candidates
        ),
    )


def _advisory_identity(identifier: str) -> DuplicateIdentity:
    if identifier.startswith("note:"):
        try:
            note_id = int(identifier.removeprefix("note:"))
        except ValueError as exc:
            raise ValueError("lexical advisory has an invalid note identity") from exc
        return DuplicateIdentity(existing_note_id=note_id)
    if identifier.startswith("proposal:"):
        card_id = identifier.removeprefix("proposal:").strip()
        if card_id:
            return DuplicateIdentity(generated_card_id=card_id)
    raise ValueError("lexical advisory has an invalid generated-card identity")


def _proposal_text(proposal: GapCardProposal) -> str:
    return _normalize_card_text(
        proposal.fields.get("Text", ""),
        proposal.fields.get("Extra", ""),
    )


def _normalize_card_text(text: str, extra: str) -> str:
    unclozed = _CLOZE.sub(lambda match: match.group(1), text)
    normalized = normalize_html(f"{unclozed} {extra}").casefold()
    return " ".join(_TOKEN.findall(normalized))
