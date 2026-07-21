from datetime import UTC, datetime

import httpx

from oms_hub.domain import LectureStepName, StepStatus
from oms_hub.outlook.client import GraphCalendarClient
from oms_hub.outlook.sync import OutlookEvent, OutlookSynchronizer
from oms_hub.repositories import CatalogRepository, LectureInput


class FakeCalendarClient:
    def list_events(
        self,
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[OutlookEvent]:
        return [
            OutlookEvent(
                external_id="event-1",
                revision="rev-1",
                subject="4K. Heme/Lymph: Anemia I | Jun Wang, MD, PhD",
                start_utc=datetime(2026, 7, 1, 13, 0, tzinfo=UTC),
            )
        ]


class FakeTokens:
    def access_token(self) -> str:
        return "test-token"


def test_sync_matches_event_and_completes_outlook_step(database):
    repository = CatalogRepository(database)
    lecture_id = repository.upsert_lecture(
        LectureInput(
            "Heme/Lymph",
            1,
            4,
            "Anemia I",
            "Jun Wang, MD, PhD",
            "2026-07-03",
        )
    )
    synchronizer = OutlookSynchronizer(repository, FakeCalendarClient())

    result = synchronizer.sync_window(
        datetime(2026, 7, 1, tzinfo=UTC),
        datetime(2026, 7, 2, tzinfo=UTC),
    )

    lecture = repository.get_lecture(lecture_id)
    assert lecture is not None
    outlook_step = next(
        step
        for step in lecture.steps
        if step.name == LectureStepName.OUTLOOK_MATCHED.value
    )
    assert result.matched == 1
    assert outlook_step.status == StepStatus.COMPLETE.value
    assert lecture.campus == "K"
    assert lecture.scheduled_start_utc == "2026-07-01T13:00:00+00:00"


def test_repeat_sync_updates_one_external_event(database):
    repository = CatalogRepository(database)
    repository.upsert_lecture(
        LectureInput(
            "Heme/Lymph",
            1,
            4,
            "Anemia I",
            "Jun Wang, MD, PhD",
            "2026-07-03",
        )
    )
    synchronizer = OutlookSynchronizer(repository, FakeCalendarClient())
    start = datetime(2026, 7, 1, tzinfo=UTC)
    end = datetime(2026, 7, 2, tzinfo=UTC)

    synchronizer.sync_window(start, end)
    synchronizer.sync_window(start, end)

    assert len(repository.list_external_events()) == 1


def test_graph_client_follows_next_link_verbatim():
    first_url = "https://graph.microsoft.com/v1.0/me/calendarView"
    next_url = (
        "https://graph.microsoft.com/v1.0/me/calendarView?$skiptoken=opaque"
    )
    requested_urls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if len(requested_urls) == 1:
            return httpx.Response(
                200,
                json={"value": [], "@odata.nextLink": next_url},
            )
        return httpx.Response(200, json={"value": []})

    transport = httpx.MockTransport(handle)
    client = GraphCalendarClient(
        FakeTokens(),
        httpx.Client(transport=transport),
    )

    assert (
        client.list_events(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
        )
        == []
    )
    assert requested_urls[0].startswith(first_url)
    assert requested_urls[1] == next_url


def test_graph_event_timestamp_is_normalized_to_utc():
    event = OutlookEvent.from_graph(
        {
            "id": "event-1",
            "lastModifiedDateTime": "rev-1",
            "subject": "Lecture",
            "start": {"dateTime": "2026-07-01T09:00:00-04:00"},
        }
    )

    assert event.start_utc == datetime(2026, 7, 1, 13, 0, tzinfo=UTC)
