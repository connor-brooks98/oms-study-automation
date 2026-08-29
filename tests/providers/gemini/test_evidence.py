from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from oms_hub.providers.gemini.evidence import validate_private_shadow_record

ROOT = Path(__file__).resolve().parents[3]


def _blocked_record(*, category: str = "provider", status: int | None = 400) -> dict[str, object]:
    return {
        "status": "blocked",
        "source_revision_hash": "a" * 64,
        "document_types": ["markdown"],
        "page_count": 1,
        "slide_count": 1,
        "provider_operation_states": ["private_shadow_failed"],
        "byte_usage": {"index_inputs": 1},
        "failure_stage": "prior_state_check",
        "failure_input_identity": "none",
        "provider_error_category": category,
        "provider_status_code": status,
        "provider_reason": "provider_bad_request",
        "provider_cleanup_outcome": "unknown",
        "provider_reconciliation_outcome": "unknown",
        "warnings": ["private_shadow_failed", "private_cleanup_unknown"],
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


def test_private_shadow_evidence_accepts_known_generic_bad_request() -> None:
    record = validate_private_shadow_record(_blocked_record(), process_exit_code=1)

    assert record["provider_reason"] == "provider_bad_request"
    assert record["provider_status_code"] == 400


@pytest.mark.parametrize(
    ("category", "status"),
    (("quota", 400), ("provider", 404)),
)
def test_private_shadow_bad_request_requires_provider_400(
    category: str, status: int
) -> None:
    with pytest.raises(ValueError):
        validate_private_shadow_record(_blocked_record(category=category, status=status), 1)


def test_private_shadow_evidence_cli_is_canonical_utf8_and_maps_errors() -> None:
    valid = _cli(_blocked_record(), 1)

    assert valid.returncode == 0
    assert valid.stderr == ""
    assert valid.stdout == json.dumps(
        _blocked_record(), sort_keys=True, separators=(",", ":")
    ) + "\n"

    invalid = _cli({"status": "blocked"}, 1)

    assert invalid.returncode == 52
    assert invalid.stdout == ""
    assert invalid.stderr == ""
