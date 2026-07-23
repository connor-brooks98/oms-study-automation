from dataclasses import replace
from datetime import UTC, datetime

from oms_hub.domain import LectureStepName, StepStatus
from oms_hub.panopto.discovery import PanoptoDiscovery, PollingPolicy
from oms_hub.panopto.domain import PanoptoSession
from oms_hub.panopto.matcher import RecordingMatcher
from oms_hub.panopto.repository import PanoptoRepository
from oms_hub.repositories import CatalogRepository, LectureInput


def scheduled_lecture(database):
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
    lecture = catalog.get_lecture(lecture_id)
    assert lecture is not None
    return catalog, lecture


def msk_session() -> PanoptoSession:
    return PanoptoSession(
        "8796399e-393c-4256-b6e4-b48f0150d156",
        "6H. MSK Shoulder Disease Injury and Treatment Joseph Silvers",
        datetime(2026, 7, 23, 13, 5, tzinfo=UTC),
        3600.0,
        "OMS II / MSK",
        "English_USA",
        "https://captions.example/file.txt",
    )


def test_polling_window_weekday_schedule_and_enabled_gate(database):
    _, lecture = scheduled_lecture(database)
    policy = PollingPolicy("America/New_York", "09:20", "19:00")

    assert not policy.eligible(
        datetime(2026, 7, 23, 13, 19, tzinfo=UTC), [lecture], True
    )
    assert policy.eligible(
        datetime(2026, 7, 23, 13, 20, tzinfo=UTC), [lecture], True
    )
    assert policy.eligible(
        datetime(2026, 7, 23, 23, 0, tzinfo=UTC), [lecture], True
    )
    assert not policy.eligible(
        datetime(2026, 7, 23, 23, 1, tzinfo=UTC), [lecture], True
    )
    assert not policy.eligible(
        datetime(2026, 7, 23, 13, 20, tzinfo=UTC), [lecture], False
    )
    saturday = replace(
        msk_session(),
        created_utc=datetime(2026, 7, 25, 13, 20, tzinfo=UTC),
    )
    assert not policy.eligible(saturday.created_utc, [lecture], True)


def test_matcher_requires_schedule_and_title_evidence(database):
    _, lecture = scheduled_lecture(database)

    match = RecordingMatcher().match(msk_session(), [lecture])

    assert match.lecture_id == lecture.id
    assert match.confidence >= 0.90
    assert not match.needs_review


def test_only_unmatched_recording_is_not_enough(database):
    _, lecture = scheduled_lecture(database)
    unrelated = replace(msk_session(), name="Unrelated Grand Rounds")

    match = RecordingMatcher().match(unrelated, [lecture])

    assert match.lecture_id is None
    assert match.needs_review


class FakePanopto:
    def __init__(self, value: PanoptoSession):
        self.value = value
        self.searches: list[str] = []

    def search_sessions(self, search_query: str, max_pages: int = 3):
        self.searches.append(search_query)
        return [replace(self.value, caption_download_url=None)]

    def get_session(self, session_id: str):
        assert session_id == self.value.session_id
        return self.value


def test_discovery_deduplicates_matches_and_hands_caption_url_only_in_memory(database):
    catalog, lecture = scheduled_lecture(database)
    repository = PanoptoRepository(database)
    client = FakePanopto(msk_session())
    ingested: list[tuple[int, str]] = []
    discovery = PanoptoDiscovery(
        catalog,
        repository,
        client,
        RecordingMatcher(),
        PollingPolicy("America/New_York", "09:20", "19:00"),
        on_match=lambda recording_id, url: ingested.append((recording_id, url)),
    )
    repository.set_enabled(True)

    summary = discovery.poll(datetime(2026, 7, 23, 13, 20, tzinfo=UTC))

    assert summary.seen == 1
    assert summary.matched == 1
    assert summary.needs_review == 0
    assert 1 <= len(client.searches) <= 3
    assert ingested and ingested[0][1] == "https://captions.example/file.txt"
    updated = catalog.get_lecture(lecture.id)
    assert updated is not None
    statuses = {step.name: step.status for step in updated.steps}
    assert statuses[LectureStepName.PANOPTO_RECORDING_FOUND] == StepStatus.COMPLETE


def test_non_english_session_is_persisted_for_review_not_ingested(database):
    catalog, _ = scheduled_lecture(database)
    repository = PanoptoRepository(database)
    client = FakePanopto(replace(msk_session(), content_language="English_GBR"))
    ingested: list[tuple[int, str]] = []
    discovery = PanoptoDiscovery(
        catalog,
        repository,
        client,
        RecordingMatcher(),
        PollingPolicy("America/New_York", "09:20", "19:00"),
        on_match=lambda recording_id, url: ingested.append((recording_id, url)),
    )
    repository.set_enabled(True)

    summary = discovery.poll(datetime(2026, 7, 23, 13, 20, tzinfo=UTC))

    assert summary.needs_review == 1
    assert ingested == []
