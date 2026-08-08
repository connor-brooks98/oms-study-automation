import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

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
                identifier=f"proposal:{other.concept_id}",
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
        vectors = np.asarray(
            await self.embedder.embed(
                [
                    proposed_text,
                    *(comparison.text for comparison in comparisons),
                ],
                input_type="document",
            ),
            dtype=np.float32,
        )
        if (
            vectors.ndim != 2
            or vectors.shape[0] != len(comparisons) + 1
            or not np.isfinite(vectors).all()
        ):
            raise ValueError("deduplication embeddings are invalid")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if np.any(norms == 0):
            raise ValueError(
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


def _proposal_text(proposal: GapCardProposal) -> str:
    return _normalize_card_text(
        proposal.fields.get("Text", ""),
        proposal.fields.get("Extra", ""),
    )


def _normalize_card_text(text: str, extra: str) -> str:
    unclozed = _CLOZE.sub(lambda match: match.group(1), text)
    normalized = normalize_html(f"{unclozed} {extra}").casefold()
    return " ".join(_TOKEN.findall(normalized))
