"""Canonical, versioned catalog for preserved A0 failure regressions.

The catalog identifies evidence, not provider payloads.  In particular, the
candidate ``111dfaf`` provider response was not retained; its rows are covered
by deterministic behavioral reconstructions in the regression suite.
"""

from __future__ import annotations

import importlib
from copy import deepcopy
from typing import Literal, TypedDict

EvidenceQuality = Literal[
    "historical_artifact",
    "behavioral_reconstruction",
    "native_pending",
]

_CANDIDATE_111DFAF_S4B_NOTE_IDS = (
    1476661104838,
    1476661110050,
    1476669552837,
    1476669559297,
    1476669572157,
    1476669579159,
    1478915707022,
    1478915789517,
    1478973207060,
    1486519322563,
    1486522390591,
    1486522395177,
    1486522403872,
    1486522422740,
    1486522534230,
    1486522543509,
    1486522549639,
    1486522556268,
    1486522563815,
    1489975262057,
    1520730669566,
    1525899000205,
    1525899048575,
)


class HistoricalRegression(TypedDict):
    id: str
    kind: str
    evidence_quality: EvidenceQuality
    candidate: str
    executable_assertion: str
    identifiers: dict[str, object]


_CATALOG: tuple[HistoricalRegression, ...] = (
    {
        "id": "A0-H01",
        "kind": "unsupported_existing_artifact_import",
        "evidence_quality": "behavioral_reconstruction",
        "candidate": "2df0e3c",
        "executable_assertion": "test_existing_artifact_import_uses_supported_service_path",
        "identifiers": {},
    },
    {
        "id": "A0-H02",
        "kind": "windows_path_materialization",
        "evidence_quality": "behavioral_reconstruction",
        "candidate": "d250934",
        "executable_assertion": "test_windows_path_materialization_rejects_unknown_root",
        "identifiers": {},
    },
    {
        "id": "A0-H03",
        "kind": "derived_pdf_adoption_provenance",
        "evidence_quality": "historical_artifact",
        "candidate": "afc560c",
        "executable_assertion": "test_derived_pdf_adoption_preserves_provenance",
        "identifiers": {},
    },
    {
        "id": "A0-H04",
        "kind": "s2_schema_conflict",
        "evidence_quality": "behavioral_reconstruction",
        "candidate": "4f1387e",
        "executable_assertion": "test_s2_importance_depth_conflict_fails_validation",
        "identifiers": {},
    },
    {
        "id": "A0-H05",
        "kind": "anthropic_unsupported_temperature_transport",
        "evidence_quality": "historical_artifact",
        "candidate": "8a928a2",
        "executable_assertion": "test_anthropic_unsupported_temperature_is_not_transported",
        "identifiers": {},
    },
    {
        "id": "A0-H06",
        "kind": "semantic_blank_note_eligibility",
        "evidence_quality": "historical_artifact",
        "candidate": "c36ece8",
        "executable_assertion": "test_semantic_blank_note_1629377933055_is_ineligible",
        "identifiers": {"note_id": 1629377933055},
    },
    {
        "id": "A0-H07",
        "kind": "candidate_111dfaf_s4b_partition",
        "evidence_quality": "behavioral_reconstruction",
        "candidate": "111dfaf",
        "executable_assertion": (
            "test_candidate_111dfaf_s4b_partition_reconstruction_degrades_all_23_to_s4c"
        ),
        "identifiers": {
            "job_id": "7502ac16-3792-4c69-85b6-02a4596e21a4",
            "s4b_partition_degraded_note_count": 23,
            "s4b_partition_degraded_note_ids": list(_CANDIDATE_111DFAF_S4B_NOTE_IDS),
            "raw_provider_response": "unavailable",
            "s6_exact_selected_batch": "native_pending_capsule_backed_semantic_reconstruction",
            "s6_raw_provider_response": "unavailable",
        },
    },
    {
        "id": "A0-H08",
        "kind": "candidate_111dfaf_s6_partition",
        "evidence_quality": "behavioral_reconstruction",
        "candidate": "111dfaf",
        "executable_assertion": (
            "test_terminal_s6_partition_mismatch_fails_closed_with_attempt_delta"
        ),
        "identifiers": {
            "job_id": "7502ac16-3792-4c69-85b6-02a4596e21a4",
            "raw_provider_response": "unavailable",
        },
    },
)


def historical_regression_catalog() -> dict[str, object]:
    """Return the JSON-safe catalog embedded in every rehearsal capsule."""
    return {"schema_version": 2, "failures": deepcopy(list(_CATALOG))}


def historical_regression_ids() -> frozenset[str]:
    """Return immutable catalog identities for suite-coverage assertions."""
    return frozenset(entry["id"] for entry in _CATALOG)


def resolve_historical_regression_assertions() -> frozenset[str]:
    """Fail closed if catalog evidence stops naming real regression tests."""
    module = importlib.import_module("tests.anki.test_rehearsal_regressions")
    names = frozenset(entry["executable_assertion"] for entry in _CATALOG)
    unresolved = sorted(name for name in names if not callable(getattr(module, name, None)))
    if unresolved:
        raise RuntimeError(
            "historical regression assertions are unresolved: " + ", ".join(unresolved)
        )
    return names
