import re
from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from oms_hub.anki.correction_contracts import (
    WARNING_FLOOR,
    GeneratedCardIdentity,
    GeneratedFactResolution,
    GeneratedOutputSet,
    GeneratedResolutionKind,
    SelectionMetadata,
)
from oms_hub.anki.gaps import GapValidationError, validate_gap_card_fields

_CLOZE = re.compile(
    r"\{\{c\d+::(?P<answer>.*?)(?:::[^{}]*?)?\}\}",
    re.IGNORECASE,
)
_HTML = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


class ConceptResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    concept_id: str = Field(min_length=1)
    missing_fact_ids: tuple[str, ...]
    status: str = Field(min_length=1)
    converged: bool
    cited_passage_ids: tuple[str, ...]


class GeneratedResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    card_id: str = Field(min_length=1)
    fact_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    extra: str = ""
    split: bool = False
    split_index: int | None = Field(
        default=None,
        ge=1,
        exclude_if=lambda value: value is None,
    )


class AuditResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    nid: int
    verdict: Literal["keep", "drop", "uncertain"]


class ReconciliationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    concepts: tuple[ConceptResolution, ...]
    generated_cards: tuple[GeneratedResolution, ...]
    unresolved_fact_ids: tuple[str, ...]
    expected_audit_nids: tuple[int, ...]
    audit_verdicts: tuple[AuditResolution, ...]
    source_passage_ids: tuple[str, ...]
    forbidden_cloze_targets: tuple[str, ...]
    prompt_sync_stale: bool


class AssertionFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assertion_id: str
    message: str


class ReconciliationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: tuple[str, ...]
    failed: tuple[AssertionFinding, ...]
    warned: tuple[AssertionFinding, ...]
    can_render_envelope: bool


