"""Canonical, provider-safe private-shadow evidence."""

from __future__ import annotations

import argparse
import json
import re
import sys
import warnings
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

_STATE_PATTERN = re.compile(
    r"^(?P<name>inputs_(?:uploaded|imported)|(?:documents|files|stores)_delete_attempted):"
    r"(?P<count>0|[1-9][0-9]{0,4})$"
)
warnings.filterwarnings(
    "ignore",
    message=r"'oms_hub\.providers\.gemini\.evidence' found in sys\.modules",
    category=RuntimeWarning,
)
_FIXED_STATES = frozenset(
    {
        "prior_operator_state_empty",
        "store_created",
        "positive_query_complete",
        "wrong_scope_query_complete",
        "file_reconciliation_empty",
        "store_reconciliation_empty",
        "private_shadow_failed",
    }
)
_PROVIDER_REASONS = frozenset(
    {
        "none",
        "invalid_argument",
        "provider_bad_request",
        "sdk_contract",
        "timeout",
        "transport_error",
        "unknown_provider",
        "unsupported_mime_type",
    }
)
_FAILURE_STAGES = frozenset(
    {
        "prior_state_check",
        "create_store",
        "upload_input",
        "import_input",
        "wait_for_import",
        "positive_query",
        "positive_validation",
        "negative_query",
        "negative_validation",
        "cleanup",
        "unknown",
    }
)
_INPUT_IDENTITIES = frozenset(
    {"none", "pptx", "pdf", "normalized_markdown", "visual_asset", "unknown"}
)
_WARNINGS = frozenset(
    {
        "citation_document_identity_unavailable",
        "citation_document_uri_absent",
        "citation_document_uri_invalid",
        "citation_excerpt_invalid",
        "citation_excerpt_absent",
        "citation_excerpt_mismatch",
        "citation_excerpt_unavailable",
        "citation_file_absent",
        "citation_file_invalid",
        "citation_metadata_invalid",
        "citation_metadata_absent",
        "citation_annotations_absent",
        "citation_annotations_invalid",
        "citation_content_absent",
        "citation_content_invalid",
        "citation_page_invalid",
        "citation_page_absent",
        "citation_page_mismatch",
        "citation_scope_mismatch",
        "citation_steps_absent",
        "citation_steps_invalid",
        "citation_wrong_document",
        "citation_wrong_file",
        "negative_answer_invalid",
        "positive_answer_invalid",
        "positive_answer_missing_marker",
        "positive_answer_unsupported",
        "positive_citation_missing",
        "positive_citation_unresolved",
        "private_cleanup_failed",
        "private_cleanup_unknown",
        "private_citation_unresolved",
        "private_reconciliation_failed",
        "private_shadow_failed",
        "private_usage_invalid",
        "private_wrong_scope_retrieved",
        "structured_output_invalid",
        "structured_output_absent",
        "structured_output_unavailable",
        "usage_input_absent",
        "usage_input_invalid",
        "usage_output_absent",
        "usage_output_invalid",
        "usage_count_invalid",
    }
)
_MAX_INDEX_INPUT_BYTES = 1_099_511_627_776
_MAX_TRANSIENT_ATTEMPTS = 10_000


def _validate_common(record: PrivateShadowPassed | PrivateShadowBlocked) -> None:
    document_types = record.document_types
    if document_types != sorted(set(document_types)):
        raise ValueError("document types must be sorted and unique")
    if set(record.byte_usage) != {"index_inputs"}:
        raise ValueError("byte usage keys are invalid")
    index_inputs = record.byte_usage["index_inputs"]
    if type(index_inputs) is not int or index_inputs < 1 or index_inputs > _MAX_INDEX_INPUT_BYTES:
        raise ValueError("index input byte usage is invalid")
    if any(warning not in _WARNINGS for warning in record.warnings):
        raise ValueError("warnings are not allowlisted")
    if len(set(record.warnings)) != len(record.warnings):
        raise ValueError("warnings must be unique")
    for state in record.provider_operation_states:
        if state in _FIXED_STATES:
            continue
        matched = _STATE_PATTERN.fullmatch(state)
        if matched is None or int(matched.group("count")) > 10_000:
            raise ValueError("operation state is not allowlisted")


