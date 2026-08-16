"""Deterministic R2 format-fidelity diagnostics; this module has no stage wiring."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from oms_hub.anki.contracts import canonical_payload_sha256
from oms_hub.anki.course_policy import CourseCurationPolicy
from oms_hub.anki.domain import SourceKind
from oms_hub.anki.sources import SourcePassage
from oms_hub.document_processing.run_styles import StyledTextRunSidecar, matches_policy_color

FidelityStatus = Literal[
    "continue",
    "blocked",
    "confirmation_required",
    "continue_degraded",
    "blocked_fallback_unavailable",
    "not_applicable",
]


class R2FidelityDiagnostic(BaseModel):
    """Frozen result of checking whether a policy's color signal is enforceable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sidecar_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    matching_colored_count: int = Field(ge=0)
    nonmatching_colored_count: int = Field(ge=0)
    unresolved_color_count: int = Field(ge=0)
    transcript_count: int = Field(ge=0)
    outline_count: int = Field(ge=0)
    status: FidelityStatus
    may_advance: bool
    degraded_mode: Literal["transcript_outline"] | None = None
    diagnostic_sha256: str = ""

    def canonical_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"diagnostic_sha256"})

    @model_validator(mode="after")
    def _validate_hash_and_outcome(self) -> R2FidelityDiagnostic:
        expected = canonical_payload_sha256(self.canonical_payload())
        if self.diagnostic_sha256 not in {"", expected}:
            raise ValueError("fidelity diagnostic hash does not match its canonical payload")
        if not self.diagnostic_sha256:
            object.__setattr__(self, "diagnostic_sha256", expected)
        if self.status == "continue_degraded":
            if not self.may_advance or self.degraded_mode != "transcript_outline":
                raise ValueError("degraded fidelity result must declare its available fallback")
            if not (self.transcript_count or self.outline_count):
                raise ValueError("degraded fidelity result requires transcript or outline fallback")
            if self.matching_colored_count:
                raise ValueError("degraded fidelity result cannot have matching colored runs")
        elif self.degraded_mode is not None:
            raise ValueError("only degraded fidelity results may declare a degraded mode")
        elif self.status in {"continue", "not_applicable"}:
            if not self.may_advance:
                raise ValueError("continuing fidelity result must advance")
            if self.status == "continue" and not self.matching_colored_count:
                raise ValueError("continue fidelity result requires a matching colored run")
        elif self.status in {
            "blocked",
            "confirmation_required",
            "blocked_fallback_unavailable",
        }:
            if self.may_advance:
                raise ValueError("blocking fidelity result cannot advance")
            if self.matching_colored_count:
                raise ValueError("blocking fidelity result cannot have matching colored runs")
            if self.status == "blocked_fallback_unavailable" and (
                self.transcript_count or self.outline_count
            ):
                raise ValueError("unavailable fallback result cannot have fallback evidence")
        return self


FormatFidelityDiagnostic = R2FidelityDiagnostic


def audit_fidelity(
    sidecar: StyledTextRunSidecar,
    policy: CourseCurationPolicy,
    *,
    source_passages: Sequence[SourcePassage] = (),
) -> R2FidelityDiagnostic:
    """Apply the exact R2 fallback matrix without inventing formatting evidence."""
    transcript_count = sum(
        passage.source_kind is SourceKind.TRANSCRIPT and bool(passage.text.strip())
        for passage in source_passages
    )
    outline_count = sum(
        passage.source_kind is SourceKind.SUMMARY and bool(passage.text.strip())
        for passage in source_passages
    )
    usable_runs = tuple(run for run in sidecar.runs if run.text.strip())
    colored = tuple(run for run in usable_runs if run.resolved_color is not None)
    matching = sum(matches_policy_color(run, policy.emphasis_colors) for run in colored)
    nonmatching = len(colored) - matching
    unresolved = sum(run.color_attempted and run.resolved_color is None for run in usable_runs)
    if policy.emphasis_mode not in {"colored_text", "combined"}:
        return _diagnostic(
            sidecar, policy, matching, nonmatching, unresolved, transcript_count, outline_count,
            "not_applicable", True,
        )
    if matching:
        return _diagnostic(
            sidecar, policy, matching, nonmatching, unresolved, transcript_count, outline_count,
            "continue", True,
        )
    if policy.missing_emphasis_fallback == "block":
        return _diagnostic(
            sidecar, policy, matching, nonmatching, unresolved, transcript_count, outline_count,
            "blocked", False,
        )
    if policy.missing_emphasis_fallback == "require_confirmation":
        return _diagnostic(
            sidecar, policy, matching, nonmatching, unresolved, transcript_count, outline_count,
            "confirmation_required", False,
        )
    if transcript_count or outline_count:
        return _diagnostic(
            sidecar, policy, matching, nonmatching, unresolved, transcript_count, outline_count,
            "continue_degraded", True, "transcript_outline",
        )
    return _diagnostic(
        sidecar, policy, matching, nonmatching, unresolved, transcript_count, outline_count,
        "blocked_fallback_unavailable", False,
    )


def _diagnostic(
    sidecar: StyledTextRunSidecar, policy: CourseCurationPolicy,
    matching: int, nonmatching: int, unresolved: int, transcript: int, outline: int,
    status: FidelityStatus, may_advance: bool,
    degraded_mode: Literal["transcript_outline"] | None = None,
) -> R2FidelityDiagnostic:
    return R2FidelityDiagnostic(
        source_sha256=sidecar.source_sha256, sidecar_sha256=sidecar.sidecar_sha256,
        policy_sha256=policy.policy_sha256, matching_colored_count=matching,
        nonmatching_colored_count=nonmatching, unresolved_color_count=unresolved,
        transcript_count=transcript, outline_count=outline, status=status,
        may_advance=may_advance, degraded_mode=degraded_mode,
    )
