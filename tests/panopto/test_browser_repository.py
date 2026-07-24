from datetime import UTC, datetime, timedelta

from oms_hub.panopto.browser_domain import BrowserCommandKind
from oms_hub.panopto.domain import PanoptoSession, RecordingMatch
from oms_hub.panopto.repository import PanoptoRepository

NOW = datetime(2026, 7, 23, 13, 20, tzinfo=UTC)


def _recording_id(repository: PanoptoRepository) -> int:
    disposition = repository.upsert_recording(
        PanoptoSession(
            "8796399e-393c-4256-b6e4-b48f0150d156",
            "6H. MSK Shoulder Disease Injury and Treatment",
            NOW,
            3600,
            "Shared with Me",
            None,
            None,
        ),
        RecordingMatch(None, 0.0, ("test fixture",), True),
    )
    return disposition.recording_id


def test_browser_command_queue_coalesces_and_claims_once(database):
    repository = PanoptoRepository(database)

    first = repository.queue_browser_command(
        BrowserCommandKind.SCAN, {"manual": False}, NOW
    )
    second = repository.queue_browser_command(
        BrowserCommandKind.SCAN, {"manual": False}, NOW
    )

    assert first == second
    claimed = repository.claim_browser_command(NOW)
    assert claimed is not None
    assert claimed.id == first
    assert claimed.kind is BrowserCommandKind.SCAN
    assert claimed.payload == {"manual": False}
    assert repository.claim_browser_command(NOW) is None


def test_browser_heartbeat_truncates_error(database):
    repository = PanoptoRepository(database)

    repository.heartbeat("panopto_login_required", NOW, "x" * 5000)

    connection = repository.connection()
    assert connection.state == "panopto_login_required"
    assert connection.last_error is not None
    assert len(connection.last_error) == 1000


def test_recording_viewer_url_is_kept_in_additive_source_table(database):
    repository = PanoptoRepository(database)
    recording_id = _recording_id(repository)
    viewer_url = (
        "https://lmunet.hosted.panopto.com/Panopto/Pages/Viewer.aspx?"
        "id=8796399e-393c-4256-b6e4-b48f0150d156"
    )

    repository.set_recording_source(recording_id, viewer_url)

    assert repository.get_recording_source(recording_id) == viewer_url


def test_recording_source_rejects_non_lmu_origin(database):
    repository = PanoptoRepository(database)
    recording_id = _recording_id(repository)

    try:
        repository.set_recording_source(recording_id, "https://evil.example/viewer")
    except ValueError as error:
        assert "LMU Panopto" in str(error)
    else:
        raise AssertionError("non-LMU URL was accepted")


def test_stale_running_command_is_requeued(database):
    repository = PanoptoRepository(database)
    command_id = repository.queue_browser_command(
        BrowserCommandKind.SCAN, {"manual": False}, NOW
    )
    assert repository.claim_browser_command(NOW) is not None

    recovered = repository.recover_stale_browser_commands(
        NOW + timedelta(minutes=6),
        timeout_seconds=300,
    )

    assert recovered == 1
    claimed = repository.claim_browser_command(NOW + timedelta(minutes=6))
    assert claimed is not None
    assert claimed.id == command_id


def test_explicit_retry_requeues_a_running_browser_command(database):
    repository = PanoptoRepository(database)
    command_id = repository.queue_browser_command(
        BrowserCommandKind.ACCEPTANCE,
        {"session_id": "old", "viewer_url": "https://example.test/old"},
        NOW,
    )
    assert repository.claim_browser_command(NOW) is not None

    retried_id = repository.queue_browser_command(
        BrowserCommandKind.ACCEPTANCE,
        {"session_id": "new", "viewer_url": "https://example.test/new"},
        NOW + timedelta(minutes=1),
        retry_running=True,
    )

    assert retried_id == command_id
    retried = repository.claim_browser_command(NOW + timedelta(minutes=1))
    assert retried is not None
    assert retried.id == command_id
    assert retried.payload == {
        "session_id": "new",
        "viewer_url": "https://example.test/new",
    }
