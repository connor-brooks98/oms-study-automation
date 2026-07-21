from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from oms_hub.domain import LectureStepName, StepStatus
from oms_hub.matching import CatalogLecture, LectureMatcher
from oms_hub.outlook_parser import parse_lecture_title
from oms_hub.repositories import CatalogRepository


@dataclass(frozen=True, slots=True)
class OutlookEvent:
    external_id: str
    revision: str
    subject: str
    start_utc: datetime

    @classmethod
    def from_graph(cls, raw: dict[str, Any]) -> "OutlookEvent":
        start = str(raw["start"]["dateTime"]).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(start)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return cls(
            external_id=str(raw["id"]),
            revision=str(raw.get("lastModifiedDateTime", "")),
            subject=str(raw.get("subject", "")),
            start_utc=parsed.astimezone(UTC),
        )


class CalendarClient(Protocol):
    def list_events(
        self,
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[OutlookEvent]: ...


@dataclass(frozen=True, slots=True)
class SyncResult:
    seen: int
    matched: int
    needs_review: int


class OutlookSynchronizer:
    def __init__(
        self,
        repository: CatalogRepository,
        client: CalendarClient,
    ):
        self.repository = repository
        self.client = client

    def sync_window(
        self,
        start_utc: datetime,
        end_utc: datetime,
    ) -> SyncResult:
        lectures = [
            CatalogLecture(
                item.id,
                item.subject,
                item.exam_number,
                item.lecture_number,
                item.topic,
                item.lecturer,
            )
            for item in self.repository.list_lectures()
        ]
        matcher = LectureMatcher(lectures)
        matched = 0
        review = 0
        events = self.client.list_events(start_utc, end_utc)
        for event in events:
            try:
                parsed = parse_lecture_title(event.subject)
                candidate = matcher.match(parsed)
            except ValueError as error:
                self.repository.upsert_external_event(
                    event,
                    None,
                    True,
                    str(error),
                )
                review += 1
                continue
            self.repository.upsert_external_event(
                event,
                candidate.lecture_id,
                candidate.needs_review,
                "; ".join(candidate.evidence),
            )
            if candidate.lecture_id is None:
                review += 1
                continue
            self.repository.update_schedule(
                candidate.lecture_id,
                event.start_utc.isoformat(),
                parsed.campus,
            )
            self.repository.set_step_status(
                candidate.lecture_id,
                LectureStepName.OUTLOOK_MATCHED,
                StepStatus.COMPLETE,
            )
            matched += 1
        return SyncResult(len(events), matched, review)
