from pydantic import BaseModel


class StepApi(BaseModel):
    name: str
    status: str
    detail: str | None


class LectureApi(BaseModel):
    id: int
    subject: str
    exam_number: int
    lecture_number: int
    topic: str
    lecturer: str
    exam_date: str | None
    scheduled_start_utc: str | None
    campus: str | None
    steps: list[StepApi]
