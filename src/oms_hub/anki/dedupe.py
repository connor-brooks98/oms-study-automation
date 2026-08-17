import re
from collections.abc import Mapping, Sequence
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
from oms_hub.anki.semantic.domain import EmbeddingClient, FloatMatrix

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
class V3DedupeProposal:
    """The small R10 surface; deliberately independent of legacy gap proposals."""

    card_id: str
    fact_id: str
    text: str
    extra: str


@dataclass(frozen=True, slots=True)
class V3DedupeResult:
    card_id: str
    disposition: Literal["generated", "duplicate", "overlap"]
    duplicate_of: str | None
    nearest_matches: tuple[DedupeMatch, ...]
    missing_existing_vector_note_ids: tuple[int, ...]


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
        if not (0 <= overlap_threshold < duplicate_threshold <= 1 and nearest_limit >= 1):
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
        *,
        existing_document_vectors: Mapping[int, FloatMatrix] | None = None,
    ) -> DeduplicationResult:
        proposed_text = _proposal_text(proposal)
        existing_comparisons = [
            _Comparison(
                identifier=f"note:{note.note_id}",
                text=_normalize_card_text(note.text, note.extra),
            )
            for note in existing_notes
        ]
        proposal_comparisons = [
            _Comparison(
                identifier=_proposal_identifier(other),
                text=_proposal_text(other),
            )
            for other in batch
            if other is not proposal
        ]
        comparisons = existing_comparisons + proposal_comparisons
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
                    sorted(exact, key=lambda match: match.identifier)[: self.nearest_limit]
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
        if existing_document_vectors is None:
            embedded = await self.embedder.embed(
                [
                    proposed_text,
                    *(comparison.text for comparison in comparisons),
                ],
                input_type="document",
            )
        else:
            embedded = await self.embedder.embed(
                [
                    _proposal_document_text(proposal),
                    *(_proposal_document_text(other) for other in batch if other is not proposal),
                ],
                input_type="document",
            )
        try:
            vectors = np.asarray(embedded, dtype=np.float32)
        except (TypeError, ValueError) as exc:
            raise SemanticDedupeIntegrityError(
                "deduplication embeddings must be a numeric rectangular matrix"
            ) from exc
        if existing_document_vectors is not None:
            existing_vectors = _pinned_existing_document_vectors(
                existing_notes,
                existing_document_vectors,
            )
            if vectors.ndim != 2 or vectors.shape[1] != existing_vectors.shape[1]:
                raise SemanticDedupeIntegrityError(
                    "pinned existing document vectors have incompatible dimensions"
                )
            vectors = np.concatenate(
                (vectors[:1], existing_vectors, vectors[1:]),
                axis=0,
            )
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

    async def classify_v3_batch(
        self,
        proposals: Sequence[V3DedupeProposal],
        existing_notes: Sequence[NormalizedNote],
        *,
        existing_document_vectors: Mapping[int, FloatMatrix],
    ) -> tuple[V3DedupeResult, ...]:
        """Deduplicate R9 cards once, in pinned document space where available.

        Existing cards without a frozen vector are retained for exact comparison
        and explicitly surfaced as exact-only diagnostics; they are never
        silently embedded again.
        """
        ordered = tuple(proposals)
        identities = tuple(_v3_proposal_order(item) for item in ordered)
        if identities != tuple(sorted(identities)) or len(identities) != len(set(identities)):
            raise ValueError("R10 proposals must be sorted and unique by fact/card ID")
        note_ids = [note.note_id for note in existing_notes]
        if len(note_ids) != len(set(note_ids)):
            raise SemanticDedupeIntegrityError("R10 existing note identities are ambiguous")
        if not ordered:
            return ()
        existing = tuple(sorted(existing_notes, key=lambda item: item.note_id))
        missing = tuple(
            note.note_id for note in existing if note.note_id not in existing_document_vectors
        )
        exact_results: dict[str, V3DedupeResult] = {}
        deferred_exact: dict[str, str] = {}
        pending: list[V3DedupeProposal] = []
        for proposal in ordered:
            text = _normalize_card_text(proposal.text, proposal.extra)
            exact_existing = sorted(
                [
                    DedupeMatch(f"note:{note.note_id}", 1.0, True)
                    for note in existing
                    if _normalize_card_text(note.text, note.extra) == text
                ],
                key=_v3_match_order,
            )
            if exact_existing:
                exact_results[proposal.card_id] = V3DedupeResult(
                    proposal.card_id,
                    "duplicate",
                    exact_existing[0].identifier,
                    tuple(exact_existing[: self.nearest_limit]),
                    missing,
                )
            elif exact_pending := next(
                (
                    previous
                    for previous in pending
                    if _normalize_card_text(previous.text, previous.extra) == text
                ),
                None,
            ):
                deferred_exact[proposal.card_id] = exact_pending.card_id
            else:
                pending.append(proposal)
        if not pending:
            return tuple(exact_results[proposal.card_id] for proposal in ordered)
        embedded = await self.embedder.embed(
            [_v3_document_text(item) for item in pending], input_type="document"
        )
        try:
            proposal_vectors = np.asarray(embedded, dtype=np.float32)
        except (TypeError, ValueError) as exc:
            raise SemanticDedupeIntegrityError(
                "R10 proposal embeddings must be a numeric rectangular matrix"
            ) from exc
        _validate_embedding_vectors(proposal_vectors, expected_rows=len(pending))
        if proposal_vectors.shape[1] < 1 or np.any(np.linalg.norm(proposal_vectors, axis=1) == 0):
            raise SemanticDedupeIntegrityError(
                "R10 proposal embeddings cannot contain zero vectors"
            )
        pinned: dict[int, np.ndarray] = {}
        for note in existing:
            vector = existing_document_vectors.get(note.note_id)
            if vector is None:
                continue
            try:
                row = np.asarray(vector, dtype=np.float32)
            except (TypeError, ValueError) as exc:
                raise SemanticDedupeIntegrityError("R10 pinned vectors must be numeric") from exc
            if (
                row.ndim != 1
                or row.shape[0] != proposal_vectors.shape[1]
                or not np.isfinite(row).all()
            ):
                raise SemanticDedupeIntegrityError("R10 pinned vector dimensions are incompatible")
            if np.linalg.norm(row) == 0:
                raise SemanticDedupeIntegrityError("R10 pinned vectors cannot contain zero vectors")
            pinned[note.note_id] = row / np.linalg.norm(row)
        proposal_normalized = proposal_vectors / np.linalg.norm(
            proposal_vectors, axis=1, keepdims=True
        )
        accepted: list[tuple[V3DedupeProposal, np.ndarray]] = []
        results: list[V3DedupeResult] = []
        for index, proposal in enumerate(pending):
            text = _normalize_card_text(proposal.text, proposal.extra)
            exact = [
                DedupeMatch(f"note:{note.note_id}", 1.0, True)
                for note in existing
                if _normalize_card_text(note.text, note.extra) == text
            ] + [
                DedupeMatch(item.card_id, 1.0, True)
                for item, _vector in accepted
                if _normalize_card_text(item.text, item.extra) == text
            ]
            exact.sort(key=_v3_match_order)
            if exact:
                results.append(
                    V3DedupeResult(
                        proposal.card_id,
                        "duplicate",
                        exact[0].identifier,
                        tuple(exact[: self.nearest_limit]),
                        missing,
                    )
                )
                continue
            semantic: list[DedupeMatch] = [
                DedupeMatch(
                    f"note:{note_id}",
                    float(vector @ proposal_normalized[index]),
                    False,
                )
                for note_id, vector in pinned.items()
            ] + [
                DedupeMatch(
                    item.card_id,
                    float(vector @ proposal_normalized[index]),
                    False,
                )
                for item, vector in accepted
            ]
            semantic.sort(key=lambda item: (-item.score, *_v3_match_order(item)))
            nearest = tuple(semantic[: self.nearest_limit])
            highest = nearest[0] if nearest else None
            if highest is not None and highest.score >= self.duplicate_threshold:
                results.append(
                    V3DedupeResult(
                        proposal.card_id, "duplicate", highest.identifier, nearest, missing
                    )
                )
            elif highest is not None and highest.score >= self.overlap_threshold:
                results.append(V3DedupeResult(proposal.card_id, "overlap", None, nearest, missing))
            else:
                results.append(
                    V3DedupeResult(proposal.card_id, "generated", None, nearest, missing)
                )
                accepted.append((proposal, proposal_normalized[index]))
        semantic_results = {result.card_id: result for result in results}
        final: list[V3DedupeResult] = []
        for proposal in ordered:
            if proposal.card_id in exact_results:
                final.append(exact_results[proposal.card_id])
                continue
            if (canonical_id := deferred_exact.get(proposal.card_id)) is None:
                final.append(semantic_results[proposal.card_id])
                continue
            canonical = semantic_results[canonical_id]
            if canonical.disposition == "generated":
                final.append(
                    V3DedupeResult(
                        proposal.card_id,
                        "duplicate",
                        canonical.card_id,
                        (DedupeMatch(canonical.card_id, 1.0, True),),
                        missing,
                    )
                )
            elif canonical.disposition == "duplicate":
                final.append(
                    V3DedupeResult(
                        proposal.card_id,
                        "duplicate",
                        canonical.duplicate_of,
                        canonical.nearest_matches,
                        missing,
                    )
                )
            else:
                final.append(
                    V3DedupeResult(
                        proposal.card_id, "overlap", None, canonical.nearest_matches, missing
                    )
                )
        return tuple(final)

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


