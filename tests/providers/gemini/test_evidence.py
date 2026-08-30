from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from oms_hub.providers.gemini.evidence import validate_private_shadow_record

ROOT = Path(__file__).resolve().parents[3]
MAX_INDEX_INPUT_BYTES = 1_099_511_627_776


def _blocked_record(*, category: str = "provider", status: int | None = 400) -> dict[str, object]:
    return {
        "status": "blocked",
        "source_revision_hash": "a" * 64,
        "document_types": ["markdown"],
        "page_count": 1,
        "slide_count": 1,
        "provider_operation_states": ["private_shadow_failed"],
        "byte_usage": {"index_inputs": 1},
        "transient_attempts": 0,
        "failure_class": "unclassified",
        "failure_stage": "prior_state_check",
        "failure_input_identity": "none",
        "provider_error_category": category,
        "provider_status_code": status,
        "provider_reason": "provider_bad_request",
        "provider_cleanup_outcome": "unknown",
        "provider_reconciliation_outcome": "unknown",
        "warnings": ["private_shadow_failed", "private_cleanup_unknown"],
    }


def _passed_record() -> dict[str, object]:
    return {
        "status": "passed",
        "source_revision_hash": "a" * 64,
        "document_types": ["markdown"],
        "page_count": 1,
        "slide_count": 1,
        "provider_operation_states": [
            "prior_operator_state_empty",
            "store_created",
            "inputs_uploaded:1",
            "inputs_imported:1",
            "positive_query_complete",
            "wrong_scope_query_complete",
            "documents_delete_attempted:1",
            "files_delete_attempted:1",
            "file_reconciliation_empty",
            "stores_delete_attempted:1",
            "store_reconciliation_empty",
        ],
        "citation_resolution_rate": 1.0,
        "duration_ms": 1,
        "byte_usage": {"index_inputs": 1},
        "transient_attempts": 0,
        "failure_class": "none",
        "token_usage": {"input": 0, "output": 0},
        "warnings": [],
    }


def _cli(record: object, process_exit_code: int) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {"PYTHONPATH": str(ROOT / "src")}
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "oms_hub.providers.gemini.evidence",
            "--process-exit-code",
            str(process_exit_code),
        ],
        input=json.dumps(record, separators=(",", ":")) + "\n",
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env=environment,
    )


