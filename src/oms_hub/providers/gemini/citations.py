"""Fail-closed mapping from Gemini citation payloads to source evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum

from oms_hub.indexing.service import IndexManifest, IndexManifestInput
from oms_hub.providers.contracts import EvidenceRef

_EVIDENCE_MARKER = re.compile(r"\[EVIDENCE:([A-Za-z0-9_-]{1,200})\]")
_NUMBERED_LOCATOR = re.compile(r"(?:slide\s+|page\s+)?([1-9][0-9]*)(?::[1-9][0-9]*)?\Z")
_FUZZY_THRESHOLD = 0.9


class CitationMatchKind(StrEnum):
    EVIDENCE_MARKER = "evidence_marker"
    PDF_PAGE = "pdf_page"
    EXACT_EXCERPT = "exact_excerpt"
    FUZZY_EXCERPT = "fuzzy_excerpt"


@dataclass(frozen=True, slots=True)
class ProviderCitation:
    provider_file_name: str
    source_excerpt: str
    page_number: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.provider_file_name, str)
            or not self.provider_file_name.strip()
            or len(self.provider_file_name) > 500
            or not self.provider_file_name.isprintable()
        ):
            raise ValueError("provider citation file name is invalid")
        if (
            not isinstance(self.source_excerpt, str)
            or len(self.source_excerpt) > 20_000
            or not self.source_excerpt.isprintable()
        ):
            raise ValueError("provider citation excerpt is invalid")
        if self.page_number is not None and self.page_number < 1:
            raise ValueError("provider citation page number must be positive")


@dataclass(frozen=True, slots=True)
class CitationCandidate:
    evidence: EvidenceRef
    match_kind: CitationMatchKind
    confidence: float


def map_provider_citation(
    citation: ProviderCitation,
    source_index_manifest: IndexManifest,
) -> EvidenceRef | None:
    candidate = select_citation_candidate(citation, source_index_manifest)
    return candidate.evidence if candidate is not None else None


def select_citation_candidate(
    citation: ProviderCitation,
    source_index_manifest: IndexManifest,
) -> CitationCandidate | None:
    evidence_by_id = {ref.evidence_id: ref for ref in source_index_manifest.evidence}
    markers = set(_EVIDENCE_MARKER.findall(citation.source_excerpt))
    if markers:
        if len(markers) != 1:
            return None
        evidence = evidence_by_id.get(markers.pop())
        return (
            CitationCandidate(evidence, CitationMatchKind.EVIDENCE_MARKER, 1.0)
            if evidence is not None
            else None
        )

    manifest_input = _resolve_input(citation.provider_file_name, source_index_manifest.inputs)
    if manifest_input is None:
        return None
    allowed = tuple(
        evidence_by_id[evidence_id]
        for evidence_id in manifest_input.evidence_ids
        if evidence_id in evidence_by_id
    )
    if manifest_input.input_kind == "pdf" and citation.page_number is not None:
        page_matches = tuple(
            evidence
            for evidence in allowed
            if evidence.locator_kind in {"page", "slide"}
            and _locator_number(evidence.locator_value) == citation.page_number
        )
        if len(page_matches) == 1:
            return CitationCandidate(page_matches[0], CitationMatchKind.PDF_PAGE, 1.0)
        if page_matches:
            return None

    excerpt = _normalize(citation.source_excerpt)
    if not excerpt:
        return None
    exact = tuple(evidence for evidence in allowed if _normalize(evidence.excerpt) == excerpt)
    if len(exact) == 1:
        return CitationCandidate(exact[0], CitationMatchKind.EXACT_EXCERPT, 1.0)
    if exact:
        return None

    scored = tuple(
        (
            SequenceMatcher(None, excerpt, _normalize(evidence.excerpt), autojunk=False).ratio(),
            evidence,
        )
        for evidence in allowed
    )
    if not scored:
        return None
    best_score = max(score for score, _ in scored)
    best = tuple(evidence for score, evidence in scored if score == best_score)
    if best_score < _FUZZY_THRESHOLD or len(best) != 1:
        return None
    return CitationCandidate(best[0], CitationMatchKind.FUZZY_EXCERPT, best_score)


def _resolve_input(
    provider_file_name: str,
    inputs: tuple[IndexManifestInput, ...],
) -> IndexManifestInput | None:
    alias = provider_file_name.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    matches = tuple(
        item for item in inputs if alias in {item.input_key.casefold(), item.path.name.casefold()}
    )
    return matches[0] if len(matches) == 1 else None


def _locator_number(value: str) -> int | None:
    match = _NUMBERED_LOCATOR.fullmatch(value.casefold().strip())
    return int(match.group(1)) if match is not None else None


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


__all__ = [
    "CitationCandidate",
    "CitationMatchKind",
    "ProviderCitation",
    "map_provider_citation",
    "select_citation_candidate",
]
