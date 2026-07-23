import hashlib
from datetime import UTC, datetime

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.panopto.domain import PanoptoSession
from oms_hub.panopto.openai_client import CleanResult
from oms_hub.panopto.prompt import PromptLoader
from oms_hub.repositories import CatalogRepository, LectureInput


class AcceptancePanopto:
    def __init__(self):
        self.raw = b"Raw shoulder transcript with substantive medical detail."
        self.session = PanoptoSession(
            "8796399e-393c-4256-b6e4-b48f0150d156",
            "6H. MSK Shoulder Disease Injury and Treatment Joseph Silvers",
            datetime(2026, 7, 23, 13, 5, tzinfo=UTC),
            3600.0,
            "OMS II / MSK",
            "English_USA",
            "https://captions.example/file.txt",
        )

    def search_sessions(self, search_query: str, max_pages: int = 3):
        return [self.session]

    def get_session(self, session_id: str):
        assert session_id == self.session.session_id
        return self.session

    def download_captions(self, download_url: str, max_bytes: int):
        assert download_url == self.session.caption_download_url
        return self.raw


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


def test_schedule_to_panopto_to_cleaned_transcript_acceptance(tmp_path):
    prompt_path = tmp_path / "vault" / "Transcript Cleaning.md"
    prompt_path.parent.mkdir()
    prompt_path.write_text("Preserve every substantive fact.", encoding="utf-8")
    prompt_sha256 = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "ProgramData",
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        panopto_client_id="client-id",
        panopto_revision_root=tmp_path / "ProgramData" / "revisions",
        study_root=tmp_path / "OMS II",
        transcript_prompt_path=prompt_path,
    )
    app = create_app(settings)
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
    app.state.panopto_repository.set_enabled(True)
    app.state.panopto_prompt = PromptLoader(prompt_path, prompt_sha256)
    app.state.panopto_pipeline.prompt = app.state.panopto_prompt
    fake_panopto = AcceptancePanopto()
    fake_openai = AcceptanceCleaner()
    app.state.panopto_client = fake_panopto
    app.state.panopto_pipeline.panopto = fake_panopto
    app.state.panopto_pipeline.cleaner = fake_openai
    app.state.panopto_discovery.client = fake_panopto
    app.state.panopto_discovery.on_match = app.state.panopto_pipeline.ingest_captions

    app.state.panopto_discovery.poll(
        datetime(2026, 7, 23, 13, 20, tzinfo=UTC)
    )
    while app.state.panopto_pipeline.run_next():
        pass

    lecture = catalog.get_lecture(lecture_id)
    assert lecture is not None
    statuses = {step.name: step.status for step in lecture.steps}
    assert statuses["panopto_recording_found"] == "complete"
    assert statuses["transcript_downloaded"] == "complete"
    assert statuses["transcript_cleaned"] == "complete"
    assert statuses["transcript_filed"] == "complete"
    filed = list(
        (tmp_path / "OMS II" / "MSK" / "Exam 1" / "Transcripts").glob("*.txt")
    )
    assert len(filed) == 1
    raw_files = list((tmp_path / "ProgramData" / "revisions").glob("*/raw.txt"))
    assert len(raw_files) == 1

    app.state.panopto_discovery.poll(
        datetime(2026, 7, 23, 13, 35, tzinfo=UTC)
    )
    while app.state.panopto_pipeline.run_next():
        pass
    assert fake_openai.call_count == 1
    assert len(list((tmp_path / "ProgramData" / "revisions").glob("*/raw.txt"))) == 1

    first_raw = raw_files[0].read_bytes()
    fake_panopto.raw = (
        b"Corrected shoulder transcript with substantive medical detail."
    )
    app.state.panopto_discovery.poll(
        datetime(2026, 7, 23, 13, 50, tzinfo=UTC)
    )
    while app.state.panopto_pipeline.run_next():
        pass

    assert fake_openai.call_count == 2
    raw_files = list((tmp_path / "ProgramData" / "revisions").glob("*/raw.txt"))
    assert len(raw_files) == 2
    assert any(path.read_bytes() == first_raw for path in raw_files)