def _raw_cli(raw: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {"PYTHONPATH": str(ROOT / "src")}
    return subprocess.run(
        [sys.executable, "-m", "oms_hub.providers.gemini.evidence", "--process-exit-code", "1"],
        input=raw,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env=environment,
    )


def test_private_shadow_evidence_accepts_known_generic_bad_request() -> None:
    record = validate_private_shadow_record(_blocked_record(), process_exit_code=1)

    assert record["provider_reason"] == "provider_bad_request"
    assert record["provider_status_code"] == 400


def test_private_shadow_diagnostic_hash_is_blocked_only_and_strict() -> None:
    blocked = _blocked_record()
    blocked["diagnostic_sha256"] = "a" * 64

    validated = validate_private_shadow_record(blocked, process_exit_code=1)
    assert validated["diagnostic_sha256"] == "a" * 64

    blocked["diagnostic_sha256"] = "A" * 64
    with pytest.raises(ValueError):
        validate_private_shadow_record(blocked, process_exit_code=1)

    passed = _passed_record()
    passed["diagnostic_sha256"] = "a" * 64
    with pytest.raises(ValueError):
        validate_private_shadow_record(passed, process_exit_code=0)


def test_private_shadow_transient_failure_class_requires_bounded_retry_aggregate() -> None:
    record = _blocked_record(category="transient", status=500)
    record["provider_reason"] = "transport_error"
    record["failure_class"] = "infrastructure_transient"
    record["transient_attempts"] = 2

    validated = validate_private_shadow_record(record, process_exit_code=1)

    assert validated["failure_class"] == "infrastructure_transient"
    assert validated["transient_attempts"] == 2

    record["failure_class"] = "unclassified"
    with pytest.raises(ValueError):
        validate_private_shadow_record(record, process_exit_code=1)


@pytest.mark.parametrize(
    ("category", "status"),
    (("quota", 400), ("provider", 404)),
)
def test_private_shadow_bad_request_requires_provider_400(category: str, status: int) -> None:
    with pytest.raises(ValueError):
        validate_private_shadow_record(_blocked_record(category=category, status=status), 1)


@pytest.mark.parametrize("factory", (_passed_record, _blocked_record))
@pytest.mark.parametrize(
    "byte_count",
    (-1, 0, MAX_INDEX_INPUT_BYTES + 1, 10**1000),
)
def test_private_shadow_byte_usage_rejects_invalid_bounds(
    factory: Callable[[], dict[str, object]], byte_count: int
) -> None:
    record = factory()
    record["byte_usage"] = {"index_inputs": byte_count}

    with pytest.raises(ValueError):
        validate_private_shadow_record(record, 0 if record["status"] == "passed" else 1)


@pytest.mark.parametrize("factory", (_passed_record, _blocked_record))
@pytest.mark.parametrize("byte_count", (1, MAX_INDEX_INPUT_BYTES))
def test_private_shadow_byte_usage_accepts_exact_bounds(
    factory: Callable[[], dict[str, object]], byte_count: int
) -> None:
    record = factory()
    record["byte_usage"] = {"index_inputs": byte_count}

    assert validate_private_shadow_record(record, 0 if record["status"] == "passed" else 1)[
        "byte_usage"
    ] == {"index_inputs": byte_count}


def test_private_shadow_model_rejects_lifecycle_warning_and_count_contradictions() -> None:
    lifecycle = _passed_record()
    lifecycle["provider_operation_states"] = [
        "prior_operator_state_empty",
        "store_created",
        "inputs_imported:1",
        "inputs_imported:1",
        "positive_query_complete",
        "wrong_scope_query_complete",
        "documents_delete_attempted:1",
        "files_delete_attempted:1",
        "file_reconciliation_empty",
        "stores_delete_attempted:1",
        "store_reconciliation_empty",
    ]
    warnings = _blocked_record()
    warnings["warnings"] = ["private_cleanup_unknown", "private_shadow_failed"]
    counts = _blocked_record()
    counts["failure_stage"] = "upload_input"
    counts["failure_input_identity"] = "pptx"
    counts["provider_operation_states"] = [
        "prior_operator_state_empty",
        "store_created",
        "documents_delete_attempted:0",
        "files_delete_attempted:0",
        "stores_delete_attempted:0",
        "private_shadow_failed",
    ]

    cases: tuple[tuple[dict[str, object], int], ...] = (
        (lifecycle, 0),
        (warnings, 1),
        (counts, 1),
    )
    for record, exit_code in cases:
        with pytest.raises(ValueError):
            validate_private_shadow_record(record, exit_code)


def test_private_shadow_unknown_stage_rejects_invalid_progress() -> None:
    record = _blocked_record()
    record["failure_stage"] = "unknown"
    record["provider_operation_states"] = [
        "store_created",
        "documents_delete_attempted:0",
        "files_delete_attempted:0",
        "stores_delete_attempted:0",
        "private_shadow_failed",
    ]

    with pytest.raises(ValueError):
        validate_private_shadow_record(record, 1)


@pytest.mark.parametrize(
    ("progress", "cleanup"),
    (
        (
            ["prior_operator_state_empty"],
            [
                "documents_delete_attempted:0",
                "files_delete_attempted:0",
                "stores_delete_attempted:0",
            ],
        ),
        (
            ["prior_operator_state_empty", "store_created"],
            [
                "documents_delete_attempted:0",
                "files_delete_attempted:0",
                "stores_delete_attempted:1",
            ],
        ),
        (
            [
                "prior_operator_state_empty",
                "store_created",
                "inputs_uploaded:1",
                "inputs_imported:1",
            ],
            [
                "documents_delete_attempted:1",
                "files_delete_attempted:1",
                "stores_delete_attempted:1",
            ],
        ),
        (
            [
                "prior_operator_state_empty",
                "store_created",
                "inputs_uploaded:1",
                "inputs_imported:1",
                "positive_query_complete",
            ],
            [
                "documents_delete_attempted:1",
                "files_delete_attempted:1",
                "stores_delete_attempted:1",
            ],
        ),
        (
            [
                "prior_operator_state_empty",
                "store_created",
                "inputs_uploaded:1",
                "inputs_imported:1",
                "positive_query_complete",
                "wrong_scope_query_complete",
            ],
            [
                "documents_delete_attempted:1",
                "files_delete_attempted:1",
                "stores_delete_attempted:1",
            ],
        ),
    ),
)
def test_private_shadow_unknown_stage_accepts_valid_progress_shapes(
    progress: list[str], cleanup: list[str]
) -> None:
    record = _blocked_record()
    record["failure_stage"] = "unknown"
    record["provider_operation_states"] = [*progress, *cleanup, "private_shadow_failed"]

    assert validate_private_shadow_record(record, 1)["failure_stage"] == "unknown"


def test_private_shadow_evidence_cli_is_canonical_utf8_and_maps_errors() -> None:
    valid = _cli(_blocked_record(), 1)

    assert valid.returncode == 0
    assert valid.stderr == ""
    assert (
        valid.stdout == json.dumps(_blocked_record(), sort_keys=True, separators=(",", ":")) + "\n"
    )

    invalid = _cli({"status": "blocked"}, 1)

    assert invalid.returncode == 52
    assert invalid.stdout == ""
    assert invalid.stderr == ""


def test_private_shadow_evidence_cli_accepts_windows_utf8_bom() -> None:
    raw = "\ufeff" + json.dumps(_blocked_record(), separators=(",", ":"))

    result = _raw_cli(raw)

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout == json.dumps(_blocked_record(), sort_keys=True, separators=(",", ":")) + "\n"


@pytest.mark.parametrize("raw", ("PRIVATE SOURCE CONTENT", "{"))
def test_private_shadow_evidence_cli_silently_rejects_malformed_input(raw: str) -> None:
    result = _raw_cli(raw)

    assert result.returncode == 51
    assert result.stdout == ""
    assert result.stderr == ""


def test_private_shadow_evidence_cli_silently_rejects_deep_json() -> None:
    result = _raw_cli("[" * 100_000 + "0" + "]" * 100_000)

    assert result.returncode == 51
    assert result.stdout == ""
    assert result.stderr == ""
