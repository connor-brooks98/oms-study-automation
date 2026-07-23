from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from oms_hub.models import LectureModel
from oms_hub.panopto.domain import PanoptoSession, RecordingMatch
from oms_hub.panopto.matcher import RecordingMatcher
from oms_hub.panopto.repository import PanoptoRepository
from oms_hub.repositories import CatalogRepository


class PanoptoDiscoveryClient(Protocol):
    def search_sessions(
        self,
        search_query: str,
        max_pages: int = 3,
    ) -> list[PanoptoSession]: ...

    def get_session(self, session_id: str) -> PanoptoSession: ...


@dataclass(frozen=True, slots=True)
class DiscoverySummary:
    seen: int
    matched: int
    needs_review: int
    skipped: bool = False


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
        if local.weekday() >= 5 or not self.start <= local.time().replace(tzinfo=None) <= self.end:
            return False
        return any(self._lecture_date(lecture) == local.date() for lecture in scheduled)

    def utc_bounds(self, local_day: date) -> tuple[datetime, datetime]:
        start = datetime.combine(local_day, time.min, self.timezone)
        return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)

    def _lecture_date(self, lecture: LectureModel) -> date | None:
        if not lecture.scheduled_start_utc:
            return None
        try:
            scheduled = datetime.fromisoformat(
                lecture.scheduled_start_utc.replace("Z", "+00:00")
            )
        except ValueError:
            return None
        if scheduled.tzinfo is None:
            return None
        return scheduled.astimezone(self.timezone).date()


class PanoptoDiscovery:
    def __init__(
        self,
        catalog: CatalogRepository,
        repository: PanoptoRepository,
        client: PanoptoDiscoveryClient,
        matcher: RecordingMatcher,
        policy: PollingPolicy,
        on_match: Callable[[int, str], object] | None = None,
    ):
        self.catalog = catalog
        self.repository = repository
        self.client = client
        self.matcher = matcher
        self.policy = policy
        self.on_match = on_match

    def poll(
        self,
        now: datetime,
        manual_session_id: str | None = None,
    ) -> DiscoverySummary:
        local_day = now.astimezone(self.policy.timezone).date()
        today_start, today_end = self.policy.utc_bounds(local_day)
        today = self.catalog.list_scheduled_between(today_start, today_end)
        connection = self.repository.connection()
        if manual_session_id is None and not self.policy.eligible(
            now, today, connection.enabled
        ):
            return DiscoverySummary(0, 0, 0, skipped=True)

        backfill_start, _ = self.policy.utc_bounds(local_day - timedelta(days=1))
        lectures = self.catalog.list_scheduled_between(backfill_start, today_end)
        sessions = (
            [self.client.get_session(manual_session_id)]
            if manual_session_id
            else self._search(lectures)
        )

        seen = matched = review = 0
        allowed_days = {local_day, local_day - timedelta(days=1)}
        for summary_session in sessions[:100]:
            if summary_session.created_utc.astimezone(self.policy.timezone).date() not in allowed_days:
                continue
            seen += 1
            full_session = (
                summary_session
                if manual_session_id
                else self.client.get_session(summary_session.session_id)
            )
            match = self.matcher.match(full_session, lectures)
            if full_session.content_language != "English_USA":
                match = RecordingMatch(
                    None,
                    match.confidence,
                    match.evidence + ("English_USA captions unavailable",),
                    True,
                )
            disposition = self.repository.upsert_recording(full_session, match)
            if match.needs_review or match.lecture_id is None:
                review += 1
                continue
            matched += 1
            if full_session.caption_download_url and self.on_match is not None:
                self.on_match(
                    disposition.recording_id,
                    full_session.caption_download_url,
                )
        self.repository.mark_poll_success(now)
        return DiscoverySummary(seen, matched, review)

    def _search(self, lectures: list[LectureModel]) -> list[PanoptoSession]:
        deduplicated: dict[str, PanoptoSession] = {}
        for lecture in lectures:
            searches = (
                f"{lecture.subject} {lecture.topic}",
                f"{lecture.lecture_number} {lecture.topic}",
                f"{lecture.lecturer} {lecture.topic}",
            )
            for query in dict.fromkeys(value.strip() for value in searches if value.strip()):
                for session in self.client.search_sessions(query, max_pages=3):
                    deduplicated.setdefault(session.session_id, session)
                    if len(deduplicated) >= 100:
                        return list(deduplicated.values())
        return list(deduplicated.values())

