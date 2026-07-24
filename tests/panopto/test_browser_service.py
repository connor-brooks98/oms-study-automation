from dataclasses import replace
from datetime import UTC, datetime

import pytest

from oms_hub.panopto.browser_domain import (
    BrowserRecording,
    BrowserRequestKind,
    TranscriptExtraction,
)
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
    request_id = service.queue_scheduled_scan(NOW)

    assert request_id is not None
    request = repository.next_browser_request(NOW)
    assert request is not None
    assert request.kind is BrowserRequestKind.SCAN
    assert request.payload == {"manual": False}


def test_connection_test_creates_recoverable_request(database):
    service, repository = _service(database)

    request_id = service.queue_connection_test(NOW)

    request = repository.next_browser_request(NOW)
    assert request is not None
    assert request.id == request_id
    assert request.kind is BrowserRequestKind.CONNECTION_TEST


def test_caption_retry_stays_inside_weekday_polling_window(database):
    service, repository = _service(database)
    friday_at_close = datetime(2026, 7, 24, 23, 0, tzinfo=UTC)
    request_id = repository.create_browser_request(
        BrowserRequestKind.SCAN,
        {"manual": False},
        friday_at_close,
    )

    service.defer_captions(request_id, friday_at_close)

    assert repository.next_browser_request(
        datetime(2026, 7, 27, 13, 19, tzinfo=UTC)
    ) is None
    assert repository.next_browser_request(
        datetime(2026, 7, 27, 13, 20, tzinfo=UTC)
    ).id == request_id


def test_discovery_returns_extract_only_for_confident_match(database):
    service, repository = _service(database)

    result = service.process_discovery("command-id", [_recording()], NOW)

    assert len(result) == 1
    assert result[0].action == "download_caption"
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


def test_manual_remap_survives_discovery_replay_and_downloads(database):
    service, repository = _service(database)
    unrelated = replace(_recording(), name="Unrelated Grand Rounds")
    reviewed = service.process_discovery("first-scan", [unrelated], NOW)[0]
    lecture_id = CatalogRepository(database).list_lectures()[0].id

    repository.remap_recording(reviewed.recording_id, lecture_id)
    replayed = service.process_discovery("second-scan", [unrelated], NOW)[0]

    persisted = repository.get_recording(reviewed.recording_id)
    assert persisted.lecture_id == lecture_id
    assert persisted.review_state == "manual"
    assert replayed.action == "download_caption"
    assert replayed.viewer_url == VIEWER_URL
    assert repository.list_review_recordings() == []


def test_discovery_ignores_recordings_older_than_previous_day(database):
    service, _ = _service(database)
    old = replace(
        _recording(),
        created_utc=datetime(2026, 7, 21, 13, 5, tzinfo=UTC),
    )

    assert service.process_discovery("command-id", [old], NOW) == []


def test_incomplete_extraction_is_rejected_before_ingestion(database):
    service, _ = _service(database)

    with pytest.raises(ValueError, match="complete"):
        service.ingest_extraction(
            TranscriptExtraction(
                "command-id",
                1,
                SESSION_ID,
                VIEWER_URL,
                "English_USA",
                10,
                False,
                "partial transcript",
            )
        )
