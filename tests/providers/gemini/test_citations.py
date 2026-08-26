from __future__ import annotations

import hashlib
from pathlib import Path

from oms_hub.indexing.service import IndexManifest, IndexManifestInput
from oms_hub.providers.contracts import AuthorityClass, EvidenceRef
from oms_hub.providers.gemini.citations import (
    CitationMatchKind,
    ProviderCitation,
    map_provider_citation,
    select_citation_candidate,
)

REVISION_ID = "sr_aaaaaaaaaaaaaaaaaaaaaaaaaa"


def _ref(evidence_id: str, slide: int, excerpt: str) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=evidence_id,
        source_revision_id=REVISION_ID,
        authority_class=AuthorityClass.COURSE_MATERIAL,
        locator_kind="slide",
        locator_value=str(slide),
        excerpt=excerpt,
        checksum=hashlib.sha256(excerpt.encode()).hexdigest(),
    )


def _input(
    tmp_path: Path,
    *,
    key: str,
    kind: str,
    filename: str,
    evidence_ids: tuple[str, ...],
) -> IndexManifestInput:
    path = tmp_path / filename
    path.write_text(key, encoding="utf-8")
    return IndexManifestInput(
        input_key=key,
        input_kind=kind,
        path=path,
        media_type="application/pdf" if kind == "pdf" else "text/markdown",
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        evidence_ids=evidence_ids,
    )


def _manifest(tmp_path: Path, *evidence: EvidenceRef) -> IndexManifest:
    evidence_ids = tuple(ref.evidence_id for ref in evidence)
    return IndexManifest(
        source_revision_id=REVISION_ID,
        authority_class=AuthorityClass.COURSE_MATERIAL,
        inputs=(
            _input(
                tmp_path,
                key="pdf",
                kind="pdf",
                filename="lecture-13.pdf",
                evidence_ids=evidence_ids,
            ),
            _input(
                tmp_path,
                key="normalized_markdown",
                kind="markdown",
                filename="lecture-13-normalized.md",
                evidence_ids=evidence_ids,
            ),
        ),
        evidence=tuple(evidence),
    )


def test_explicit_marker_has_precedence_and_foreign_marker_fails_closed(
    tmp_path: Path,
) -> None:
    marker = _ref("ev_marker", 7, "Marker-selected evidence.")
    page = _ref("ev_page", 42, "Page-selected evidence.")
    manifest = _manifest(tmp_path, marker, page)

    mapped = map_provider_citation(
        ProviderCitation(
            provider_file_name="lecture-13.pdf",
            page_number=42,
            source_excerpt="[EVIDENCE:ev_marker] provider excerpt",
        ),
        manifest,
    )
    foreign = map_provider_citation(
        ProviderCitation(
            provider_file_name="lecture-13.pdf",
            page_number=42,
            source_excerpt="[EVIDENCE:ev_from_other_revision] provider excerpt",
        ),
        manifest,
    )

    assert mapped == marker
    assert foreign is None


def test_pdf_page_maps_to_unique_slide_evidence(tmp_path: Path) -> None:
    page = _ref("ev_page", 42, "Page-selected evidence.")
    manifest = _manifest(tmp_path, page)

    mapped = map_provider_citation(
        ProviderCitation(
            provider_file_name="lecture-13.pdf",
            page_number=42,
            source_excerpt="unrelated provider excerpt",
        ),
        manifest,
    )

    assert mapped is not None
    assert mapped.locator_kind == "slide"
    assert mapped.locator_value == "42"


def test_pdf_page_collision_is_not_guessed(tmp_path: Path) -> None:
    first = _ref("ev_page_a", 42, "First block on the slide.")
    second = _ref("ev_page_b", 42, "Second block on the slide.")
    manifest = _manifest(tmp_path, first, second)

    mapped = map_provider_citation(
        ProviderCitation(
            provider_file_name="lecture-13.pdf",
            page_number=42,
            source_excerpt="not an exact block",
        ),
        manifest,
    )

    assert mapped is None


def test_exact_excerpt_is_unique_and_bounded_to_the_named_input(tmp_path: Path) -> None:
    exact = _ref("ev_exact", 9, "Platelets fall after the exposure window.")
    other = _ref("ev_other", 10, "A separate evidence statement.")
    manifest = _manifest(tmp_path, exact, other)

    mapped = map_provider_citation(
        ProviderCitation(
            provider_file_name="normalized_markdown",
            source_excerpt="  Platelets   fall after the exposure window. ",
        ),
        manifest,
    )
    unknown_file = map_provider_citation(
        ProviderCitation(
            provider_file_name="other-source.md",
            source_excerpt=exact.excerpt,
        ),
        manifest,
    )

    assert mapped == exact
    assert unknown_file is None


def test_duplicate_exact_excerpt_is_not_guessed(tmp_path: Path) -> None:
    excerpt = "The same normalized evidence appears twice."
    manifest = _manifest(
        tmp_path,
        _ref("ev_duplicate_a", 11, excerpt),
        _ref("ev_duplicate_b", 12, excerpt),
    )

    mapped = map_provider_citation(
        ProviderCitation(
            provider_file_name="lecture-13-normalized.md",
            source_excerpt=excerpt,
        ),
        manifest,
    )

    assert mapped is None


def test_fuzzy_excerpt_records_confidence_and_stays_within_one_input(
    tmp_path: Path,
) -> None:
    close = _ref(
        "ev_fuzzy",
        13,
        "Immune thrombocytopenia causes a substantial platelet count fall.",
    )
    unrelated = _ref("ev_unrelated", 14, "The intrinsic pathway begins upstream.")
    manifest = _manifest(tmp_path, close, unrelated)
    citation = ProviderCitation(
        provider_file_name="lecture-13-normalized.md",
        source_excerpt="Immune thrombocytopenia causes substantial platelet count fall.",
    )

    candidate = select_citation_candidate(citation, manifest)

    assert candidate is not None
    assert candidate.match_kind is CitationMatchKind.FUZZY_EXCERPT
    assert 0.9 <= candidate.confidence < 1.0
    assert candidate.evidence == close
    assert map_provider_citation(citation, manifest) == close


def test_fuzzy_tie_and_cross_file_evidence_are_not_guessed(tmp_path: Path) -> None:
    first = _ref("ev_tie_a", 15, "alpha beta gamma delta")
    second = _ref("ev_tie_b", 16, "alpha beta gamma delta")
    crossed_evidence = _ref("ev_crossed", 17, "Only the Markdown input contains this.")
    manifest = _manifest(tmp_path, first, second, crossed_evidence)
    image_path = tmp_path / "figure.png"
    image_path.write_bytes(b"image")
    image_input = IndexManifestInput(
        input_key=f"image.{hashlib.sha256(image_path.read_bytes()).hexdigest()}",
        input_kind="image",
        path=image_path,
        media_type="image/png",
        sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
        evidence_ids=("ev_tie_a",),
    )
    manifest = IndexManifest(
        source_revision_id=manifest.source_revision_id,
        authority_class=manifest.authority_class,
        inputs=(*manifest.inputs, image_input),
        evidence=manifest.evidence,
    )

    tie = map_provider_citation(
        ProviderCitation(
            provider_file_name="lecture-13-normalized.md",
            source_excerpt="alpha beta gamma delt",
        ),
        manifest,
    )
    crossed = map_provider_citation(
        ProviderCitation(
            provider_file_name="figure.png",
            source_excerpt=crossed_evidence.excerpt,
        ),
        manifest,
    )

    assert tie is None
    assert crossed is None
