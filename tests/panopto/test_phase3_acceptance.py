import hashlib
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.canvas.pairing import PairingService
from oms_hub.config import Settings
from oms_hub.panopto.openai_client import CleanResult
from oms_hub.panopto.prompt import PromptLoader
from oms_hub.repositories import CatalogRepository, LectureInput
from tests.canvas.test_pairing import MemorySecretStore

SESSION_ID = "8796399e-393c-4256-b6e4-b48f0150d156"
VIEWER_URL = (
    "https://lmunet.hosted.panopto.com/Panopto/Pages/Viewer.aspx?"
    f"id={SESSION_ID}"
)
NOW = datetime(2026, 7, 23, 13, 20, tzinfo=UTC)


class AcceptanceCleaner:
    def __init__(self):
        self.call_count = 0

    def clean(self, raw_text, prompt):
        self.call_count += 1
        return CleanResult(
            raw_text,
            "gpt-5.6-terra",
            f"resp_{self.call_count}",
            100,
            80,
            1450,
        )


def _prepared(tmp_path):
    prompt_path = tmp_path / "vault" / "Transcript Cleaning.md"
    prompt_path.parent.mkdir()
    prompt_path.write_text("Preserve every substantive fact.", encoding="utf-8")
    prompt_sha256 = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "ProgramData",
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        panopto_revision_root=tmp_path / "ProgramData" / "revisions",
        panopto_inbox=tmp_path / "PanoptoInbox",
        panopto_quarantine_root=tmp_path / "ProgramData" / "quarantine",
        study_root=tmp_path / "OMS II",
        transcript_prompt_path=prompt_path,
    )
    app = create_app(settings)
    app.state.canvas_pairing = PairingService(
        app.state.canvas_repository,
        MemorySecretStore(),
    )
    code = app.state.canvas_pairing.create_code()
    bearer = app.state.canvas_pairing.exchange(code.value, "acceptance-extension")
    catalog = CatalogRepository(app.state.database)
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
    app.state.panopto_repository.approve_prompt(prompt_sha256, str(prompt_path))
    app.state.panopto_prompt = PromptLoader(prompt_path, prompt_sha256)
    app.state.panopto_pipeline.prompt = app.state.panopto_prompt
    cleaner = AcceptanceCleaner()
    app.state.panopto_pipeline.cleaner = cleaner
    client = TestClient(app)
    return client, {"Authorization": f"Bearer {bearer}"}, catalog, lecture_id, cleaner


def _recording() -> dict[str, object]:
    return {
        "session_id": SESSION_ID,
        "name": "6H. MSK Shoulder Disease Injury and Treatment Joseph Silvers",
        "created_utc": "2026-07-23T13:05:00Z",
        "duration_seconds": 3600,
        "folder_name": "Shared with Me",
        "viewer_url": VIEWER_URL,
    }


def _run_browser_cycle(
    client: TestClient,
    headers: dict[str, str],
    transcript: str,
) -> None:
    request_id = client.app.state.panopto_browser.queue_manual_scan(NOW)
    request = client.get("/api/panopto/request", headers=headers).json()
    assert request["id"] == request_id
    discovery = client.post(
        f"/api/panopto/request/{request_id}/discover",
        headers=headers,
        json={"recordings": [_recording()]},
    )
    disposition = discovery.json()["dispositions"][0]
    assert disposition["action"] == "download_caption"
    path = (
        client.app.state.settings.panopto_inbox
        / request_id
        / f"{SESSION_ID}-captions.txt"
    )
    path.parent.mkdir(parents=True)
    path.write_text(transcript, encoding="utf-8")
    response = client.post(
        f"/api/panopto/request/{request_id}/download",
        headers=headers,
        json={
            "recording_id": disposition["recording_id"],
            "session_id": SESSION_ID,
            "viewer_url": VIEWER_URL,
            "language": "English_USA",
            "chrome_download_id": 17,
            "path": str(path.resolve()),
        },
    )
    assert response.status_code == 200
    client.post(
        f"/api/panopto/request/{request_id}/result",
        headers=headers,
        json={
            "status": "complete",
            "reason_code": None,
        },
    )
    while client.app.state.panopto_pipeline.run_next():
        pass


def test_browser_discovery_to_cleaned_transcript_acceptance(tmp_path):
    client, headers, catalog, lecture_id, cleaner = _prepared(tmp_path)
    first = "00:01 Raw shoulder transcript\n00:04 Substantive medical detail."

    _run_browser_cycle(client, headers, first)

    lecture = catalog.get_lecture(lecture_id)
    assert lecture is not None
    statuses = {step.name: step.status for step in lecture.steps}
    assert statuses["panopto_recording_found"] == "complete"
    assert statuses["transcript_downloaded"] == "complete"
    assert statuses["transcript_cleaned"] == "complete"
    assert statuses["transcript_filed"] == "complete"
    raw_files = list(
        (tmp_path / "ProgramData" / "revisions").glob("*/raw.txt")
    )
    assert len(raw_files) == 1
    assert raw_files[0].read_text(encoding="utf-8") == first
    filed = list(
        (tmp_path / "OMS II" / "MSK" / "Exam 1" / "Transcripts").glob("*.txt")
    )
    assert len(filed) == 1

    _run_browser_cycle(client, headers, first)
    assert cleaner.call_count == 1
    assert len(list(
        (tmp_path / "ProgramData" / "revisions").glob("*/raw.txt")
    )) == 1

    corrected = (
        "00:01 Corrected shoulder transcript\n"
        "00:04 Substantive medical detail."
    )
    _run_browser_cycle(client, headers, corrected)

    assert cleaner.call_count == 2
    all_raw = list((tmp_path / "ProgramData" / "revisions").glob("*/raw.txt"))
    assert len(all_raw) == 2
    assert {path.read_text(encoding="utf-8") for path in all_raw} == {
        first,
        corrected,
    }


def test_captions_pending_creates_no_revision_job_or_review(
    tmp_path,
    monkeypatch,
):
    client, headers, _, _, cleaner = _prepared(tmp_path)
    request_id = client.app.state.panopto_browser.queue_manual_scan(NOW)
    discovery = client.post(
        f"/api/panopto/request/{request_id}/discover",
        headers=headers,
        json={"recordings": [_recording()]},
    )
    assert discovery.status_code == 200

    class Clock:
        @classmethod
        def now(cls, timezone):
            return NOW

    monkeypatch.setattr("oms_hub.panopto.api.datetime", Clock)
    result = client.post(
        f"/api/panopto/request/{request_id}/result",
        headers=headers,
        json={
            "status": "waiting_for_captions",
            "reason_code": "captions_pending",
        },
    )

    assert result.status_code == 200
    assert cleaner.call_count == 0
    assert list((tmp_path / "ProgramData" / "revisions").glob("*/raw.txt")) == []
    assert client.app.state.panopto_repository.pending_review_count() == 0
    assert client.app.state.panopto_pipeline.run_next() is False
    assert client.app.state.panopto_repository.next_browser_request(
        NOW + timedelta(minutes=14)
    ) is None
    assert client.app.state.panopto_repository.next_browser_request(
        NOW + timedelta(minutes=15)
    ).id == request_id
