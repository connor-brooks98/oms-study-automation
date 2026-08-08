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
    CardConceptLedger,
    CardRecord,
    CensusTrust,
    ClassifierBatchAudit,
    ClassifierResult,
    ClassifierTelemetry,
    FastCardClassification,
    GeneratedCardResolution,
    SnapshotCensus,
    TagScopeResult,
)
from oms_hub.anki.correction_contracts import EvidenceQuality
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
    return evidence_quality_v2(result, source_index) is not None


def evidence_quality_v2(
    result: CardClassification,
    source_index: CardCentricSourceIndex,
) -> EvidenceQuality | None:
    """Return v2 grounding quality only for an eligible thorough classification."""
    if result.verdict != "YES" or result.flags or not result.supporting_passage_ids:
        return None
    passages = {passage.passage_id: passage for passage in source_index.passages}
    cited = tuple(passages.get(passage_id) for passage_id in result.supporting_passage_ids)
    if any(passage is None for passage in cited):
        return None
    if any(passage.authority != "summary" for passage in cited if passage is not None):
        return EvidenceQuality.PRIMARY_SOURCE
    return EvidenceQuality.SUMMARY_GROUNDED


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


def select_high_yield_v2(
    classifications: Sequence[CardClassification],
    *,
    fast_classifications: Sequence[FastCardClassification],
    ledger: CardConceptLedger,
    source_index: CardCentricSourceIndex,
    generated_cards: Sequence[GeneratedCardResolution],
    fast_fallback_note_ids: Sequence[int] = (),
    target: int = 65,
    cap: int = 70,
    minimum: int = 60,
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[str, ...]]:
    """Deterministic v2 tiers. T6 never creates coverage and only fills a short deck."""
    if not minimum <= target <= cap:
        raise CardCentricValidationError("selection target/cap is invalid")
    concepts = {concept.concept_id: concept for concept in ledger.concepts}

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

    generated = [
        item
        for item in generated_cards
        if item.status == "generated" and item.concept_id in concepts
    ]
    generated.sort(key=lambda item: (*priority(item.concept_id), item.card_id))
    clean = [item for item in classifications if selection_eligible_v2(item, source_index)]
    clean.sort(
        key=lambda item: (
            min(
                (priority(cid) for cid in item.covered_concept_ids if cid in concepts),
                default=(3, ""),
            ),
            item.note_id,
        )
    )
    maybe = [item for item in classifications if item.verdict == "MAYBE"]
    maybe.sort(key=lambda item: item.note_id)
    fast = [item for item in fast_classifications if fast_selection_eligible_v2(item, source_index)]
    fast.sort(key=lambda item: item.note_id)
    selected_notes: list[int] = []
    selected_generated: list[str] = []

    def add_generated(rows: Sequence[GeneratedCardResolution], *, force: bool = False) -> None:
        for row in rows:
            if row.card_id not in selected_generated and (
                force or len(selected_notes) + len(selected_generated) < target
            ):
                selected_generated.append(row.card_id)

    def add_notes(
        rows: Sequence[CardClassification | FastCardClassification], *, force: bool = False
    ) -> None:
        for row in rows:
            if row.note_id not in selected_notes and (
                force or len(selected_notes) + len(selected_generated) < target
            ):
                selected_notes.append(row.note_id)

    # Select every mandatory identity before any ordinary tier.  Otherwise a
    # medium generated row could consume the target slot that a mandatory high
    # existing row needs, yielding an unactionable mixed overflow.
    mandatory_generated = [row for row in generated if priority(row.concept_id)[0] == 0]
    mandatory_clean = [
        row
        for row in clean
        if min(
            (priority(cid)[0] for cid in row.covered_concept_ids if cid in concepts),
            default=3,
        )
        == 0
    ]
    mandatory_fast = [
        row
        for row in fast
        if min(
            (priority(cid)[0] for cid in row.grounded_concept_ids if cid in concepts),
            default=3,
        )
        == 0
    ]
    add_generated(mandatory_generated, force=True)
    add_notes(mandatory_clean, force=True)
    add_notes(mandatory_fast, force=True)

    # T2 medium generated gaps, T4 low generated gaps, and T5 grounded
    # remainder fill only to the 65-card target. Fast YES has the same
    # coverage standing as thorough YES, so it is never demoted to T6.
    add_generated([row for row in generated if priority(row.concept_id)[0] == 1])
    add_generated([row for row in generated if priority(row.concept_id)[0] == 2])
    add_notes([row for row in clean if row.note_id not in selected_notes])
    add_notes([row for row in fast if row.note_id not in selected_notes])
    # T6 may include a thorough MAYBE and the documented prefilter fallback
    # only below the warning floor.  Fast YES rows establish coverage in S5 and
    # therefore belong to T3/T5 instead of this non-coverage tier.
    if len(selected_notes) + len(selected_generated) < minimum:
        add_notes(maybe)
    if len(selected_notes) + len(selected_generated) < minimum:
        add_notes(
            [
                FastCardClassification(
                    note_id=note_id,
                    verdict="NEEDS_REVIEW",
                    reason="documented semantic prefilter fallback",
                )
                for note_id in sorted(set(fast_fallback_note_ids))
            ]
        )
    selected = tuple(sorted(selected_notes))
    selected_gen = tuple(selected_generated)
    return (
        selected,
        tuple(sorted(item.note_id for item in clean if item.note_id not in set(selected))),
        selected_gen,
    )


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