class PrivateShadowPassed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["passed"]
    source_revision_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_types: list[Literal["image", "markdown", "pdf", "pptx"]] = Field(
        min_length=1, max_length=4
    )
    page_count: int = Field(ge=1, le=10_000)
    slide_count: int = Field(ge=1, le=10_000)
    provider_operation_states: list[str] = Field(min_length=1, max_length=32)
    citation_resolution_rate: float
    duration_ms: int = Field(ge=0, le=86_400_000)
    byte_usage: dict[str, int]
    transient_attempts: int = Field(ge=0, le=_MAX_TRANSIENT_ATTEMPTS)
    failure_class: Literal["none"]
    token_usage: dict[str, int]
    warnings: list[str]

    @model_validator(mode="after")
    def validate_record(self) -> PrivateShadowPassed:
        _validate_common(self)
        states = self.provider_operation_states
        if (
            len(states) != 11
            or states[0:2] != ["prior_operator_state_empty", "store_created"]
            or states[4:6] != ["positive_query_complete", "wrong_scope_query_complete"]
            or states[8:]
            != [
                "file_reconciliation_empty",
                "stores_delete_attempted:1",
                "store_reconciliation_empty",
            ]
            or self.warnings
            or self.citation_resolution_rate != 1.0
            or set(self.token_usage) != {"input", "output"}
            or any(value < 0 or value > 1_000_000_000 for value in self.token_usage.values())
        ):
            raise ValueError("passed private-shadow evidence is inconsistent")
        uploads = _STATE_PATTERN.fullmatch(states[2])
        imports = _STATE_PATTERN.fullmatch(states[3])
        documents = _STATE_PATTERN.fullmatch(states[6])
        files = _STATE_PATTERN.fullmatch(states[7])
        if (
            uploads is None
            or imports is None
            or documents is None
            or files is None
            or uploads.group("name") != "inputs_uploaded"
            or imports.group("name") != "inputs_imported"
            or documents.group("name") != "documents_delete_attempted"
            or files.group("name") != "files_delete_attempted"
            or len(
                {
                    uploads.group("count"),
                    imports.group("count"),
                    documents.group("count"),
                    files.group("count"),
                }
            )
            != 1
            or int(uploads.group("count")) < 1
        ):
            raise ValueError("passed private-shadow lifecycle is incomplete")
        return self


