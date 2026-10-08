import pytest

from oms_hub.domain import StepStatus, V2StepName
from oms_hub.progress import LectureOverallStatus, overall_status


@pytest.mark.parametrize('quiz_status', [None, *StepStatus])
def test_materials_complete_independently_of_quiz_and_optional_summary(quiz_status):
    steps = V2StepName.first_release()
    assert V2StepName.QUIZ_PUBLISHED not in steps
    assert V2StepName.SUMMARY_FILED not in steps
    assert len(steps) == 11
    statuses = {step.value: StepStatus.COMPLETE.value for step in steps}
    statuses[V2StepName.SUMMARY_FILED.value] = StepStatus.FAILED.value
    if quiz_status:
        statuses[V2StepName.QUIZ_PUBLISHED.value] = quiz_status.value
    assert overall_status(statuses) == LectureOverallStatus.COMPLETE


@pytest.mark.parametrize('state,expected', [
    ('waiting', LectureOverallStatus.PROCESSING), ('failed', LectureOverallStatus.FAILED),
    ('needs_review', LectureOverallStatus.NEEDS_REVIEW),
])
def test_real_incomplete_ingestion_remains_visible(state, expected):
    statuses = {step.value: 'complete' for step in V2StepName.first_release()}
    statuses['transcript_cleaned'] = state
    assert overall_status(statuses) is expected
