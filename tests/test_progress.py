from oms_hub.domain import StepStatus, V2StepName
from oms_hub.progress import LectureOverallStatus, overall_status


def test_release_progress_requires_quiz_but_not_optional_summary():
    steps = V2StepName.first_release()

    assert steps[-1] == V2StepName.QUIZ_PUBLISHED
    assert V2StepName.SUMMARY_FILED not in steps
    assert len(steps) == 12
    statuses = {step.value: StepStatus.COMPLETE.value for step in steps}
    statuses[V2StepName.SUMMARY_FILED.value] = StepStatus.FAILED.value
    assert overall_status(statuses) == LectureOverallStatus.COMPLETE
