from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from oms_hub.models import LectureModel


class PollingPolicy:
    def __init__(self, timezone: str, start: str, end: str):
        self.timezone = ZoneInfo(timezone)
        self.start = time.fromisoformat(start)
        self.end = time.fromisoformat(end)

    def eligible(
        self,
        now: datetime,
        scheduled: list[LectureModel],
        enabled: bool,
    ) -> bool:
        if not enabled:
            return False
        local = now.astimezone(self.timezone)
        local_time = local.time().replace(tzinfo=None)
        if local.weekday() >= 5 or not self.start <= local_time <= self.end:
            return False
        return any(self._lecture_date(lecture) == local.date() for lecture in scheduled)

    def utc_bounds(self, local_day: date) -> tuple[datetime, datetime]:
        start = datetime.combine(local_day, time.min, self.timezone)
        return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)

    def _lecture_date(self, lecture: LectureModel) -> date | None:
        if not lecture.scheduled_start_utc:
            return None
        try:
            scheduled = datetime.fromisoformat(lecture.scheduled_start_utc)
        except ValueError:
            return None
        if scheduled.tzinfo is None:
            return None
        return scheduled.astimezone(self.timezone).date()
