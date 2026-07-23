from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

from oms_hub.panopto.browser_domain import (
    BrowserCommandKind,
    BrowserDisposition,
    BrowserRecording,
)
from oms_hub.panopto.discovery import PollingPolicy
from oms_hub.panopto.domain import PanoptoSession
from oms_hub.panopto.matcher import RecordingMatcher
from oms_hub.panopto.repository import PanoptoRepository
from oms_hub.repositories import CatalogRepository


def validate_viewer_url(viewer_url: str, session_id: str) -> None:
    parsed = urlparse(viewer_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "lmunet.hosted.panopto.com"
        or parsed.path != "/Panopto/Pages/Viewer.aspx"
        or parse_qs(parsed.query).get("id") != [session_id]
    ):
        raise ValueError("Viewer URL must use the matching LMU Panopto session")


class PanoptoBrowserService:
    def __init__(
        self,
        catalog: CatalogRepository,
        repository: PanoptoRepository,
        matcher: RecordingMatcher,
        policy: PollingPolicy,
    ):
        self.catalog = catalog
        self.repository = repository
        self.matcher = matcher
        self.policy = policy

    def queue_scheduled_scan(self, now: datetime) -> str | None:
        local_day = now.astimezone(self.policy.timezone).date()
        today_start, today_end = self.policy.utc_bounds(local_day)
        lectures = self.catalog.list_scheduled_between(today_start, today_end)
        connection = self.repository.connection()
        if not self.policy.eligible(now, lectures, connection.enabled):
            return None
        return self.repository.queue_browser_command(
            BrowserCommandKind.SCAN,
            {"manual": False},
            now,
        )

    def queue_manual_scan(self, now: datetime) -> str:
        return self.repository.queue_browser_command(
            BrowserCommandKind.SCAN,
            {"manual": True},
            now,
        )

    def queue_connection_check(self, now: datetime) -> str:
        return self.repository.queue_browser_command(
            BrowserCommandKind.CONNECTION_CHECK,
            {},
            now,
        )

    def queue_acceptance(
        self,
        now: datetime,
        session_id: str,
        viewer_url: str,
    ) -> str:
        validate_viewer_url(viewer_url, session_id)
        return self.repository.queue_browser_command(
            BrowserCommandKind.ACCEPTANCE,
            {"session_id": session_id, "viewer_url": viewer_url},
            now,
        )

    def process_discovery(
        self,
        command_id: str,
        recordings: list[BrowserRecording],
        now: datetime,
    ) -> list[BrowserDisposition]:
        del command_id
        local_day = now.astimezone(self.policy.timezone).date()
        earliest = local_day - timedelta(days=1)
        start, _ = self.policy.utc_bounds(earliest)
        _, end = self.policy.utc_bounds(local_day)
        lectures = self.catalog.list_scheduled_between(start, end)
        dispositions: list[BrowserDisposition] = []
        for item in recordings[:100]:
            item_day = item.created_utc.astimezone(self.policy.timezone).date()
            if item_day not in {earliest, local_day}:
                continue
            validate_viewer_url(item.viewer_url, item.session_id)
            session = PanoptoSession(
                item.session_id,
                item.name,
                item.created_utc,
                item.duration_seconds,
                item.folder_name,
                None,
                None,
            )
            match = self.matcher.match(session, lectures)
            stored = self.repository.upsert_recording(session, match)
            self.repository.set_recording_source(
                stored.recording_id,
                item.viewer_url,
            )
            if match.lecture_id is not None and not match.needs_review:
                action = "extract_transcript"
                viewer_url: str | None = item.viewer_url
            else:
                action = "review"
                viewer_url = None
            dispositions.append(
                BrowserDisposition(
                    stored.recording_id,
                    item.session_id,
                    action,
                    viewer_url,
                    "; ".join(match.evidence)[:500],
                )
            )
        self.repository.mark_poll_success(now)
        return dispositions
