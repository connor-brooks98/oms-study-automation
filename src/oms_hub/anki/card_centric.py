"""Deterministic S1/S3 and batched S4 services for card_centric_v1."""

import asyncio
import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from oms_hub.anki.card_centric_contracts import (
    CardCentricPassage,
    CardCentricSourceIndex,
    CardClassification,
    CardClassificationBatchOutput,
    CardConcept,
    CardConceptLedger,
    CardRecord,
    CensusTrust,
    ClassifierBatchAudit,
    ClassifierResult,
    ClassifierTelemetry,
    FastCardClassification,
    GeneratedCardResolution,
    QualitySelectionResult,
    SnapshotCensus,
    TagScopeResult,
)
from oms_hub.anki.correction_contracts import (
    CanonicalJsonObject,
    EvidenceQuality,
    MarginalValueReason,
    SelectionMetadata,
    SelectionTier,
)
from oms_hub.anki.domain import SourceKind
from oms_hub.anki.sources import SourcePassage
from oms_hub.llm.domain import GenerationOptions, ProviderCapabilities, ProviderName
from oms_hub.llm.structured import StructuredTextService


class CardCentricValidationError(ValueError):
    """A card-centric artifact or model response cannot be trusted."""


@dataclass(frozen=True, slots=True)
class CardCentricLedgerResult:
    ledger: CardConceptLedger
    request_id: str
    input_tokens: int
    output_tokens: int
    cost_microusd: int
    cache_prefix_sha256: str