class PrivateShadowBlocked(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["blocked"]
    source_revision_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_types: list[Literal["image", "markdown", "pdf", "pptx"]] = Field(
        min_length=1, max_length=4
    )
    page_count: int = Field(ge=1, le=10_000)
    slide_count: int = Field(ge=1, le=10_000)
    provider_operation_states: list[str] = Field(min_length=1, max_length=32)
    byte_usage: dict[str, int]
    transient_attempts: int = Field(ge=0, le=_MAX_TRANSIENT_ATTEMPTS)
    failure_class: Literal["infrastructure_transient", "unclassified"]
    diagnostic_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    failure_stage: Literal[
        "prior_state_check",
        "create_store",
        "upload_input",
        "import_input",
        "wait_for_import",
        "positive_query",
        "positive_validation",
        "negative_query",
        "negative_validation",
        "cleanup",
        "unknown",
    ]
    failure_input_identity: Literal[
        "none", "pptx", "pdf", "normalized_markdown", "visual_asset", "unknown"
    ]
    provider_error_category: Literal[
        "none", "authentication", "quota", "transient", "contract", "provider"
    ]
    provider_status_code: int | None = Field(default=None, ge=100, le=599)
    provider_reason: Literal[
        "none",
        "invalid_argument",
        "provider_bad_request",
        "sdk_contract",
        "timeout",
        "transport_error",
        "unknown_provider",
        "unsupported_mime_type",
    ]
    provider_cleanup_outcome: Literal["complete", "failed", "unknown"]
    provider_reconciliation_outcome: Literal["empty", "not_empty", "unknown"]
    warnings: list[str] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def validate_record(self) -> PrivateShadowBlocked:
        _validate_common(self)
        states = self.provider_operation_states
        if states[-1] != "private_shadow_failed":
            raise ValueError("blocked evidence requires a failure state")
        if self.provider_error_category == "none" and (
            self.provider_status_code is not None or self.provider_reason != "none"
        ):
            raise ValueError("absent diagnostics are inconsistent")
        if (self.failure_class == "infrastructure_transient") != (
            self.provider_error_category == "transient"
        ):
            raise ValueError("failure class contradicts provider category")
        if self.provider_reason in {
            "invalid_argument",
            "provider_bad_request",
            "unsupported_mime_type",
        } and (self.provider_error_category != "provider" or self.provider_status_code != 400):
            raise ValueError("bad-request diagnostics require provider HTTP 400")
        input_stage = self.failure_stage in {"upload_input", "import_input", "wait_for_import"}
        if input_stage != (self.failure_input_identity != "none"):
            raise ValueError("failure input identity contradicts its stage")
        if self.failure_stage == "prior_state_check":
            if states != ["private_shadow_failed"]:
                raise ValueError("prior-state failure carried impossible progress")
        else:
            _validate_blocked_lifecycle(self)
        if (
            self.provider_cleanup_outcome == "complete"
            and self.provider_reconciliation_outcome != "empty"
        ):
            raise ValueError("completed cleanup must reconcile empty")
        file_empty = "file_reconciliation_empty" in states
        store_empty = "store_reconciliation_empty" in states
        if self.provider_reconciliation_outcome == "empty" and not (file_empty and store_empty):
            raise ValueError("empty reconciliation lacks final checks")
        if self.provider_reconciliation_outcome == "not_empty" and file_empty and store_empty:
            raise ValueError("nonempty reconciliation contradicts final checks")
        expected = _expected_warnings(self)
        if self.warnings != expected:
            raise ValueError("warning order is inconsistent")
        return self


def _validate_blocked_lifecycle(record: PrivateShadowBlocked) -> None:
    states = record.provider_operation_states
    cleanup_start = next(
        (
            index
            for index, state in enumerate(states)
            if state.startswith("documents_delete_attempted:")
        ),
        -1,
    )
    if cleanup_start < 1:
        raise ValueError("cleanup progress is absent")
    progress, cleanup = states[:cleanup_start], states[cleanup_start:]
    required = ("documents_delete_attempted", "files_delete_attempted", "stores_delete_attempted")
    counts: dict[str, int] = {}
    position = 0
    for name in required[:2]:
        matched = _STATE_PATTERN.fullmatch(cleanup[position]) if position < len(cleanup) else None
        if matched is None or matched.group("name") != name:
            raise ValueError("cleanup progress is malformed")
        counts[name] = int(matched.group("count"))
        position += 1
        if (
            name == "files_delete_attempted"
            and position < len(cleanup)
            and cleanup[position] == "file_reconciliation_empty"
        ):
            position += 1
    matched = _STATE_PATTERN.fullmatch(cleanup[position]) if position < len(cleanup) else None
    if matched is None or matched.group("name") != required[2]:
        raise ValueError("store cleanup progress is malformed")
    counts[required[2]] = int(matched.group("count"))
    position += 1
    if position < len(cleanup) and cleanup[position] == "store_reconciliation_empty":
        position += 1
    if cleanup[position:] != ["private_shadow_failed"]:
        raise ValueError("cleanup progress ordering is invalid")
    kind, input_count = _progress_kind(progress)
    if kind == "invalid":
        raise ValueError("operation progress is invalid")
    expected_kind = {
        "create_store": "before_store",
        "upload_input": "after_store",
        "import_input": "after_store",
        "wait_for_import": "after_store",
        "positive_query": "after_inputs",
        "positive_validation": "after_inputs",
        "negative_query": "after_positive",
        "negative_validation": "after_positive",
        "cleanup": "after_negative",
    }.get(record.failure_stage)
    if record.failure_stage == "unknown":
        expected_kind = kind
    if kind != expected_kind:
        raise ValueError("failure stage contradicts operation progress")
    if kind in {"after_inputs", "after_positive", "after_negative"}:
        if input_count is None or (
            counts["documents_delete_attempted"] != input_count
            or counts["files_delete_attempted"] != input_count
            or counts["stores_delete_attempted"] != 1
        ):
            raise ValueError("completed-input cleanup counts are invalid")
    elif kind == "after_store" and counts["stores_delete_attempted"] != 1:
        raise ValueError("post-store cleanup count is invalid")


def _progress_kind(progress: list[str]) -> tuple[str, int | None]:
    if progress == ["prior_operator_state_empty"]:
        return "before_store", None
    if progress == ["prior_operator_state_empty", "store_created"]:
        return "after_store", None
    if len(progress) < 4 or progress[:2] != ["prior_operator_state_empty", "store_created"]:
        return "invalid", None
    uploads = _STATE_PATTERN.fullmatch(progress[2])
    imports = _STATE_PATTERN.fullmatch(progress[3])
    if (
        uploads is None
        or imports is None
        or uploads.group("name") != "inputs_uploaded"
        or imports.group("name") != "inputs_imported"
        or uploads.group("count") != imports.group("count")
        or int(uploads.group("count")) < 1
    ):
        return "invalid", None
    if len(progress) == 4:
        return "after_inputs", int(uploads.group("count"))
    if len(progress) == 5 and progress[4] == "positive_query_complete":
        return "after_positive", int(uploads.group("count"))
    if len(progress) == 6 and progress[4:] == [
        "positive_query_complete",
        "wrong_scope_query_complete",
    ]:
        return "after_negative", int(uploads.group("count"))
    return "invalid", None


def _expected_warnings(record: PrivateShadowBlocked) -> list[str]:
    if record.failure_stage == "cleanup":
        if record.provider_cleanup_outcome == "failed":
            return ["private_cleanup_failed"]
        if record.provider_cleanup_outcome == "unknown":
            return ["private_cleanup_failed", "private_cleanup_unknown"]
        return []
    if record.warnings[0] in {"private_cleanup_failed", "private_cleanup_unknown"}:
        return []
    expected = [record.warnings[0]]
    if record.provider_cleanup_outcome == "failed":
        expected.append("private_cleanup_failed")
    elif record.provider_cleanup_outcome == "unknown":
        expected.append("private_cleanup_unknown")
    return expected


def validate_private_shadow_record(value: object, process_exit_code: int) -> dict[str, object]:
    """Validate one sanitized private-shadow result and return canonical data."""

    if isinstance(process_exit_code, bool) or not isinstance(process_exit_code, int):
        raise ValueError("process exit code is invalid")
    if not isinstance(value, Mapping):
        raise ValueError("private-shadow evidence must be an object")
    status = value.get("status")
    if status == "passed":
        if process_exit_code != 0:
            raise ValueError("passed evidence requires a zero process exit")
        return PrivateShadowPassed.model_validate(value).model_dump(mode="json")
    if status == "blocked":
        if process_exit_code == 0:
            raise ValueError("blocked evidence requires a nonzero process exit")
        return PrivateShadowBlocked.model_validate(value).model_dump(mode="json")
    raise ValueError("private-shadow evidence status is invalid")


def failure_record(
    preflight: Mapping[str, object] | None,
    error: BaseException,
    *,
    failure_stage: str,
    states: list[str],
    cleanup_outcome: str,
    reconciliation_outcome: str,
    input_identity: str = "none",
    transient_attempts: int = 0,
    diagnostic_sha256: str | None = None,
) -> PrivateShadowBlocked:
    """Build a bounded blocked record without retaining exception content."""

    category = getattr(error, "category", "none")
    if category not in {"none", "authentication", "quota", "transient", "contract", "provider"}:
        category = "none"
    status_code = getattr(error, "provider_status_code", None)
    if (
        isinstance(status_code, bool)
        or not isinstance(status_code, int)
        or not 100 <= status_code <= 599
    ):
        status_code = None
    reason = getattr(error, "diagnostic_code", "none")
    if reason not in _PROVIDER_REASONS:
        reason = "none"
    if category == "provider" and status_code == 400 and reason == "unknown_provider":
        reason = "provider_bad_request"
    warning = getattr(error, "reason", None)
    if warning not in _WARNINGS:
        warning = "private_shadow_failed"
    if cleanup_outcome == "failed" and warning != "private_cleanup_failed":
        warnings = [warning, "private_cleanup_failed"]
    elif cleanup_outcome == "unknown" and warning != "private_cleanup_failed":
        warnings = [warning, "private_cleanup_unknown"]
    elif cleanup_outcome == "unknown":
        warnings = ["private_cleanup_failed", "private_cleanup_unknown"]
    else:
        warnings = [warning]
    fallback = {
        "source_revision_hash": "0" * 64,
        "document_types": ["markdown"],
        "page_count": 1,
        "slide_count": 1,
        "byte_usage": {"index_inputs": 1},
    }
    source = fallback if preflight is None else preflight
    if (
        type(transient_attempts) is not int
        or not 0 <= transient_attempts <= _MAX_TRANSIENT_ATTEMPTS
    ):
        transient_attempts = 0
    if diagnostic_sha256 is not None and (
        not isinstance(diagnostic_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", diagnostic_sha256) is None
    ):
        raise ValueError("diagnostic SHA-256 is invalid")
    return PrivateShadowBlocked.model_validate(
        {
            "status": "blocked",
            "source_revision_hash": source["source_revision_hash"],
            "document_types": source["document_types"],
            "page_count": source["page_count"],
            "slide_count": source["slide_count"],
            "provider_operation_states": [*states, "private_shadow_failed"],
            "byte_usage": source["byte_usage"],
            "transient_attempts": transient_attempts,
            "failure_class": (
                "infrastructure_transient" if category == "transient" else "unclassified"
            ),
            "diagnostic_sha256": diagnostic_sha256,
            "failure_stage": failure_stage if failure_stage in _FAILURE_STAGES else "unknown",
            "failure_input_identity": (
                input_identity if input_identity in _INPUT_IDENTITIES else "unknown"
            ),
            "provider_error_category": category,
            "provider_status_code": status_code,
            "provider_reason": reason,
            "provider_cleanup_outcome": (
                cleanup_outcome
                if cleanup_outcome in {"complete", "failed", "unknown"}
                else "unknown"
            ),
            "provider_reconciliation_outcome": (
                reconciliation_outcome
                if reconciliation_outcome in {"empty", "not_empty", "unknown"}
                else "unknown"
            ),
            "warnings": warnings,
        }
    )


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--process-exit-code", required=True, type=int)
    arguments = parser.parse_args(argv)
    try:
        raw_bytes = sys.stdin.buffer.read(200 * 1024 + 1)
        if len(raw_bytes) > 200 * 1024:
            return 51
        raw = raw_bytes.decode("utf-8-sig")
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, MemoryError, OverflowError, RecursionError):
        return 51
    try:
        canonical = validate_private_shadow_record(value, arguments.process_exit_code)
    except (ValidationError, ValueError):
        return 52
    sys.stdout.write(json.dumps(canonical, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "PrivateShadowBlocked",
    "PrivateShadowPassed",
    "failure_record",
    "validate_private_shadow_record",
]
