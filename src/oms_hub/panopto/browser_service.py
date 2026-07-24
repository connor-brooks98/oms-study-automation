from datetime import datetime, timedelta
from typing import Protocol
from urllib.parse import parse_qs, urlparse

from oms_hub.panopto.browser_domain import (
    BrowserCommandKind,
    BrowserDisposition,
    BrowserRecording,
    BrowserRequestKind,
    TranscriptExtraction,
)
from oms_hub.panopto.discovery import PollingPolicy
from oms_hub.panopto.domain import PanoptoSession
from oms_hub.panopto.matcher import RecordingMatcher
from oms_hub.panopto.repository import PanoptoRepository
from oms_hub.repositories import CatalogRepository


class TranscriptIngestor(Protocol):
    def ingest_transcript(self, recording_id: int, payload: bytes) -> int: ...


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
        pipeline: TranscriptIngestor | None = None,
    ):
        self.catalog = catalog
        self.repository = repository
        self.matcher = matcher
        self.policy = policy
        self.pipeline = pipeline

    def queue_scheduled_scan(self, now: datetime) -> str | None:
        local_day = now.astimezone(self.policy.timezone).date()
        today_start, today_end = self.policy.utc_bounds(local_day)
        lectures = self.catalog.list_scheduled_between(today_start, today_end)
        connection = self.repository.connection()
        if not self.policy.eligible(now, lectures, connection.enabled):
            return None
        return self.repository.create_browser_request(
            BrowserRequestKind.SCAN,
            {"manual": False},
            now,
        )

    def queue_manual_scan(self, now: datetime) -> str:
        return self.repository.create_browser_request(
            BrowserRequestKind.SCAN,
            {"manual": True},
            now,
        )

    def queue_connection_test(self, now: datetime) -> str:
        return self.repository.create_browser_request(
            BrowserRequestKind.CONNECTION_TEST,
            {},
            now,
        )

    def queue_connection_check(self, now: datetime) -> str:
        return self.queue_connection_test(now)

    def defer_captions(self, request_id: str, now: datetime) -> datetime:
        local = now.astimezone(self.policy.timezone)
        candidate = local + timedelta(minutes=15)
        candidate_time = candidate.time().replace(tzinfo=None)
        if (
            candidate.weekday() < 5
            and candidate.date() == local.date()
            and candidate_time <= self.policy.end
        ):
            eligible = candidate
        else:
            next_day = local.date() + timedelta(days=1)
            while next_day.weekday() >= 5:
                next_day += timedelta(days=1)
            eligible = datetime.combine(
                next_day,
                self.policy.start,
                self.policy.timezone,
            )
        due = eligible.astimezone(now.tzinfo)
        self.repository.wait_browser_request(
            request_id,
            "captions_pending",
            due,
            now,
        )
        return due

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
            retry_running=True,
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
                action = "download_caption"
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

    def ingest_extraction(self, extraction: TranscriptExtraction) -> int:
        if not extraction.complete:
            raise ValueError("Transcript extraction is not complete")
        if extraction.language != "English_USA":
            raise ValueError("English (United States) transcript is required")
        if extraction.line_count <= 0:
            raise ValueError("Transcript line count is invalid")
        validate_viewer_url(extraction.viewer_url, extraction.session_id)
        recording = self.repository.get_recording(extraction.recording_id)
        if recording.session_id != extraction.session_id:
            raise ValueError("Transcript recording does not match the session")
        source = self.repository.get_recording_source(extraction.recording_id)
        if source != extraction.viewer_url:
            raise ValueError("Transcript viewer URL does not match discovery")
        if self.pipeline is None:
            raise ValueError("Transcript pipeline is unavailable")
        return self.pipeline.ingest_transcript(
            extraction.recording_id,
            extraction.text.encode("utf-8"),
        )
