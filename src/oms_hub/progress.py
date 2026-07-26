from collections.abc import Mapping
from enum import StrEnum

from oms_hub.domain import StepStatus, V2StepName


class ProgressSection(StrEnum):
    SLIDES = "slides"
    TRANSCRIPT = "transcript"
    LATER_WORKFLOW = "later_workflow"


class LectureOverallStatus(StrEnum):
    NOT_STARTED = "not_started"
    PROCESSING = "processing"
    NEEDS_REVIEW = "needs_review"
    COMPLETE = "complete"
    QUARANTINED = "quarantined"
    FAILED = "failed"


STEP_SECTIONS: dict[V2StepName, ProgressSection] = {
    V2StepName.SLIDES_RECEIVED: ProgressSection.SLIDES,
    V2StepName.SLIDES_MATCHED: ProgressSection.SLIDES,
    V2StepName.SLIDES_VALIDATED: ProgressSection.SLIDES,
    V2StepName.PDF_CONVERTED: ProgressSection.SLIDES,
    V2StepName.SLIDES_FILED: ProgressSection.SLIDES,
    V2StepName.ICLOUD_PDF_STAGED: ProgressSection.SLIDES,
    V2StepName.TRANSCRIPT_RECEIVED: ProgressSection.TRANSCRIPT,
    V2StepName.TRANSCRIPT_MATCHED: ProgressSection.TRANSCRIPT,
    V2StepName.TRANSCRIPT_VALIDATED: ProgressSection.TRANSCRIPT,
    V2StepName.TRANSCRIPT_CLEANED: ProgressSection.TRANSCRIPT,
    V2StepName.TRANSCRIPT_FILED: ProgressSection.TRANSCRIPT,
    V2StepName.NOTEBOOK_CREATED: ProgressSection.LATER_WORKFLOW,
    V2StepName.SOURCES_UPLOADED: ProgressSection.LATER_WORKFLOW,
    V2StepName.SUMMARY_FILED: ProgressSection.LATER_WORKFLOW,
    V2StepName.QUIZ_PUBLISHED: ProgressSection.LATER_WORKFLOW,
    V2StepName.ANKI_SYNCED: ProgressSection.LATER_WORKFLOW,
}

SLIDE_PIPELINE_STEPS = (
    V2StepName.SLIDES_VALIDATED,
    V2StepName.PDF_CONVERTED,
    V2StepName.SLIDES_FILED,
    V2StepName.ICLOUD_PDF_STAGED,
)

_SATISFIED = {StepStatus.COMPLETE.value, StepStatus.SKIPPED.value}
_ACTIVE = {StepStatus.QUEUED.value, StepStatus.RUNNING.value}
_MATCH_STEPS = {
    V2StepName.SLIDES_MATCHED.value,
    V2StepName.TRANSCRIPT_MATCHED.value,
}


def overall_status(steps: Mapping[str, str]) -> LectureOverallStatus:
    first_release = {
        step.value: steps.get(step.value, StepStatus.WAITING.value)
        for step in V2StepName.first_release()
    }
    statuses = tuple(first_release.values())

    if StepStatus.FAILED.value in statuses:
        return LectureOverallStatus.FAILED
    if any(
        first_release[name] == StepStatus.NEEDS_REVIEW.value
        for name in _MATCH_STEPS
    ):
        return LectureOverallStatus.QUARANTINED
    if StepStatus.NEEDS_REVIEW.value in statuses:
        return LectureOverallStatus.NEEDS_REVIEW
    if any(status in _ACTIVE for status in statuses):
        return LectureOverallStatus.PROCESSING
    if all(status in _SATISFIED for status in statuses):
        return LectureOverallStatus.COMPLETE
    if any(status in _SATISFIED for status in statuses):
        return LectureOverallStatus.PROCESSING
    return LectureOverallStatus.NOT_STARTED