class CardCentricReconciliationInput(BaseModel):
    """Frozen S9 inputs. Kept separate so retrieval-v4 reconciliation is unchanged."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pipeline_contract_version: Literal["card_centric_v1", "card_centric_v2"] = "card_centric_v1"
    concept_ids: tuple[str, ...]
    coverage: dict[str, Literal["covered", "uncovered", "intentional_gap"]]
    required_fact_ids: tuple[str, ...]
    uncovered_after_s5: tuple[str, ...]
    residual_ran_for: tuple[str, ...]
    # ``generated_cards`` is retained as the selected-only rendering view.
    # S9 validates S7 and S8 independently so a valid unselected card cannot
    # be mistaken for an unresolved fact or bypass generation validation.
    generated_cards: tuple[GeneratedResolution, ...]
    raw_generated_cards: tuple[GeneratedResolution, ...] = ()
    canonical_generated_cards: tuple[GeneratedResolution, ...] = ()
    terminal_resolutions: tuple[GeneratedFactResolution, ...] = ()
    terminal_resolutions_provided: bool = False
    canonical_unresolved_fact_ids: tuple[str, ...] = ()
    unresolved_fact_ids: tuple[str, ...]
    expected_scoped_nids: tuple[int, ...]
    classifications: tuple[AuditResolution, ...]
    eligible_yes_nids: tuple[int, ...]
    selected_nids: tuple[int, ...]
    selected_generated_card_ids: tuple[str, ...]
    generated_card_ids: tuple[str, ...]
    source_passage_ids: tuple[str, ...]
    forbidden_cloze_targets: tuple[str, ...]
    forbidden_cloze_targets_by_fact: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    prompt_sync_stale: bool
    untagged_rate: float = Field(ge=0, le=1)
    census_warning_rate: float = Field(default=0.03, gt=0, le=1)
    target: int = Field(default=65, ge=1)
    cap: int = Field(default=70, ge=1)
    mandatory_nids: tuple[int, ...] = ()
    mandatory_generated_card_ids: tuple[str, ...] = ()
    # These immutable S9 mappings let review-time reconciliation prove that
    # coverage comes from a currently selected card, never an unselected row.
    covered_concept_ids_by_nid: dict[int, tuple[str, ...]] = Field(default_factory=dict)
    generated_concept_id_by_card_id: dict[str, str] = Field(default_factory=dict)
    overflow_acknowledgement: dict[str, object] | None = None
    selection_metadata: tuple[SelectionMetadata, ...] = ()
    selection_order: tuple[str, ...] = ()
    selected_count: int | None = None
    below_warning_floor: bool | None = None
    semantic_review_required_card_ids: tuple[str, ...] = ()
    ledger_provenance_ok: bool = True
    historical_yes_rates: tuple[float, ...] = ()
    t6_selected_nids: tuple[int, ...] = ()


def selected_card_centric_coverage(
    snapshot: CardCentricReconciliationInput,
) -> dict[str, Literal["covered", "uncovered", "intentional_gap"]]:
    """Derive S9 coverage solely from the current selected identities.

    An intentional gap is durable only when every generated fact requested for
    that concept is represented by a canonical unresolved resolution.  This
    intentionally does not trust the pre-selection S5 coverage value.
    """
    selected_concepts = {
        concept_id
        for note_id in snapshot.selected_nids
        for concept_id in snapshot.covered_concept_ids_by_nid.get(note_id, ())
    } | {
        snapshot.generated_concept_id_by_card_id[card_id]
        for card_id in snapshot.selected_generated_card_ids
        if card_id in snapshot.generated_concept_id_by_card_id
    }
    required_by_concept: dict[str, set[str]] = {
        concept_id: set() for concept_id in snapshot.concept_ids
    }
    for fact_id in snapshot.required_fact_ids:
        concept_id, separator, _ = fact_id.rpartition("-M")
        if separator and concept_id in required_by_concept:
            required_by_concept[concept_id].add(fact_id)
    unresolved = set(snapshot.canonical_unresolved_fact_ids)
    return {
        concept_id: (
            "covered"
            if concept_id in selected_concepts
            else "intentional_gap"
            if required_by_concept[concept_id] and required_by_concept[concept_id] <= unresolved
            else "uncovered"
        )
        for concept_id in snapshot.concept_ids
    }


def reconcile_card_centric(snapshot: CardCentricReconciliationInput) -> ReconciliationReport:
    """Architecture A1--A10 with warnings only for A7/A9/A10."""
    legacy_snapshot = _is_legacy_card_centric_snapshot(snapshot)
    snapshot = _adapt_legacy_card_centric_snapshot(snapshot, legacy_snapshot)
    strict_v2 = (
        snapshot.pipeline_contract_version == "card_centric_v2" and not legacy_snapshot
    )
    passed: list[str] = []
    failed: list[AssertionFinding] = []
    warned: list[AssertionFinding] = []
    required = set(snapshot.required_fact_ids)
    raw_generated = snapshot.raw_generated_cards or snapshot.canonical_generated_cards
    if not raw_generated:
        raw_generated = snapshot.generated_cards
    canonical_generated = snapshot.canonical_generated_cards or snapshot.generated_cards
    terminal = snapshot.terminal_resolutions
    terminal_by_fact = {resolution.fact_id: resolution for resolution in terminal}
    semantic_review_ids = set(snapshot.semantic_review_required_card_ids)

    # S7/S8 conservation is independent of the selected S9 subset.  A
    # duplicate is a terminal duplicate resolution, never an unresolved fact.
    output_set_error = False
    if snapshot.terminal_resolutions_provided and not semantic_review_ids:
        try:
            GeneratedOutputSet(
                required_fact_ids=snapshot.required_fact_ids,
                canonical_all_generated=tuple(
                    GeneratedCardIdentity(
                        card_id=item.card_id,
                        fact_id=item.fact_id,
                        split=item.split,
                        split_index=item.split_index,
                    )
                    for item in canonical_generated
                ),
                selected_generated_card_ids=snapshot.selected_generated_card_ids,
                resolutions=terminal,
            )
        except ValidationError:
            output_set_error = True
    elif strict_v2:
        output_set_error = True

    terminal_exact = (
        len(terminal_by_fact) == len(terminal)
        and set(terminal_by_fact) == required
        and not semantic_review_ids & {
            card_id
            for resolution in terminal
            for card_id in resolution.generated_card_ids
        }
        and not output_set_error
    )
    # Legacy standalone callers did not retain an S8 terminal mapping. Keep
    # their original A1/A2 behavior; all real v2 stage snapshots use the
    # frozen terminal mapping above.
    generated_by_fact = {item.fact_id for item in canonical_generated if item.text.strip()}
    unresolved = set(snapshot.unresolved_fact_ids)
    legacy_exact = (
        required == generated_by_fact | unresolved
        and not generated_by_fact & unresolved
        and len(snapshot.required_fact_ids) == len(required)
    )
    exact_fact_terminal = (
        terminal_exact
        if snapshot.terminal_resolutions_provided
        or strict_v2
        else legacy_exact
    )
    _record(
        "A1",
        exact_fact_terminal,
        "Every uncovered-after-S6 fact must be generated or explicitly unresolved",
        passed,
        failed,
    )
    duplicates = {
        fact_id
        for fact_id in generated_by_fact
        if sum(item.fact_id == fact_id for item in canonical_generated) > 1
        and not all(item.split for item in canonical_generated if item.fact_id == fact_id)
    }
    _record(
        "A2",
        exact_fact_terminal and not duplicates,
        "Missing facts must reconcile exactly; repeated generated facts require split rows",
        passed,
        failed,
    )
    expected = set(snapshot.expected_scoped_nids)
    observed = [item.nid for item in snapshot.classifications]
    _record(
        "A3",
        set(observed) == expected and len(observed) == len(set(observed)),
        "Every scoped card must have exactly one YES/MAYBE/NO classification",
        passed,
        failed,
    )
    _record(
        "A4",
        set(snapshot.coverage) == set(snapshot.concept_ids)
        and snapshot.coverage == selected_card_centric_coverage(snapshot)
        and all(value in {"covered", "intentional_gap"} for value in snapshot.coverage.values()),
        "Every checklist concept must be selected-covered or intentionally unresolved",
        passed,
        failed,
    )
    _record(
        "duplicate_coverage",
        not strict_v2 or _duplicate_terminals_have_selected_coverage(snapshot, terminal),
        "Duplicate terminals must name a selected identity that covers the duplicated fact",
        passed,
        failed,
    )
    forbidden_by_fact = {
        fact_id: tuple(targets)
        for fact_id, targets in snapshot.forbidden_cloze_targets_by_fact.items()
    }
    _record(
        "A5",
        not _forbidden_cloze_rows(
            raw_generated,
            forbidden_by_fact,
            snapshot.forbidden_cloze_targets,
        ),
        "Generated cards cannot blank a forbidden cloze target",
        passed,
        failed,
    )
    malformed: list[str] = []
    for item in raw_generated:
        try:
            validate_gap_card_fields(item.text, item.extra)
        except GapValidationError as exc:
            malformed.append(f"{item.card_id}: {exc}")
    _record(
        "A5b", not malformed, "Generated cards fail structural cloze validation", passed, failed
    )
    _record(
        "S7",
        not strict_v2 or not canonical_generated or bool(snapshot.raw_generated_cards),
        "V2 reconciliation requires the raw S7 generated rows before S8 selection",
        passed,
        failed,
    )
    _record(
        "A6",
        len(snapshot.selected_nids) + len(snapshot.selected_generated_card_ids) >= 10,
        "YES plus generated cards must total at least 10",
        passed,
        failed,
    )
    no_rate = (
        (
            sum(item.verdict == "drop" for item in snapshot.classifications)
            / len(snapshot.classifications)
        )
        if snapshot.classifications
        else 0.0
    )
    _warn("A7", no_rate <= 0.60, "Classify NO-rate exceeds 60%", passed, warned)
    uncovered = set(snapshot.uncovered_after_s5)
    _record(
        "A8",
        uncovered <= set(snapshot.residual_ran_for),
        "Residual sweep did not run for every uncovered concept",
        passed,
        failed,
    )
    _warn(
        "A9",
        snapshot.untagged_rate <= snapshot.census_warning_rate,
        "Untagged system population exceeds census warning threshold",
        passed,
        warned,
    )
    _warn(
        "A10",
        not snapshot.prompt_sync_stale,
        "The run used a stale prompt checkout",
        passed,
        warned,
    )
    yes_rate = (
        sum(item.verdict == "keep" for item in snapshot.classifications)
        / len(snapshot.classifications)
        if snapshot.classifications
        else 0.0
    )
    history = tuple(rate for rate in snapshot.historical_yes_rates if 0 <= rate <= 1)
    if history:
        baseline = sum(history) / len(history)
        a11_ok = abs(yes_rate - baseline) <= 0.15
        message = "Classification YES rate shifts more than 15 points from rolling artifact history"
    else:
        a11_ok = 0.15 <= yes_rate <= 0.70
        message = "Classification YES rate is outside bootstrap bounds [15%, 70%]"
    _warn("A11", a11_ok, message, passed, warned)
    total = len(snapshot.selected_nids) + len(snapshot.selected_generated_card_ids)
    mandatory_selected = set(snapshot.mandatory_nids) <= set(snapshot.selected_nids) and set(
        snapshot.mandatory_generated_card_ids
    ) <= set(snapshot.selected_generated_card_ids)
    ordered_metadata = tuple(
        sorted(snapshot.selection_metadata, key=lambda item: item.selected_position)
    )
    overflow_metadata = tuple(
        item for item in ordered_metadata if item.selected_position > snapshot.cap
    )
    v2_overflow_ready = (
        strict_v2
        and len(overflow_metadata) == total - snapshot.cap
        and all(
            item.mandatory
            and item.overflow_reason is not None
            and bool(item.overflow_reason.strip())
            and item.manual_acknowledgement_required
            for item in overflow_metadata
        )
    )
    legacy_overflow_ready = (
        mandatory_selected
        and set(snapshot.selected_nids) == set(snapshot.mandatory_nids)
        and (
            set(snapshot.selected_generated_card_ids)
            == set(snapshot.mandatory_generated_card_ids)
            if snapshot.pipeline_contract_version == "card_centric_v1"
            else False
        )
    )
    overflow_ok = total <= snapshot.cap or (
        snapshot.overflow_acknowledgement is not None
        and (v2_overflow_ready or legacy_overflow_ready)
    )
    _record(
        "selection_cap",
        overflow_ok,
        "Selection cap requires immutable acknowledgement for mandatory overflow",
        passed,
        failed,
    )
    _record(
        "selection_conservation",
        set(snapshot.selected_nids)
        <= set(snapshot.eligible_yes_nids) | set(snapshot.t6_selected_nids)
        and set(snapshot.t6_selected_nids) <= set(snapshot.selected_nids)
        and set(snapshot.selected_generated_card_ids) <= set(snapshot.generated_card_ids),
        "Selected cards must be eligible, documented T6 fallback, or generated output",
        passed,
        failed,
    )
    _record(
        "selection_mandatory",
        mandatory_selected,
        "Mandatory evidence-backed cards cannot be removed during review",
        passed,
        failed,
    )
    metadata_identities = tuple(item.identity for item in snapshot.selection_metadata)
    expected_identities = tuple(
        f"existing:{note_id}" for note_id in snapshot.selected_nids
    ) + tuple(f"generated:{card_id}" for card_id in snapshot.selected_generated_card_ids)
    metadata_valid = (
        not strict_v2
        or (
            len(snapshot.selection_metadata) == total
            and set(metadata_identities) == set(expected_identities)
            and len(set(metadata_identities)) == len(metadata_identities)
            and tuple(
                item.identity
                for item in sorted(
                    snapshot.selection_metadata,
                    key=lambda item: item.selected_position,
                )
            )
            == snapshot.selection_order
            and snapshot.selected_count == total
            and snapshot.below_warning_floor == (total < WARNING_FLOOR)
        )
    )
    _record(
        "selection_metadata",
        metadata_valid,
        "Selection metadata, order, count, and below-floor flag must match the frozen selection",
        passed,
        failed,
    )
    _warn(
        "selection_warning_floor",
        total >= WARNING_FLOOR,
        "Selected deck is below the 60-card warning floor",
        passed,
        warned,
    )
    _record(
        "S8",
        not semantic_review_ids,
        "Semantic dedupe review is non-terminal and requires manual resolution before issuance",
        passed,
        failed,
    )
    return ReconciliationReport(
        passed=tuple(passed),
        failed=tuple(failed),
        warned=tuple(warned),
        can_render_envelope=not failed,
    )


def reconcile(snapshot: ReconciliationInput) -> ReconciliationReport:
    passed: list[str] = []
    failed: list[AssertionFinding] = []
    warned: list[AssertionFinding] = []

    missing = {fact_id for concept in snapshot.concepts for fact_id in concept.missing_fact_ids}
    generated = {card.fact_id for card in snapshot.generated_cards}
    unresolved = set(snapshot.unresolved_fact_ids)

    _record(
        "A1",
        missing <= generated | unresolved,
        "Every missing fact must have a generated card or unresolved record",
        passed,
        failed,
    )
    exact_fact_partition = (
        missing == generated | unresolved
        and not generated & unresolved
        and len(snapshot.unresolved_fact_ids) == len(unresolved)
    )
    _record(
        "A2",
        exact_fact_partition,
        "Missing facts must reconcile exactly by fact ID",
        passed,
        failed,
    )

    expected_audit = set(snapshot.expected_audit_nids)
    audit_counts = Counter(item.nid for item in snapshot.audit_verdicts)
    exact_audit_partition = (
        set(audit_counts) == expected_audit
        and all(count == 1 for count in audit_counts.values())
        and len(snapshot.expected_audit_nids) == len(expected_audit)
    )
    _record(
        "A3",
        exact_audit_partition,
        "Every candidate note must have exactly one audit verdict",
        passed,
        failed,
    )

    valid_statuses = {"covered", "intentional_gap"}
    _record(
        "A4",
        all(concept.status in valid_statuses for concept in snapshot.concepts),
        "Every concept must finish covered or as an intentional gap",
        passed,
        failed,
    )
    _record(
        "A5",
        not _forbidden_cloze_cards(snapshot),
        "Generated cards cannot blank a forbidden cloze target",
        passed,
        failed,
    )

    if expected_audit:
        verdict_count = len(snapshot.audit_verdicts)
        drop_count = sum(item.verdict == "drop" for item in snapshot.audit_verdicts)
        keep_count = sum(item.verdict == "keep" for item in snapshot.audit_verdicts)
        _record(
            "A6",
            verdict_count > 0 and drop_count / verdict_count <= 0.35,
            "Audit drop rate cannot exceed 35%",
            passed,
            failed,
        )
        _record(
            "A7",
            keep_count >= 10,
            "At least 10 candidate notes must survive the audit",
            passed,
            failed,
        )
    else:
        passed.extend(("A6", "A7"))

    if snapshot.concepts:
        unresolved_concepts = sum(
            bool(set(concept.missing_fact_ids) & unresolved) for concept in snapshot.concepts
        )
        unresolved_rate = unresolved_concepts / len(snapshot.concepts)
        _record(
            "A8",
            unresolved_rate <= 0.40,
            "No more than 40% of concepts may remain unresolved",
            passed,
            failed,
        )
    else:
        passed.append("A8")

    cited = {
        passage_id for concept in snapshot.concepts for passage_id in concept.cited_passage_ids
    }
    _warn(
        "A9",
        set(snapshot.source_passage_ids) <= cited,
        "One or more source passages are uncited",
        passed,
        warned,
    )
    _warn(
        "A10",
        all(concept.converged for concept in snapshot.concepts),
        "One or more concepts did not converge",
        passed,
        warned,
    )
    _warn(
        "A11",
        not snapshot.prompt_sync_stale,
        "The run used a stale prompt checkout",
        passed,
        warned,
    )
    return ReconciliationReport(
        passed=tuple(passed),
        failed=tuple(failed),
        warned=tuple(warned),
        can_render_envelope=not failed,
    )


def _forbidden_cloze_cards(
    snapshot: ReconciliationInput,
) -> tuple[str, ...]:
    forbidden = {
        _normalize_visible(target)
        for target in snapshot.forbidden_cloze_targets
        if _normalize_visible(target)
    }
    violations: list[str] = []
    for card in snapshot.generated_cards:
        answers = {
            _normalize_visible(match.group("answer")) for match in _CLOZE.finditer(card.text)
        }
        if forbidden & answers:
            violations.append(card.card_id)
    return tuple(violations)


def _forbidden_cloze_rows(
    cards: tuple[GeneratedResolution, ...],
    forbidden_by_fact: dict[str, tuple[str, ...]],
    global_forbidden: tuple[str, ...],
) -> tuple[str, ...]:
    """Check every raw S7 generated row against its own fact's exclusions."""
    violations: list[str] = []
    for card in cards:
        targets = forbidden_by_fact.get(card.fact_id, global_forbidden)
        forbidden = {
            _normalize_visible(target) for target in targets if _normalize_visible(target)
        }
        answers = {
            _normalize_visible(match.group("answer")) for match in _CLOZE.finditer(card.text)
        }
        if forbidden & answers:
            violations.append(card.card_id)
    return tuple(violations)