def _v3_proposal_order(proposal: V3DedupeProposal) -> tuple[str, int, str]:
    prefix = f"card:{proposal.fact_id}:"
    ordinal = proposal.card_id.removeprefix(prefix)
    if not proposal.card_id.startswith(prefix) or not ordinal.isdecimal() or int(ordinal) < 1:
        raise ValueError("R10 card IDs must be card:{fact_id}:{positive_integer}")
    return proposal.fact_id, int(ordinal), proposal.card_id


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


def _pinned_existing_document_vectors(
    existing_notes: Sequence[NormalizedNote],
    vectors_by_note_id: Mapping[int, FloatMatrix],
) -> np.ndarray:
    note_ids = [note.note_id for note in existing_notes]
    if len(note_ids) != len(set(note_ids)):
        raise SemanticDedupeIntegrityError("existing document vector identities are ambiguous")
    if set(vectors_by_note_id) != set(note_ids):
        raise SemanticDedupeIntegrityError(
            "pinned existing document vectors do not match existing note identities"
        )
    try:
        vectors = np.asarray(
            [vectors_by_note_id[note_id] for note_id in note_ids],
            dtype=np.float32,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SemanticDedupeIntegrityError(
            "pinned existing document vectors must be a numeric rectangular matrix"
        ) from exc
    _validate_embedding_vectors(vectors, expected_rows=len(note_ids))
    if vectors.shape[1] < 1:
        raise SemanticDedupeIntegrityError(
            "pinned existing document vectors have invalid dimensions"
        )
    if np.any(np.linalg.norm(vectors, axis=1) == 0):
        raise SemanticDedupeIntegrityError(
            "pinned existing document vectors cannot contain zero vectors"
        )
    return vectors


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


def _v3_document_text(proposal: V3DedupeProposal) -> str:
    text = normalize_html(proposal.text)
    return text if text.strip() else normalize_html(proposal.extra)


def _v3_match_order(match: DedupeMatch) -> tuple[int, int | str]:
    if match.identifier.startswith("note:"):
        return 0, int(match.identifier.removeprefix("note:"))
    return 1, match.identifier


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


def _proposal_document_text(proposal: GapCardProposal) -> str:
    """Match the frozen snapshot's ``semantic_text(note)`` document space."""
    text = normalize_html(proposal.fields.get("Text", ""))
    return text if text.strip() else normalize_html(proposal.fields.get("Extra", ""))


def _normalize_card_text(text: str, extra: str) -> str:
    unclozed = _CLOZE.sub(lambda match: match.group(1), text)
    normalized = normalize_html(f"{unclozed} {extra}").casefold()
    return " ".join(_TOKEN.findall(normalized))
