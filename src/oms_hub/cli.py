import argparse
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import uvicorn

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.matching import CatalogLecture, LectureMatcher
from oms_hub.outlook.auth import OutlookTokenProvider
from oms_hub.outlook.client import GraphCalendarClient
from oms_hub.outlook.sync import OutlookSynchronizer
from oms_hub.outlook_parser import parse_lecture_title
from oms_hub.repositories import CatalogRepository
from oms_hub.scheduler import build_scheduler
from oms_hub.security.secret_store import KeyringSecretStore
from oms_hub.tracker_import import TrackerImporter


def _repository(settings: Settings) -> CatalogRepository:
    database = Database(settings.database_url)
    database.create_schema()
    return CatalogRepository(database)


def _calendar(settings: Settings) -> GraphCalendarClient:
    if not settings.outlook_client_id:
        raise SystemExit(
            "Set OMS_HUB_OUTLOOK_CLIENT_ID before using Outlook commands"
        )
    tokens = OutlookTokenProvider(
        settings.outlook_client_id,
        settings.outlook_tenant,
        KeyringSecretStore(),
    )
    return GraphCalendarClient(tokens)


def import_tracker(args: argparse.Namespace) -> int:
    result = TrackerImporter(_repository(Settings())).import_once(
        Path(args.path)
    )
    print(
        f"imported={result.imported} issues={result.issues} "
        f"sha256={result.source_sha256}"
    )
    return 0


def outlook_login(args: argparse.Namespace) -> int:
    settings = Settings()
    if not settings.outlook_client_id:
        raise SystemExit(
            "Set OMS_HUB_OUTLOOK_CLIENT_ID before logging in"
        )
    OutlookTokenProvider(
        settings.outlook_client_id,
        settings.outlook_tenant,
        KeyringSecretStore(),
    ).login()
    print("Outlook login complete")
    return 0


def _window(
    days: int,
    start_date: date | None = None,
) -> tuple[datetime, datetime]:
    if not 1 <= days <= 90:
        raise ValueError("days must be between 1 and 90")
    chosen = start_date or datetime.now(UTC).date()
    return (
        datetime.combine(chosen, time.min, tzinfo=UTC),
        datetime.combine(
            chosen + timedelta(days=days),
            time.min,
            tzinfo=UTC,
        ),
    )


def sync_outlook(args: argparse.Namespace) -> int:
    settings = Settings()
    start, end = _window(args.days)
    result = OutlookSynchronizer(
        _repository(settings),
        _calendar(settings),
    ).sync_window(start, end)
    print(
        f"seen={result.seen} matched={result.matched} "
        f"needs_review={result.needs_review}"
    )
    return 0


def dry_run(args: argparse.Namespace) -> int:
    settings = Settings()
    repository = _repository(settings)
    lectures = [
        CatalogLecture(
            item.id,
            item.subject,
            item.exam_number,
            item.lecture_number,
            item.topic,
            item.lecturer,
        )
        for item in repository.list_lectures()
    ]
    matcher = LectureMatcher(lectures)
    chosen = date.fromisoformat(args.date)
    start, end = _window(1, chosen)
    for event in _calendar(settings).list_events(start, end):
        try:
            candidate = matcher.match(parse_lecture_title(event.subject))
            print(
                f"{event.subject} -> lecture_id={candidate.lecture_id} "
                f"confidence={candidate.confidence:.2f} "
                f"review={candidate.needs_review}"
            )
        except ValueError as error:
            print(f"{event.subject} -> review=True reason={error}")
    return 0


def serve(args: argparse.Namespace) -> int:
    settings = Settings()
    app = create_app(settings)
    scheduler = None
    if settings.outlook_client_id:
        repository = CatalogRepository(app.state.database)
        synchronizer = OutlookSynchronizer(
            repository,
            _calendar(settings),
        )

        def sync_once() -> None:
            start, end = _window(settings.outlook_sync_days_ahead)
            synchronizer.sync_window(start, end)

        scheduler = build_scheduler(settings.timezone, sync_once)
        scheduler.start()
    try:
        uvicorn.run(
            app,
            host=settings.dashboard_host,
            port=settings.dashboard_port,
        )
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oms-hub")
    commands = parser.add_subparsers(required=True)

    tracker = commands.add_parser("import-tracker")
    tracker.add_argument("path")
    tracker.set_defaults(handler=import_tracker)

    login = commands.add_parser("outlook-login")
    login.set_defaults(handler=outlook_login)

    sync = commands.add_parser("sync-outlook")
    sync.add_argument("--days", type=int, default=14)
    sync.set_defaults(handler=sync_outlook)

    preview = commands.add_parser("dry-run")
    preview.add_argument("--date", required=True)
    preview.set_defaults(handler=dry_run)

    server = commands.add_parser("serve")
    server.set_defaults(handler=serve)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(args.handler(args))
