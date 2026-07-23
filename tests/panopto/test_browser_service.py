from dataclasses import replace
from datetime import UTC, datetime

import pytest

from oms_hub.panopto.browser_domain import BrowserCommandKind, BrowserRecording
from oms_hub.panopto.browser_service import PanoptoBrowserService
from oms_hub.panopto.discovery import PollingPolicy
from oms_hub.panopto.matcher import RecordingMatcher
from oms_hub.panopto.repository import PanoptoRepository
from oms_hub.repositories import CatalogRepository, LectureInput

NOW = datetime(2026, 7, 23, 13, 20, tzinfo=UTC)
SESSION_ID = "8796399e-393c-4256-b6e4-b48f0150d156"
VIEWER_URL = (
    "https://lmunet.hosted.panopto.com/Panopto/Pages/Viewer.aspx?"
    f"id={SESSION_ID}"
)


def _service(database) -> tuple[PanoptoBrowserService, PanoptoRepository]:
    catalog = CatalogRepository(database)
    lecture_id = catalog.upsert_lecture(
        LectureInput(
            "MSK",
            1,
            6,
            "Shoulder Disease Injury and Treatment",
            "Joseph Silvers, DO",
            None,
        )
    )
    catalog.update_schedule(lecture_id, "2026-07-23T12:00:00+00:00", "DCOM 101")
    repository = PanoptoRepository(database)
    service = PanoptoBrowserService(
        catalog,
        repository,
        RecordingMatcher("America/New_York"),
        PollingPolicy("America/New_York", "09:20", "19:00"),
    )
    return service, repository


def _recording() -> BrowserRecording:
    return BrowserRecording(
        SESSION_ID,
        "6H. MSK Shoulder Disease Injury and Treatment Joseph Silvers",
        datetime(2026, 7, 23, 13, 5, tzinfo=UTC),
        3600,
        "Shared with Me",
        VIEWER_URL,
    )


def test_scheduled_scan_queues_only_in_eligible_window(database):
    service, repository = _service(database)
    repository.set_enabled(True)

    assert service.queue_scheduled_scan(
        datetime(2026, 7, 23, 13, 19, tzinfo=UTC)
    ) is None
    command_id = service.queue_scheduled_scan(NOW)

    assert command_id is not None
    command = repository.claim_browser_command(NOW)
    assert command is not None
    assert command.kind is BrowserCommandKind.SCAN
    assert command.payload == {"manual": False}


def test_discovery_returns_extract_only_for_confident_match(database):
    service, repository = _service(database)

    result = service.process_discovery("command-id", [_recording()], NOW)

    assert len(result) == 1
    assert result[0].action == "extract_transcript"
    assert result[0].viewer_url == VIEWER_URL
    assert repository.get_recording_source(result[0].recording_id) == VIEWER_URL


def test_discovery_rejects_wrong_origin(database):
    service, _ = _service(database)

    with pytest.raises(ValueError, match="LMU Panopto"):
        service.process_discovery(
            "command-id",
            [replace(_recording(), viewer_url="https://evil.example/x")],
            NOW,
        )


def test_unmatched_recording_is_reviewed_without_extraction(database):
    service, _ = _service(database)
    unrelated = replace(_recording(), name="Unrelated Grand Rounds")

    result = service.process_discovery("command-id", [unrelated], NOW)

    assert result[0].action == "review"
    assert result[0].viewer_url is None


def test_discovery_ignores_recordings_older_than_previous_day(database):
    service, _ = _service(database)
    old = replace(
        _recording(),
        created_utc=datetime(2026, 7, 21, 13, 5, tzinfo=UTC),
    )

    assert service.process_discovery("command-id", [old], NOW) == []
