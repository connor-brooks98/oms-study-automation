import re
from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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
    generated_cards: tuple[GeneratedResolution, ...]
    # Immutable S7/S8 partition retained across review revisions. The
    # ``generated_cards`` field is the selected-only S9 view.
    canonical_generated_cards: tuple[GeneratedResolution, ...] = ()
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
    passed: list[str] = []
    failed: list[AssertionFinding] = []
    warned: list[AssertionFinding] = []
    generated_rows = tuple(item for item in snapshot.generated_cards if item.text.strip())
    generated_by_fact = {item.fact_id for item in generated_rows}
    unresolved = set(snapshot.unresolved_fact_ids)
    # A1/A2: all unresolved-after-S6 facts are represented; the caller provides
    # one generated resolution per required fact and never silently omits one.
    required = set(snapshot.required_fact_ids)
    _record(
        "A1",
        required == generated_by_fact | unresolved
        and not generated_by_fact & unresolved
        and len(snapshot.required_fact_ids) == len(required),
        "Every uncovered-after-S6 fact must be generated or explicitly unresolved",
        passed,
        failed,
    )
    duplicates = {
        fact_id
        for fact_id in generated_by_fact
        if sum(item.fact_id == fact_id for item in generated_rows) > 1
        and not all(item.split for item in generated_rows if item.fact_id == fact_id)
    }
    _record(
        "A2",
        required == generated_by_fact | unresolved
        and not generated_by_fact & unresolved
        and len(snapshot.required_fact_ids) == len(required)
        and len(snapshot.unresolved_fact_ids) == len(unresolved)
        and not duplicates,
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
    generated = tuple(item for item in snapshot.generated_cards if item.text.strip())
    _record(
        "A5",
        not _forbidden_cloze_cards(
            ReconciliationInput(
                concepts=(),
                generated_cards=generated,
                unresolved_fact_ids=(),
                expected_audit_nids=(),
                audit_verdicts=(),
                source_passage_ids=snapshot.source_passage_ids,
                forbidden_cloze_targets=snapshot.forbidden_cloze_targets,
                prompt_sync_stale=False,
            )
        ),
        "Generated cards cannot blank a forbidden cloze target",
        passed,
        failed,
    )
    malformed: list[str] = []
    for item in generated_rows:
        try:
            validate_gap_card_fields(item.text, item.extra)
        except GapValidationError as exc:
            malformed.append(f"{item.card_id}: {exc}")
    _record(
        "A5b", not malformed, "Generated cards fail structural cloze validation", passed, failed
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
    overflow_is_exactly_mandatory = set(snapshot.selected_nids) == set(
        snapshot.mandatory_nids
    ) and (
        set(snapshot.selected_generated_card_ids) == set(snapshot.mandatory_generated_card_ids)
        if snapshot.pipeline_contract_version == "card_centric_v2"
        else True
    )
    overflow_ok = total <= snapshot.cap or (
        mandatory_selected
        and overflow_is_exactly_mandatory
        and snapshot.overflow_acknowledgement is not None
        and {"token", "selection_digest", "signature"} <= set(snapshot.overflow_acknowledgement)
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
    pending_mandatory_overflow = (
        total > snapshot.cap
        and mandatory_selected
        and overflow_is_exactly_mandatory
        and snapshot.overflow_acknowledgement is None
        and {finding.assertion_id for finding in failed} == {"selection_cap"}
    )
    return ReconciliationReport(
        passed=tuple(passed),
        failed=tuple(failed),
        warned=tuple(warned),
        # Review must be reachable to obtain a server-issued acknowledgement;
        # envelope creation and apply independently require that document.
        can_render_envelope=not failed or pending_mandatory_overflow,
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
