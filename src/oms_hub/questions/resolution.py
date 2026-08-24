"""The fail-closed authority boundary for a canonical question version."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


@dataclass(frozen=True, slots=True)
class QuestionResolution:
    question_version_id: str
    approved_objective_ids: tuple[str, ...]
    source_snapshot_hash: str
    approved: bool
    nonstale: bool
    verifiable: bool


class QuestionResolutionProvider(Protocol):
    def resolve(self, question_version_id: str) -> QuestionResolution | None:
        """Return authoritative resolution data, or no resolution."""


class QuestionResolutionFailure(StrEnum):
    INVALID_QUESTION_VERSION_ID = "invalid_question_version_id"
    ABSENT = "absent"
    MISMATCHED = "mismatched"
    STALE = "stale"
    UNAPPROVED = "unapproved"
    UNVERIFIABLE = "unverifiable"
    MISSING_OBJECTIVES = "missing_objectives"
    MISSING_SOURCE_SNAPSHOT_HASH = "missing_source_snapshot_hash"
    INVALID_RECORD = "invalid_record"


class QuestionResolutionError(ValueError):
    """A question version cannot be used without authoritative resolution."""

    def __init__(
        self,
        reason: QuestionResolutionFailure,
        question_version_id: str,
    ) -> None:
        self.reason = reason
        self.question_version_id = question_version_id
        super().__init__(
            f"question version resolution failed ({reason.value}) for "
            f"{question_version_id!r}"
        )


def resolve_question_version(
    question_version_id: str,
    provider: QuestionResolutionProvider,
) -> QuestionResolution:
    """Validate one provider result before a consumer can use it."""
    if not isinstance(question_version_id, str) or not question_version_id.strip():
        raise QuestionResolutionError(
            QuestionResolutionFailure.INVALID_QUESTION_VERSION_ID,
            question_version_id,
        )

    resolution = provider.resolve(question_version_id)
    if resolution is None:
        raise QuestionResolutionError(QuestionResolutionFailure.ABSENT, question_version_id)
    if not isinstance(resolution, QuestionResolution):
        raise QuestionResolutionError(
            QuestionResolutionFailure.INVALID_RECORD,
            question_version_id,
        )
    if resolution.question_version_id != question_version_id:
        raise QuestionResolutionError(
            QuestionResolutionFailure.MISMATCHED,
            question_version_id,
        )
    if resolution.nonstale is not True:
        raise QuestionResolutionError(QuestionResolutionFailure.STALE, question_version_id)
    if resolution.approved is not True:
        raise QuestionResolutionError(QuestionResolutionFailure.UNAPPROVED, question_version_id)
    if resolution.verifiable is not True:
        raise QuestionResolutionError(
            QuestionResolutionFailure.UNVERIFIABLE,
            question_version_id,
        )
    if not resolution.approved_objective_ids or any(
        not isinstance(objective_id, str) or not objective_id.strip()
        for objective_id in resolution.approved_objective_ids
    ):
        raise QuestionResolutionError(
            QuestionResolutionFailure.MISSING_OBJECTIVES,
            question_version_id,
        )
    if not isinstance(resolution.source_snapshot_hash, str) or not (
        resolution.source_snapshot_hash.strip()
    ):
        raise QuestionResolutionError(
            QuestionResolutionFailure.MISSING_SOURCE_SNAPSHOT_HASH,
            question_version_id,
        )
    return resolution