def _duplicate_terminals_have_selected_coverage(
    snapshot: CardCentricReconciliationInput,
    terminal: tuple[GeneratedFactResolution, ...],
) -> bool:
    """A duplicate can resolve a fact only through current selected coverage."""
    selected_notes = set(snapshot.selected_nids)
    selected_generated = set(snapshot.selected_generated_card_ids)
    for resolution in terminal:
        if resolution.kind is not GeneratedResolutionKind.DUPLICATE_OF_EXISTING:
            continue
        duplicate = resolution.duplicate_of
        if duplicate is None:  # pragma: no cover - frozen contract guarantees this
            return False
        concept_id, separator, _ = resolution.fact_id.rpartition("-M")
        if not separator:
            return False
        if duplicate.existing_note_id is not None:
            if (
                duplicate.existing_note_id not in selected_notes
                or concept_id
                not in snapshot.covered_concept_ids_by_nid.get(duplicate.existing_note_id, ())
            ):
                return False
        elif (
            duplicate.generated_card_id not in selected_generated
            or snapshot.generated_concept_id_by_card_id.get(duplicate.generated_card_id)
            != concept_id
        ):
            return False
    return True


def _is_legacy_card_centric_snapshot(snapshot: CardCentricReconciliationInput) -> bool:
    """Recognize only persisted pre-P3-D V2-shaped review snapshots.

    These rows predate the S7/S8/S9 split and therefore have no raw output,
    terminal-resolution marker, or QualitySelectionResult metadata.  New stage
    snapshots set ``terminal_resolutions_provided`` even when their resolution
    set is empty, so they never take this compatibility route.
    """
    return (
        snapshot.pipeline_contract_version == "card_centric_v2"
        and not snapshot.raw_generated_cards
        and not snapshot.terminal_resolutions_provided
        and not snapshot.terminal_resolutions
        and not snapshot.selection_metadata
        and not snapshot.selection_order
        and snapshot.selected_count is None
        and snapshot.below_warning_floor is None
    )


def _adapt_legacy_card_centric_snapshot(
    snapshot: CardCentricReconciliationInput,
    legacy_snapshot: bool,
) -> CardCentricReconciliationInput:
    """Infer the pre-P3-D raw view from the review-time selected card text."""
    if not legacy_snapshot:
        return snapshot
    return snapshot.model_copy(update={"raw_generated_cards": snapshot.generated_cards})


def _normalize_visible(value: str) -> str:
    return _SPACE.sub(" ", _HTML.sub(" ", value)).strip().casefold()


def _record(
    assertion_id: str,
    condition: bool,
    message: str,
    passed: list[str],
    failed: list[AssertionFinding],
) -> None:
    if condition:
        passed.append(assertion_id)
    else:
        failed.append(AssertionFinding(assertion_id=assertion_id, message=message))


def _warn(
    assertion_id: str,
    condition: bool,
    message: str,
    passed: list[str],
    warned: list[AssertionFinding],
) -> None:
    if condition:
        passed.append(assertion_id)
    else:
        warned.append(AssertionFinding(assertion_id=assertion_id, message=message))
