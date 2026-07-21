from oms_hub.domain import LectureStepName, StepStatus
from oms_hub.repositories import CatalogRepository

DEPENDENCIES: dict[LectureStepName, tuple[LectureStepName, ...]] = {
    LectureStepName.PPTX_DOWNLOADED: (
        LectureStepName.CANVAS_PPTX_FOUND,
    ),
    LectureStepName.PDF_FILED: (LectureStepName.PPTX_DOWNLOADED,),
    LectureStepName.GOODNOTES_DELIVERED: (LectureStepName.PDF_FILED,),
    LectureStepName.TRANSCRIPT_DOWNLOADED: (
        LectureStepName.PANOPTO_RECORDING_FOUND,
    ),
    LectureStepName.TRANSCRIPT_CLEANED: (
        LectureStepName.TRANSCRIPT_DOWNLOADED,
    ),
    LectureStepName.TRANSCRIPT_FILED: (
        LectureStepName.TRANSCRIPT_CLEANED,
    ),
    LectureStepName.PPTX_UPLOADED: (
        LectureStepName.NOTEBOOK_EXISTS,
        LectureStepName.PPTX_DOWNLOADED,
    ),
    LectureStepName.TRANSCRIPT_UPLOADED: (
        LectureStepName.NOTEBOOK_EXISTS,
        LectureStepName.TRANSCRIPT_FILED,
    ),
    LectureStepName.SOURCES_SELECTED: (
        LectureStepName.PPTX_UPLOADED,
        LectureStepName.TRANSCRIPT_UPLOADED,
    ),
    LectureStepName.SUMMARY_GENERATED: (
        LectureStepName.SOURCES_SELECTED,
    ),
    LectureStepName.SUMMARY_FILED: (
        LectureStepName.SUMMARY_GENERATED,
    ),
    LectureStepName.QUIZ_PROMPT_COMPLETED: (
        LectureStepName.SOURCES_SELECTED,
    ),
    LectureStepName.GEMINI_QUIZ_GENERATED: (
        LectureStepName.QUIZ_PROMPT_COMPLETED,
    ),
    LectureStepName.GEMINI_QUIZ_VERIFIED: (
        LectureStepName.GEMINI_QUIZ_GENERATED,
    ),
    LectureStepName.SHARE_LINK_CAPTURED: (
        LectureStepName.GEMINI_QUIZ_VERIFIED,
    ),
    LectureStepName.GOOGLE_DOC_UPDATED: (
        LectureStepName.SHARE_LINK_CAPTURED,
    ),
}

_SATISFIED = {StepStatus.COMPLETE.value, StepStatus.SKIPPED.value}
_ACTIONABLE = {StepStatus.WAITING.value, StepStatus.QUEUED.value}


class InvalidTransition(ValueError):
    pass


class ChecklistService:
    def __init__(self, repository: CatalogRepository):
        self.repository = repository

    def transition(
        self,
        lecture_id: int,
        step: LectureStepName,
        status: StepStatus,
        detail: str | None = None,
    ) -> None:
        lecture = self.repository.get_lecture(lecture_id)
        if lecture is None:
            raise KeyError(lecture_id)
        current = {item.name: item.status for item in lecture.steps}
        if status is StepStatus.COMPLETE:
            missing = [
                dependency.value
                for dependency in DEPENDENCIES.get(step, ())
                if current.get(dependency.value) not in _SATISFIED
            ]
            if missing:
                raise InvalidTransition(
                    f"missing prerequisites: {', '.join(missing)}"
                )
        self.repository.set_step_status(lecture_id, step, status, detail)

    def next_actionable_steps(
        self,
        lecture_id: int,
    ) -> list[LectureStepName]:
        lecture = self.repository.get_lecture(lecture_id)
        if lecture is None:
            raise KeyError(lecture_id)
        current = {item.name: item.status for item in lecture.steps}
        return [
            step
            for step in LectureStepName
            if current[step.value] in _ACTIONABLE
            and all(
                current[dependency.value] in _SATISFIED
                for dependency in DEPENDENCIES.get(step, ())
            )
        ]
