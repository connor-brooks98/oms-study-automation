from oms_hub.domain import V2StepName


def test_release_progress_includes_summary_and_quiz_generation():
    steps = V2StepName.first_release()

    assert steps[-2:] == (
        V2StepName.SUMMARY_FILED,
        V2StepName.QUIZ_PUBLISHED,
    )
    assert len(steps) == 13
