"""Server-owned Lecture 07 downgrade validation.

The fixture inputs and expected verdicts live in this repository.  Callers may
choose a provider/model, but never submit a verdict list or a validation token.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from oms_hub.anki.card_centric_contracts import CardClassification, CardClassificationBatchOutput
from oms_hub.anki.card_centric_fixture import LECTURE07_FIXTURE, evaluate_lecture07_fixture
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.structured import StructuredTextService

FIXTURE_VERSION = "lecture07-real-v1"
FIXTURE_INPUTS = (
    (7001, "Mitapivat activates pyruvate kinase in PK deficiency.", ("C01",)),
    (7002, "C. perfringens can cause massive intravascular hemolysis.", ("C02",)),
    (7003, "Graft-versus-host disease follows allogeneic transplant.", ("C03",)),
    (7004, "Acute hemolytic transfusion reaction begins with fever and back pain.", ("C04",)),
    (7005, "Warm AIHA is associated with SLE and CLL.", ("C05",)),
    (7006, "Avoid oxidant drugs in G6PD deficiency.", ("C06",)),
    (7101, "Cortisol deficiency is unrelated to this hematology lecture.", ()),
    (7102, "Hemophilia inheritance is not a G6PD treatment fact.", ()),
    (7201, "Heinz bodies may appear without the disease name on the card.", ("C06",)),
    (7202, "Bite cells may appear without the disease name on the card.", ("C06",)),
    (7301, "Flagged-only card must not cover a ledger concept.", ()),
    (7302, "Summary-only card must not cover a ledger concept.", ()),
)
FIXTURE_SHA256 = hashlib.sha256(
    json.dumps(FIXTURE_INPUTS, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


class FixtureClassifier(Protocol):
    def classify_fixture(
        self,
        *,
        provider: str,
        model: str,
        fixture_version: str,
        fixture_inputs: tuple[tuple[object, ...], ...],
    ) -> tuple[CardClassification, ...]: ...


@dataclass(slots=True)
class ProductionFixtureClassifier:
    """Adapter over the configured structured provider; tests inject the protocol."""

    structured: StructuredTextService

    def classify_fixture(
        self,
        *,
        provider: str,
        model: str,
        fixture_version: str,
        fixture_inputs: tuple[tuple[object, ...], ...],
    ) -> tuple[CardClassification, ...]:
        result = self.structured.generate_json(
            (
                "Classify every real Lecture07 Anki card. Return exact IDs and "
                "evidence-aware verdicts."
            ),
            json.dumps(
                {
                    "fixture_version": fixture_version,
                    "source_prefix": "Lecture07 hematology source excerpts",
                    "cards": fixture_inputs,
                }
            ),
            provider=ProviderName(provider),
            model=model,
            output_model=CardClassificationBatchOutput,
        )
        return result.value.results


@dataclass(frozen=True, slots=True)
class FixtureValidation:
    provider: str
    model: str
    passed: bool
    metrics: dict[str, object]


def validate_fixture(
    classifier: FixtureClassifier, *, provider: str, model: str
) -> FixtureValidation:
    observed = classifier.classify_fixture(
        provider=provider,
        model=model,
        fixture_version=FIXTURE_VERSION,
        fixture_inputs=FIXTURE_INPUTS,
    )
    report = evaluate_lecture07_fixture(observed)
    expected_ids = {case.note_id for case in LECTURE07_FIXTURE}
    observed_ids = {item.note_id for item in observed}
    coverage = {
        concept
        for item in observed
        if item.verdict == "YES" and not item.flags
        for concept in item.covered_concept_ids
    }
    metrics: dict[str, object] = {
        "agreement": report.agreement,
        "false_keeps": report.false_keeps,
        "false_drops": report.false_drops,
        "six_concept_coverage": {f"C{i:02d}" for i in range(1, 7)} <= coverage,
        "fixture_note_count": len(observed_ids),
        "expected_note_count": len(expected_ids),
        "fixture_sha256": FIXTURE_SHA256,
        "fixture_version": FIXTURE_VERSION,
    }
    return FixtureValidation(
        provider, model, report.passed and bool(metrics["six_concept_coverage"]), metrics
    )