@dataclass(slots=True)
class CardCentricLedgerService:
    """One cached-prefix S2 call.  The returned ledger is not a retrieval index."""

    structured: StructuredTextService
    instruction: str

    def generate(
        self,
        *,
        source_index: CardCentricSourceIndex,
        provider: ProviderName,
        model: str,
    ) -> CardCentricLedgerResult:
        summary_prefix = "\n\n".join(
            f'<passage id="{passage.passage_id}">\n{passage.text}\n</passage>'
            for passage in source_index.passages
            if passage.authority == "summary"
        )
        if not summary_prefix:
            raise CardCentricValidationError("ledger requires summary passages")
        result = self.structured.generate_json(
            self.instruction,
            json.dumps(
                {
                    "summary_passages": [
                        {"passage_id": passage.passage_id, "text": passage.text}
                        for passage in source_index.passages
                        if passage.authority == "summary"
                    ],
                    "contract": "coverage_checklist_only",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            output_model=CardConceptLedger,
            provider=provider,
            model=model,
            # S2 is deliberately summary-only: transcript and slide text are
            # reserved for the source-grounded S4/S6/S7 calls.
            options=GenerationOptions(cacheable_source_prefix=summary_prefix),
        )
        return CardCentricLedgerResult(
            ledger=result.value,
            request_id=result.request_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_microusd=result.cost_microusd,
            cache_prefix_sha256=hashlib.sha256(summary_prefix.encode()).hexdigest(),
        )


_SOURCE_ORDER = {"summary": 0, "transcript": 1, "slide": 2}
CARD_CENTRIC_SYSTEM_TOKENS = (
    "cardio",
    "endo",
    "gi",
    "heme",
    "msk",
    "neuro",
    "onc",
    "psych",
    "renal",
    "resp",
)
_UNTAGGED_SAFE_RATE = 0.03
CARD_CENTRIC_UNCONDITIONAL_RESIDUAL_RATE = 0.15
_SYSTEM_ALIASES = {
    "cardio": frozenset({"cardio", "cardiology", "cardiovascular"}),
    "endo": frozenset({"endo", "endocrine", "endocrinology"}),
    "gi": frozenset({"gi", "gastrointestinal", "gastroenterology"}),
    "heme": frozenset({"heme", "hematology", "haematology", "heme lymph", "hematology lymph"}),
    "msk": frozenset({"msk", "musculoskeletal", "orthopedics", "orthopaedics"}),
    "neuro": frozenset({"neuro", "neurology", "neuroscience"}),
    "onc": frozenset({"onc", "oncology"}),
    "psych": frozenset({"psych", "psychiatry", "behavioral science"}),
    "renal": frozenset({"renal", "nephrology"}),
    "resp": frozenset({"resp", "respiratory", "pulmonology", "pulmonary"}),
}


def resolve_card_centric_scope(
    *, tag_allowlist: tuple[str, ...], subject: str, topic: str
) -> tuple[str, ...]:
    """Pin an explicit scope or one unambiguous system token from lecture metadata."""
    explicit = tuple(token.strip() for token in tag_allowlist if token.strip())
    if explicit:
        return explicit
    subject_matches = _recognized_system_tokens(subject)
    matches = subject_matches or _recognized_system_tokens(topic)
    if len(matches) != 1:
        raise CardCentricValidationError(
            "Could not resolve exactly one card-centric system from this lecture. "
            "Enter Existing-card tag scope before queueing."
        )
    return matches


def _recognized_system_tokens(value: str) -> tuple[str, ...]:
    normalized = " ".join(value.casefold().replace("/", " ").replace("-", " ").split())
    if not normalized:
        return ()
    padded = f" {normalized} "
    matches = tuple(
        token
        for token in CARD_CENTRIC_SYSTEM_TOKENS
        if any(f" {alias} " in padded for alias in _SYSTEM_ALIASES[token])
    )
    return matches


@dataclass(frozen=True, slots=True)
class _CompletedBatch:
    results: tuple[CardClassification, ...]
    audit: ClassifierBatchAudit


def build_source_index(
    passages: Iterable[SourcePassage],
    *,
    snapshot_id: str,
    source_revision_hashes: dict[int, str],
    summary_outline_sha256: str | None = None,
) -> CardCentricSourceIndex:
    """Build the immutable S1 prefix: summary, transcript, then slides."""
    converted = [_to_card_passage(passage) for passage in passages]
    ordered = tuple(
        sorted(
            converted,
            key=lambda passage: (
                _SOURCE_ORDER[passage.authority],
                passage.source_id,
                passage.passage_id,
            ),
        )
    )
    if not ordered:
        raise CardCentricValidationError("source index has no usable passages")
    if len({passage.passage_id for passage in ordered}) != len(ordered):
        raise CardCentricValidationError("source index has duplicate passage IDs")
    prefix = "\n\n".join(
        "<passage"
        f' id="{passage.passage_id}" source_id="{passage.source_id}"'
        f' authority="{passage.authority}">\n{passage.text}\n</passage>'
        for passage in ordered
    )
    document = {
        "snapshot_id": snapshot_id,
        "source_revision_hashes": dict(sorted(source_revision_hashes.items())),
        "summary_outline_sha256": summary_outline_sha256,
        "passages": [passage.model_dump(mode="json") for passage in ordered],
        "prefix": prefix,
    }
    return CardCentricSourceIndex(
        snapshot_id=snapshot_id,
        source_revision_hashes=source_revision_hashes,
        summary_outline_sha256=summary_outline_sha256,
        passages=ordered,
        prefix=prefix,
        source_sha256=_sha(document),
    )


def build_snapshot_census(
    cards: Sequence[CardRecord],
    *,
    deck_allowlist: tuple[str, ...],
    scope_tokens: tuple[str, ...],
    snapshot_id: str = "companion_snapshot",
    system_tokens: tuple[str, ...] = CARD_CENTRIC_SYSTEM_TOKENS,
) -> SnapshotCensus:
    _unique_card_ids(cards)
    normalized_decks = tuple(sorted({deck.casefold() for deck in deck_allowlist}))
    normalized_scope = _scope_tokens(scope_tokens)
    system_universe = tuple(sorted(set(_scope_tokens(system_tokens)) | set(normalized_scope)))
    mapping: dict[
        int,
        Literal[
            "target_tagged",
            "other_system_excluded",
            "untagged",
            "deck_excluded",
        ],
    ] = {}
    for card in cards:
        eligible = not normalized_decks or bool(
            {deck.casefold() for deck in card.deck_names} & set(normalized_decks)
        )
        if not eligible:
            mapping[card.note_id] = "deck_excluded"
        elif _matches_scope(card.tags, normalized_scope):
            mapping[card.note_id] = "target_tagged"
        elif _matches_scope(card.tags, system_universe):
            mapping[card.note_id] = "other_system_excluded"
        else:
            mapping[card.note_id] = "untagged"
    tagged = sum(value == "target_tagged" for value in mapping.values())
    other_system = sum(value == "other_system_excluded" for value in mapping.values())
    untagged = sum(value == "untagged" for value in mapping.values())
    deck_excluded = sum(value == "deck_excluded" for value in mapping.values())
    denominator = tagged + other_system + untagged
    rate = 0.0 if denominator == 0 else untagged / denominator
    trusted = denominator > 0 and rate < _UNTAGGED_SAFE_RATE
    return SnapshotCensus(
        snapshot_id=snapshot_id,
        denominator_count=denominator,
        tagged_count=tagged,
        other_system_tagged_count=other_system,
        untagged_count=untagged,
        deck_excluded_count=deck_excluded,
        excluded_count=other_system + deck_excluded,
        mapping={note_id: mapping[note_id] for note_id in sorted(mapping)},
        filters_sha256=_sha(
            {
                "snapshot_id": snapshot_id,
                "deck_allowlist": normalized_decks,
                "scope_tokens": normalized_scope,
                "system_tokens": system_universe,
                "cards": [
                    {"note_id": card.note_id, "content_sha256": card.content_sha256}
                    for card in sorted(cards, key=lambda card: card.note_id)
                ],
            }
        ),
        trust=CensusTrust(
            decision="trusted" if trusted else "blocked",
            reason=(
                "untagged rate is within the card_centric_v1 tag-scope threshold"
                if trusted
                else (
                    "no deck-eligible notes are available for tag-scope trust"
                    if denominator == 0
                    else (
                        "untagged rate requires an unconditional whole-deck residual sweep"
                        if rate >= CARD_CENTRIC_UNCONDITIONAL_RESIDUAL_RATE
                        else "untagged rate exceeds the card_centric_v1 tag-scope threshold; "
                        "the whole-deck residual safety net is required"
                    )
                )
            ),
            untagged_rate=rate,
            safe_untagged_rate=_UNTAGGED_SAFE_RATE,
        ),
    )


def scope_cards(
    cards: Sequence[CardRecord],
    *,
    census: SnapshotCensus,
    scope_tokens: tuple[str, ...],
) -> TagScopeResult:
    _unique_card_ids(cards)
    ids = {card.note_id for card in cards}
    if ids != set(census.mapping):
        raise CardCentricValidationError("census mapping does not match snapshot cards")
    tokens = _scope_tokens(scope_tokens)
    scoped = tuple(
        sorted(
            card.note_id
            for card in cards
            if census.mapping[card.note_id] == "target_tagged" and _matches_scope(card.tags, tokens)
        )
    )
    unscoped = tuple(sorted(ids - set(scoped)))
    if set(scoped) & set(unscoped) or set(scoped) | set(unscoped) != ids:
        raise CardCentricValidationError("tag scope does not partition snapshot cards")
    return TagScopeResult(
        snapshot_id=census.snapshot_id,
        filters_sha256=census.filters_sha256,
        scoped_note_ids=scoped,
        unscoped_note_ids=unscoped,
    )


@dataclass(slots=True)
class CardCentricClassifier:
    structured: StructuredTextService
    instruction: str = (
        "Classify every supplied Anki card against the cached lecture sources. "
        "Return YES, MAYBE, or NO. YES requires cited source support. "
        "Do not invent IDs; return exactly one result per card."
    )
    batch_size: int = 40
    concurrency: int = 8
    capabilities: ProviderCapabilities = ProviderCapabilities()

    def __post_init__(self) -> None:
        if self.batch_size < 1 or self.concurrency < 1:
            raise ValueError("classifier batch size and concurrency must be positive")

    async def classify(
        self,
        cards: Sequence[CardRecord],
        *,
        source_index: CardCentricSourceIndex,
        concept_ids: tuple[str, ...],
        provider: ProviderName,
        model: str,
    ) -> ClassifierResult:
        _unique_card_ids(cards)
        batches = tuple(_batches(cards, self.batch_size))
        semaphore = asyncio.Semaphore(self.concurrency)

        async def one(
            batch_index: int,
            batch: tuple[CardRecord, ...],
        ) -> _CompletedBatch:
            async with semaphore:
                return await asyncio.to_thread(
                    self._classify_batch,
                    batch,
                    batch_index=batch_index,
                    source_index=source_index,
                    concept_ids=concept_ids,
                    provider=provider,
                    model=model,
                )

        completed = await asyncio.gather(
            *(one(index, batch) for index, batch in enumerate(batches))
        )
        results = tuple(
            sorted(
                (item for batch in completed for item in batch.results),
                key=lambda item: item.note_id,
            )
        )
        audits = tuple(
            sorted(
                (batch.audit for batch in completed),
                key=lambda item: item.batch_index,
            )
        )
        request_ids = tuple(audit.request_id for audit in audits)
        return ClassifierResult(
            results=results,
            telemetry=ClassifierTelemetry(
                batch_count=len(batches),
                cache_prefix_sha256=hashlib.sha256(source_index.prefix.encode()).hexdigest(),
                cache_mode=(
                    "ephemeral" if self.capabilities.prompt_prefix_caching else "ordinary_prefix"
                ),
                provider=provider.value,
                model=model,
                request_ids=request_ids,
                batches=audits,
            ),
        )

    def _classify_batch(
        self,
        cards: tuple[CardRecord, ...],
        *,
        batch_index: int,
        source_index: CardCentricSourceIndex,
        concept_ids: tuple[str, ...],
        provider: ProviderName,
        model: str,
    ) -> _CompletedBatch:
        result = self.structured.generate_json(
            self.instruction,
            json.dumps(
                {
                    "cards": [card.model_dump(mode="json") for card in cards],
                    "allowed_concept_ids": list(concept_ids),
                    "allowed_supporting_passage_ids": [
                        passage.passage_id for passage in source_index.passages
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            output_model=CardClassificationBatchOutput,
            provider=provider,
            model=model,
            options=GenerationOptions(cacheable_source_prefix=source_index.prefix),
        )
        return _CompletedBatch(
            results=self.validate_output(
                result.value,
                cards=cards,
                source_index=source_index,
                concept_ids=concept_ids,
            ),
            audit=ClassifierBatchAudit(
                batch_index=batch_index,
                note_ids=tuple(card.note_id for card in cards),
                request_id=result.request_id,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost_microusd=result.cost_microusd,
                cache_creation_input_tokens=result.cache_creation_input_tokens,
                cache_read_input_tokens=result.cache_read_input_tokens,
            ),
        )

    def validate_output(
        self,
        output: CardClassificationBatchOutput,
        *,
        cards: Sequence[CardRecord],
        source_index: CardCentricSourceIndex,
        concept_ids: tuple[str, ...],
    ) -> tuple[CardClassification, ...]:
        expected = {card.note_id for card in cards}
        observed = [result.note_id for result in output.results]
        if len(observed) != len(set(observed)) or set(observed) != expected:
            raise CardCentricValidationError(
                "classifier output does not exactly partition batch cards"
            )
        passages = {passage.passage_id: passage for passage in source_index.passages}
        allowed_concepts = set(concept_ids)
        for result in output.results:
            if not set(result.covered_concept_ids) <= allowed_concepts:
                raise CardCentricValidationError("classifier invented a concept ID")
            if not set(result.supporting_passage_ids) <= set(passages):
                raise CardCentricValidationError("classifier invented a supporting passage ID")
            if result.verdict == "YES" and not result.supporting_passage_ids:
                raise CardCentricValidationError("classifier returned an ungrounded YES")
        return tuple(sorted(output.results, key=lambda item: item.note_id))


def selection_eligible(
    result: CardClassification,
    source_index: CardCentricSourceIndex,
) -> bool:
    """Encode future S5 coverage eligibility now, before S5 exists."""
    passages = {passage.passage_id: passage for passage in source_index.passages}
    return (
        result.verdict == "YES"
        and not result.flags
        and any(
            passage_id in passages and passages[passage_id].authority != "summary"
            for passage_id in result.supporting_passage_ids
        )
    )


def selection_eligible_v2(
    result: CardClassification,
    source_index: CardCentricSourceIndex,
) -> bool:
    """V2 admits grounded summary evidence; v1 keeps its stricter rule."""
    passages = {passage.passage_id for passage in source_index.passages}
    return (
        result.verdict == "YES"
        and not result.flags
        and any(passage_id in passages for passage_id in result.supporting_passage_ids)
    )


def fast_selection_eligible_v2(
    result: FastCardClassification,
    source_index: CardCentricSourceIndex,
) -> bool:
    """Fast-pass YES rows are grounded enough for v2 coverage and selection."""
    passages = {passage.passage_id for passage in source_index.passages}
    return (
        result.verdict == "LIKELY_YES"
        and not result.flags
        and bool(result.grounded_concept_ids)
        and any(passage_id in passages for passage_id in result.supporting_passage_ids)
    )


@dataclass(frozen=True, slots=True)
class _QualitySelectionCandidate:
    identity: str
    kind: Literal["existing", "generated"]
    existing_note_id: int | None
    generated_card_id: str | None
    tier: SelectionTier
    evidence_quality: EvidenceQuality
    coverage: frozenset[tuple[str, str]]
    priority: int
    mandatory: bool
    split: bool
    flag_count: int


def _selection_identity_for_note(note_id: int) -> str:
    return f"existing:{note_id}"


def _selection_identity_for_generated(card_id: str) -> str:
    return f"generated:{card_id}"


def select_high_yield_v2(
    classifications: Sequence[CardClassification],
    *,
    fast_classifications: Sequence[FastCardClassification],
    ledger: CardConceptLedger,
    source_index: CardCentricSourceIndex,
    generated_cards: Sequence[GeneratedCardResolution],
    fast_fallback_note_ids: Sequence[int] = (),
    semantic_review_required_card_ids: Sequence[str] = (),
    overflow_acknowledgement: dict[str, object] | CanonicalJsonObject | None = None,
    target: int = 65,
    cap: int = 70,
    minimum: int = 60,
) -> QualitySelectionResult:
    """Select the smallest deterministic, quality-first v2 card set.

    T6 is a last-resort source of independently grounded cards below the warning
    floor.  It never turns an unrecovered semantic fallback into a candidate.
    """
    if not minimum <= target <= cap:
        raise CardCentricValidationError("selection target/cap is invalid")
    concepts = {concept.concept_id: concept for concept in ledger.concepts}
    source_authority = {
        passage.passage_id: passage.authority for passage in source_index.passages
    }
    semantic_review_ids = tuple(sorted(set(semantic_review_required_card_ids)))
    semantic_review_set = set(semantic_review_ids)

    def priority(concept_id: str) -> tuple[int, str]:
        concept = concepts[concept_id]
        return (
            0
            if concept.emphasis_flag or concept.importance == "high"
            else 1
            if concept.importance == "medium"
            else 2,
            concept_id,
        )

    def evidence_quality(passage_ids: Sequence[str], *, fast: bool = False) -> EvidenceQuality:
        if fast:
            return EvidenceQuality.FAST_PASS
        if any(
            source_authority.get(passage_id) in {"slide", "transcript"}
            for passage_id in passage_ids
        ):
            return EvidenceQuality.PRIMARY_SOURCE
        return EvidenceQuality.SUMMARY_GROUNDED

    def covered_priority(concept_ids: Sequence[str]) -> int:
        return min(
            (priority(concept_id)[0] for concept_id in concept_ids if concept_id in concepts),
            default=3,
        )

    candidates: list[_QualitySelectionCandidate] = []
    selectable_generated_ids: set[str] = set()
    for generated_row in generated_cards:
        tier_priority = (
            priority(generated_row.concept_id)[0]
            if generated_row.concept_id in concepts
            else 3
        )
        tier = (
            SelectionTier.T1
            if tier_priority == 0
            else SelectionTier.T2
            if tier_priority == 1
            else SelectionTier.T4
        )
        candidates.append(
            _QualitySelectionCandidate(
                identity=_selection_identity_for_generated(generated_row.card_id),
                kind="generated",
                existing_note_id=None,
                generated_card_id=generated_row.card_id,
                tier=tier,
                evidence_quality=evidence_quality(generated_row.source_passage_ids),
                coverage=frozenset({("fact", generated_row.fact_id)}),
                priority=tier_priority,
                mandatory=tier_priority == 0,
                split=generated_row.split,
                flag_count=0,
            )
        )
        if (
            generated_row.status == "generated"
            and generated_row.concept_id in concepts
            and any(
                passage_id in source_authority
                for passage_id in generated_row.source_passage_ids
            )
        ):
            selectable_generated_ids.add(generated_row.card_id)

    for classification in classifications:
        tier_priority = covered_priority(classification.covered_concept_ids)
        coverage = frozenset(
            ("concept", concept_id)
            for concept_id in classification.covered_concept_ids
            if concept_id in concepts
        )
        if selection_eligible_v2(classification, source_index):
            tier = SelectionTier.T3 if tier_priority == 0 else SelectionTier.T5
        elif (
            classification.verdict == "MAYBE"
            and not classification.flags
            and coverage
            and any(
                passage_id in source_authority
                for passage_id in classification.supporting_passage_ids
            )
        ):
            tier = SelectionTier.T6
        else:
            continue
        candidates.append(
            _QualitySelectionCandidate(
                identity=_selection_identity_for_note(classification.note_id),
                kind="existing",
                existing_note_id=classification.note_id,
                generated_card_id=None,
                tier=tier,
                evidence_quality=evidence_quality(classification.supporting_passage_ids),
                coverage=coverage,
                priority=tier_priority,
                mandatory=tier is SelectionTier.T3 and tier_priority == 0,
                split=False,
                flag_count=len(classification.flags),
            )
        )

    for fast_classification in fast_classifications:
        if not fast_selection_eligible_v2(fast_classification, source_index):
            continue
        coverage = frozenset(
            ("concept", concept_id)
            for concept_id in fast_classification.grounded_concept_ids
            if concept_id in concepts
        )
        candidates.append(
            _QualitySelectionCandidate(
                identity=_selection_identity_for_note(fast_classification.note_id),
                kind="existing",
                existing_note_id=fast_classification.note_id,
                generated_card_id=None,
                tier=SelectionTier.T6,
                evidence_quality=EvidenceQuality.FAST_PASS,
                coverage=coverage,
                priority=covered_priority(fast_classification.grounded_concept_ids),
                mandatory=False,
                split=False,
                flag_count=len(fast_classification.flags),
            )
        )

    existing_candidate_ids = tuple(
        sorted(
            {
                *(row.note_id for row in classifications),
                *(row.note_id for row in fast_classifications),
                *fast_fallback_note_ids,
            }
        )
    )
    generated_candidate_ids = tuple(sorted({row.card_id for row in generated_cards}))
    if len(generated_candidate_ids) != len(generated_cards):
        raise CardCentricValidationError("generated selection card IDs must be unique")
    if not semantic_review_set <= set(generated_candidate_ids):
        raise CardCentricValidationError("semantic review IDs must be generated candidates")

    # Preserve every input identity in the frozen partition but only make
    # structurally valid, independently grounded rows selectable.
    candidate_by_identity: dict[str, _QualitySelectionCandidate] = {}
    for candidate in candidates:
        current = candidate_by_identity.get(candidate.identity)
        if current is None or _candidate_static_key(candidate) < _candidate_static_key(current):
            candidate_by_identity[candidate.identity] = candidate
    selectable = [
        candidate
        for candidate in candidate_by_identity.values()
        if candidate.generated_card_id not in semantic_review_set
        and candidate.existing_note_id not in set(fast_fallback_note_ids)
        and (
            candidate.kind == "existing"
            or candidate.generated_card_id in selectable_generated_ids
        )
    ]
    selectable = _without_dominated_candidates(selectable)

    selected: list[_QualitySelectionCandidate] = []
    selected_coverage: set[tuple[str, str]] = set()
    for tier in SelectionTier:
        tier_candidates = [candidate for candidate in selectable if candidate.tier is tier]
        while tier_candidates:
            candidate = min(
                tier_candidates,
                key=lambda item: _candidate_selection_key(item, selected_coverage),
            )
            tier_candidates.remove(candidate)
            count = len(selected)
            if tier is SelectionTier.T6 and count >= minimum:
                break
            marginal_reason = _marginal_reason(candidate, selected_coverage, concepts)
            if count >= target and count < cap and marginal_reason is None:
                continue
            if count >= cap:
                if not candidate.mandatory:
                    continue
            selected.append(candidate)
            selected_coverage.update(candidate.coverage)

    metadata = tuple(
        SelectionMetadata(
            identity=candidate.identity,
            selected_position=index,
            tier=candidate.tier,
            evidence_quality=candidate.evidence_quality,
            mandatory=candidate.mandatory,
            marginal_value_reason=(
                _marginal_reason(candidate, set(), concepts) if 66 <= index <= cap else None
            ),
            overflow_reason=(
                "validated mandatory high-value nonredundant coverage" if index > cap else None
            ),
            manual_acknowledgement_required=index > cap,
        )
        for index, candidate in enumerate(selected, start=1)
    )
    selected_existing = tuple(
        candidate.existing_note_id
        for candidate in selected
        if candidate.existing_note_id is not None
    )
    selected_generated = tuple(
        candidate.generated_card_id
        for candidate in selected
        if candidate.generated_card_id is not None
    )
    selected_existing_set = set(selected_existing)
    selected_generated_set = set(selected_generated)
    acknowledgement = (
        overflow_acknowledgement
        if isinstance(overflow_acknowledgement, CanonicalJsonObject)
        else CanonicalJsonObject.from_mapping(overflow_acknowledgement)
        if overflow_acknowledgement is not None
        else None
    )
    return QualitySelectionResult(
        existing_candidate_note_ids=existing_candidate_ids,
        generated_candidate_card_ids=generated_candidate_ids,
        selected_existing_note_ids=selected_existing,
        selected_generated_card_ids=selected_generated,
        excluded_existing_note_ids=tuple(
            note_id for note_id in existing_candidate_ids if note_id not in selected_existing_set
        ),
        excluded_generated_card_ids=tuple(
            card_id for card_id in generated_candidate_ids if card_id not in selected_generated_set
        ),
        selection_metadata=metadata,
        below_warning_floor=len(selected) < minimum,
        target=target,
        cap=cap,
        minimum_target=minimum,
        mandatory_note_ids=tuple(
            sorted(
                candidate.existing_note_id
                for candidate in selected
                if candidate.mandatory and candidate.existing_note_id is not None
            )
        ),
        mandatory_generated_card_ids=tuple(
            sorted(
                candidate.generated_card_id
                for candidate in selected
                if candidate.mandatory and candidate.generated_card_id is not None
            )
        ),
        semantic_review_required_card_ids=semantic_review_ids,
        overflow_acknowledgement=acknowledgement,
    )


def _candidate_static_key(
    candidate: _QualitySelectionCandidate,
) -> tuple[int, int, int, int, int, str]:
    """The stable portion of the quality order, used for ties and dominance."""
    return (
        -int(candidate.mandatory),
        -_evidence_quality_rank(candidate.evidence_quality),
        candidate.priority,
        candidate.flag_count,
        len(candidate.coverage),
        candidate.identity,
    )


def _candidate_quality_key(candidate: _QualitySelectionCandidate) -> tuple[int, int, int, int]:
    return (
        -int(candidate.mandatory),
        -_evidence_quality_rank(candidate.evidence_quality),
        candidate.priority,
        candidate.flag_count,
    )


def _candidate_selection_key(
    candidate: _QualitySelectionCandidate,
    selected_coverage: set[tuple[str, str]],
) -> tuple[int, int, int, int, int, int, str]:
    return (
        -int(candidate.mandatory),
        -_evidence_quality_rank(candidate.evidence_quality),
        -len(candidate.coverage - selected_coverage),
        candidate.priority,
        candidate.flag_count,
        len(candidate.coverage & selected_coverage),
        candidate.identity,
    )


def _evidence_quality_rank(evidence_quality: EvidenceQuality) -> int:
    return {
        EvidenceQuality.PRIMARY_SOURCE: 2,
        EvidenceQuality.SUMMARY_GROUNDED: 1,
        EvidenceQuality.FAST_PASS: 0,
    }[evidence_quality]


def _without_dominated_candidates(
    candidates: Sequence[_QualitySelectionCandidate],
) -> list[_QualitySelectionCandidate]:
    kept: list[_QualitySelectionCandidate] = []
    for candidate in candidates:
        if candidate.split:
            kept.append(candidate)
            continue
        quality = _candidate_quality_key(candidate)
        dominated = any(
            not other.split
            and candidate.coverage <= other.coverage
            and _candidate_quality_key(other) <= quality
            and (
                candidate.coverage != other.coverage
                or _candidate_quality_key(other) != quality
                or other.identity < candidate.identity
            )
            for other in candidates
            if other.identity != candidate.identity
        )
        if not dominated:
            kept.append(candidate)
    return kept


def _marginal_reason(
    candidate: _QualitySelectionCandidate,
    selected_coverage: set[tuple[str, str]],
    concepts: dict[str, CardConcept],
) -> MarginalValueReason | None:
    if candidate.split:
        return MarginalValueReason.VALIDATED_NECESSARY_SPLIT
    if (
        candidate.kind == "generated"
        and candidate.priority <= 1
        and candidate.coverage - selected_coverage
    ):
        return MarginalValueReason.ONLY_VALID_REQUIRED_FACT
    if any(
        kind == "concept"
        and concept_id in concepts
        and concepts[concept_id].emphasis_flag
        and (kind, concept_id) not in selected_coverage
        for kind, concept_id in candidate.coverage
    ):
        return MarginalValueReason.UNIQUE_EMPHASIZED_DISTINCTION
    return None


def select_high_yield(
    classifications: Sequence[CardClassification],
    *,
    ledger: CardConceptLedger,
    source_index: CardCentricSourceIndex,
    generated_card_ids: Sequence[str] = (),
    overflow_acknowledgement: dict[str, str] | None = None,
    target: int = 65,
    cap: int = 70,
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[str, ...]]:
    """Stable, evidence-first selection without a minimum-padding rule.

    A clean grounded YES is eligible.  Deep/emphasized/high concepts are protected
    before ordinary cards; then coverage diversity and note ID make every tie
    deterministic.  Generated cards are not used to manufacture a 60-card floor.
    """
    if not 1 <= target <= cap:
        raise CardCentricValidationError("selection target/cap is invalid")
    concepts = {concept.concept_id: concept for concept in ledger.concepts}
    eligible = [item for item in classifications if selection_eligible(item, source_index)]
    if len({item.note_id for item in eligible}) != len(eligible):
        raise CardCentricValidationError("eligible selection note IDs are not unique")

    def rank(item: CardClassification) -> tuple[int, int, int, int, int]:
        covered = [concepts[value] for value in item.covered_concept_ids if value in concepts]
        emphasis = int(any(concept.emphasis_flag for concept in covered))
        high = int(any(concept.importance == "high" for concept in covered))
        depth = max(
            ({"deep": 2, "medium": 1, "surface": 0}[concept.depth] for concept in covered),
            default=0,
        )
        return (-emphasis, -high, -depth, -len(covered), item.note_id)

    ordered = sorted(eligible, key=rank)
    # Mandatory means evidence-backed clean cards covering an emphasized/high
    # concept. They are selected before ordinary target truncation.
    mandatory = [
        item
        for item in ordered
        if any(
            concepts[c].emphasis_flag or concepts[c].importance == "high"
            for c in item.covered_concept_ids
            if c in concepts
        )
    ]
    if len(mandatory) > cap:
        # The stage issues the durable server acknowledgement after it has the
        # canonical selection.  Do not truncate mandatory evidence merely to
        # fit the ordinary cap.
        return (
            tuple(sorted(item.note_id for item in mandatory)),
            tuple(sorted(item.note_id for item in eligible if item not in mandatory)),
            tuple(sorted(set(generated_card_ids))),
        )
    selected: list[CardClassification] = list(mandatory)
    seen_concepts: set[str] = set()
    for item in ordered:
        if item not in selected and (
            any(concept_id not in seen_concepts for concept_id in item.covered_concept_ids)
            and len(selected) < cap
        ):
            selected.append(item)
            seen_concepts.update(item.covered_concept_ids)
    for item in ordered:
        if item not in selected and len(selected) < min(target, cap):
            selected.append(item)
    selected_ids = tuple(sorted(item.note_id for item in selected))
    excluded = tuple(
        sorted(item.note_id for item in eligible if item.note_id not in set(selected_ids))
    )
    # Generated cards are source-grounded coverage candidates, not padding.  If
    # capacity remains, retain a stable subset; the S9 cap invariant prevents
    # accidental expansion during review/envelope construction.
    generated = tuple(sorted(set(generated_card_ids)))[: max(0, cap - len(selected_ids))]
    return selected_ids, excluded, generated


def _to_card_passage(passage: SourcePassage) -> CardCentricPassage:
    if passage.source_kind is SourceKind.SUMMARY:
        authority: Literal["summary", "transcript", "slide"] = "summary"
    elif passage.source_kind is SourceKind.TRANSCRIPT:
        authority = "transcript"
    elif passage.source_kind in {SourceKind.SLIDE, SourceKind.SPEAKER_NOTES}:
        authority = "slide"
    else:
        raise CardCentricValidationError(
            f"source kind {passage.source_kind.value} is not usable in card-centric prefix"
        )
    return CardCentricPassage(
        passage_id=f"{passage.source_id}:P:{passage.passage_id[:16]}",
        source_id=passage.source_id,
        source_kind=authority,
        authority=authority,
        revision_id=passage.revision_id,
        content_sha256=passage.content_hash,
        text=passage.text,
    )


def _scope_tokens(tokens: tuple[str, ...]) -> tuple[str, ...]:
    values = tuple(sorted({value.strip().casefold() for value in tokens if value.strip()}))
    if not values:
        raise CardCentricValidationError("tag scope has no resolved tokens")
    return values


def _matches_scope(tags: Sequence[str], tokens: tuple[str, ...]) -> bool:
    return any(token in tag.casefold() for token in tokens for tag in tags)


def _unique_card_ids(cards: Sequence[CardRecord]) -> None:
    if len({card.note_id for card in cards}) != len(cards):
        raise CardCentricValidationError("snapshot contains duplicate note IDs")


def _batches(cards: Sequence[CardRecord], size: int) -> Iterable[tuple[CardRecord, ...]]:
    for start in range(0, len(cards), size):
        yield tuple(cards[start : start + size])


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
