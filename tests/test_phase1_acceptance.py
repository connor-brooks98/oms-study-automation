from datetime import UTC, date, datetime

from fastapi.testclient import TestClient
from openpyxl import Workbook

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.outlook.sync import OutlookEvent, OutlookSynchronizer
from oms_hub.repositories import CatalogRepository
from oms_hub.tracker_import import TrackerImporter


class AcceptanceCalendar:
    def list_events(
        self,
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[OutlookEvent]:
        return [
            OutlookEvent(
                "e-4",
                "r-1",
                "4K. Heme/Lymph: Anemia I | Jun Wang, MD, PhD",
                datetime(2026, 7, 1, 13, tzinfo=UTC),
            )
        ]


def test_tracker_to_outlook_to_dashboard_acceptance(tmp_path):
    tracker = tmp_path / "tracker.xlsx"
    workbook = Workbook()
    dates = workbook.active
    dates.title = "EXAM DATES"
    dates.append(["DATE:", "BLOCK EXAM:"])
    dates.append([date(2026, 7, 3), "HEME 1"])
    sheet = workbook.create_sheet("HEME 1")
    sheet.append(["HEME / LYMPH 1"])
    sheet.append(["#", "Lecture Title", "Lecturer"])
    sheet.append([4, "Anemia I", "Jun Wang, MD, PhD"])
    workbook.save(tracker)
    settings = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'acceptance.db'}",
    )
    app = create_app(settings)
    repository = CatalogRepository(app.state.database)

    TrackerImporter(repository).import_once(tracker)
    synchronizer = OutlookSynchronizer(
        repository,
        AcceptanceCalendar(),
    )
    result = synchronizer.sync_window(
        datetime(2026, 7, 1, tzinfo=UTC),
        datetime(2026, 7, 2, tzinfo=UTC),
    )
    page = TestClient(app).get("/")

    assert result.matched == 1
    assert "Lecture 04: Anemia I" in page.text
    assert "1/21" in page.text
