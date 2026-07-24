from datetime import UTC, datetime

import pytest

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.panopto.browser_domain import BrowserRequestKind
from oms_hub.panopto.domain import PanoptoSession, RecordingMatch
from oms_hub.panopto.download_ingestion import PanoptoDownloadIngestion
from oms_hub.panopto.pipeline import TranscriptValidationError
from oms_hub.repositories import CatalogRepository, LectureInput

NOW = datetime(2026, 7, 23, 13, 20, tzinfo=UTC)


def _prepared(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'ingestion.db'}",
        panopto_inbox=tmp_path / "inbox",
        panopto_quarantine_root=tmp_path / "quarantine",
        panopto_revision_root=tmp_path / "revisions",
        study_root=tmp_path / "OMS II",
        transcript_prompt_path=tmp_path / "Transcript Cleaning.md",
        panopto_max_caption_bytes=256,
    )
    app = create_app(settings)
    ingestion = PanoptoDownloadIngestion(
        app.state.panopto_repository,
        app.state.panopto_pipeline,
        settings,
    )
    return app, ingestion, settings


def _recording_id(app) -> int:
    lecture_id = CatalogRepository(app.state.database).upsert_lecture(
        LectureInput(
            "MSK",
            1,
            6,
            "Shoulder Disease Injury and Treatment",
            "Joseph Silvers, DO",
            None,
        )
    )
    stored = app.state.panopto_repository.upsert_recording(
        PanoptoSession(
            "8796399e-393c-4256-b6e4-b48f0150d156",
            "6H. MSK Shoulder Disease Injury and Treatment",
            NOW,
            3600,
            "Shared with Me",
            "English_USA",
            None,
        ),
        RecordingMatch(lecture_id, 1.0, ("test fixture",), False),
    )
    return stored.recording_id


def test_test_download_validates_and_removes_temporary_file(tmp_path):
    app, ingestion, settings = _prepared(tmp_path)
    request_id = app.state.panopto_repository.create_browser_request(
        BrowserRequestKind.CONNECTION_TEST,
        {},
        NOW,
    )
    path = settings.panopto_inbox / request_id / "captions.txt"
    path.parent.mkdir(parents=True)
    path.write_text("00:01 First line\n00:03 Second line", encoding="utf-8")

    ingestion.complete_test_download(request_id, path, "English_USA", NOW)

    assert not path.exists()
    assert app.state.panopto_repository.connection().acceptance_validated_at
    assert list(settings.panopto_revision_root.glob("*/raw.txt")) == []
    assert app.state.panopto_repository.get_browser_request(request_id).state == "complete"


def test_production_download_preserves_immutable_raw_before_removing_inbox(tmp_path):
    app, ingestion, settings = _prepared(tmp_path)
    recording_id = _recording_id(app)
    request_id = app.state.panopto_repository.create_browser_request(
        BrowserRequestKind.SCAN,
        {"manual": False},
        NOW,
    )
    path = settings.panopto_inbox / request_id / "captions.txt"
    path.parent.mkdir(parents=True)
    path.write_text("00:01 Raw lecture", encoding="utf-8")

    revision_id = ingestion.complete_recording_download(
        request_id,
        recording_id,
        path,
        "English_USA",
        NOW,
    )

    assert not path.exists()
    assert (settings.panopto_revision_root / str(revision_id) / "raw.txt").is_file()


@pytest.mark.parametrize(
    ("filename", "payload", "language"),
    [
        ("captions.pdf", b"00:01 text", "English_USA"),
        ("captions.txt", b"<!doctype html><title>Login</title>", "English_USA"),
        ("captions.txt", b'{"error":"login"}', "English_USA"),
        ("captions.txt", b'{"captions":[]}', "English_USA"),
        ("captions.txt", b"00:01 text", "English_GBR"),
        ("captions.txt", b"x" * 257, "English_USA"),
    ],
)
def test_invalid_managed_download_is_quarantined(
    tmp_path,
    filename,
    payload,
    language,
):
    app, ingestion, settings = _prepared(tmp_path)
    request_id = app.state.panopto_repository.create_browser_request(
        BrowserRequestKind.CONNECTION_TEST,
        {},
        NOW,
    )
    path = settings.panopto_inbox / request_id / filename
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)

    with pytest.raises(TranscriptValidationError):
        ingestion.complete_test_download(request_id, path, language, NOW)

    assert not path.exists()
    quarantined = list(settings.panopto_quarantine_root.rglob(f"*{filename}"))
    assert len(quarantined) == 1
    request = app.state.panopto_repository.get_browser_request(request_id)
    assert request.state == "failed"
    assert request.error_code == "invalid_caption_download"


def test_download_outside_managed_inbox_is_rejected_without_moving_file(tmp_path):
    app, ingestion, _ = _prepared(tmp_path)
    request_id = app.state.panopto_repository.create_browser_request(
        BrowserRequestKind.CONNECTION_TEST,
        {},
        NOW,
    )
    path = tmp_path / "outside.txt"
    path.write_text("00:01 First line", encoding="utf-8")

    with pytest.raises(TranscriptValidationError):
        ingestion.complete_test_download(request_id, path, "English_USA", NOW)

    assert path.is_file()
    assert list((tmp_path / "quarantine").rglob("*")) == []
