import re
from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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


def reconcile(snapshot: ReconciliationInput) -> ReconciliationReport:
    passed: list[str] = []
    failed: list[AssertionFinding] = []
    warned: list[AssertionFinding] = []

    missing = {
        fact_id
        for concept in snapshot.concepts
        for fact_id in concept.missing_fact_ids
    }
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
        drop_count = sum(
            item.verdict == "drop" for item in snapshot.audit_verdicts
        )
        keep_count = sum(
            item.verdict == "keep" for item in snapshot.audit_verdicts
        )
        _warn(
            "A6",
            verdict_count > 0 and drop_count / verdict_count <= 0.35,
            "Audit drop rate cannot exceed 35%",
            passed,
            warned,
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
            bool(set(concept.missing_fact_ids) & unresolved)
            for concept in snapshot.concepts
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
        passage_id
        for concept in snapshot.concepts
        for passage_id in concept.cited_passage_ids
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
            _normalize_visible(match.group("answer"))
            for match in _CLOZE.finditer(card.text)
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
        failed.append(
            AssertionFinding(assertion_id=assertion_id, message=message)
        )


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
        warned.append(
            AssertionFinding(assertion_id=assertion_id, message=message)
        )
