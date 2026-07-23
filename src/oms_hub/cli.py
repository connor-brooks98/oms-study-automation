import argparse
import getpass
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
    sync_once = None
    if settings.outlook_client_id:
        repository = CatalogRepository(app.state.database)
        synchronizer = OutlookSynchronizer(
            repository,
            _calendar(settings),
        )

        def sync_once() -> None:
            start, end = _window(settings.outlook_sync_days_ahead)
            synchronizer.sync_window(start, end)

    pipeline = app.state.canvas_pipeline
    pipeline.recover_abandoned_jobs()
    panopto_pipeline = app.state.panopto_pipeline
    panopto_pipeline.recover_abandoned_jobs()

    def panopto_poll_once() -> object:
        return app.state.panopto_discovery.poll(datetime.now(UTC))

    scheduler = build_scheduler(
        settings.timezone,
        sync_once,
        pipeline.run_next,
        panopto_poll_once,
        panopto_pipeline.run_next,
    )
    scheduler.start()
    try:
        uvicorn.run(
            app,
            host=settings.dashboard_host,
            port=settings.dashboard_port,
        )
    finally:
        scheduler.shutdown(wait=False)
    return 0


def canvas_status(args: argparse.Namespace) -> int:
    del args
    app = create_app(Settings())
    connection = app.state.canvas_repository.connection()
    print(
        f"state={connection.state} heartbeat={connection.last_heartbeat or 'never'} "
        f"last_scan={connection.last_successful_scan or 'never'} "
        f"auto_process={connection.auto_process}"
    )
    return 0


def canvas_worker_once(args: argparse.Namespace) -> int:
    del args
    app = create_app(Settings())
    worked = app.state.canvas_pipeline.run_next()
    print("processed=1" if worked else "processed=0")
    return 0


def canvas_recover(args: argparse.Namespace) -> int:
    del args
    app = create_app(Settings())
    result = app.state.canvas_pipeline.recover_abandoned_jobs()
    print(f"requeued={result.requeued} needs_review={result.needs_review}")
    return 0


def panopto_set_secret(args: argparse.Namespace) -> int:
    del args
    value = getpass.getpass("Panopto client secret: ")
    if not value:
        raise SystemExit("Secret cannot be empty")
    secrets = KeyringSecretStore()
    secrets.set("panopto-client-secret", value)
    secrets.delete("panopto-refresh-token")
    secrets.delete("panopto-oauth-state")
    print(
        "Panopto web application client secret stored in Windows Credential "
        "Manager; reconnect Panopto in the dashboard"
    )
    return 0


def openai_set_key(args: argparse.Namespace) -> int:
    del args
    value = getpass.getpass("OpenAI API key: ")
    if not value:
        raise SystemExit("API key cannot be empty")
    KeyringSecretStore().set("openai-api-key", value)
    print("OpenAI API key stored in Windows Credential Manager")
    return 0


def panopto_init_prompt(args: argparse.Namespace) -> int:
    del args
    app = create_app(Settings())
    path = app.state.panopto_prompt.initialize()
    print(f"prompt={path}")
    return 0


def panopto_approve_prompt(args: argparse.Namespace) -> int:
    del args
    app = create_app(Settings())
    prompt = app.state.panopto_prompt.inspect()
    app.state.panopto_repository.approve_prompt(
        prompt.sha256,
        str(app.state.panopto_prompt.path),
    )
    app.state.panopto_prompt.approved_sha256 = prompt.sha256
    print(f"approved_sha256={prompt.sha256}")
    return 0


def panopto_status(args: argparse.Namespace) -> int:
    del args
    app = create_app(Settings())
    connection = app.state.panopto_repository.connection()
    print(
        f"state={connection.state} enabled={connection.enabled} "
        f"connected={app.state.panopto_tokens.connected()} "
        f"acceptance={connection.acceptance_validated_at or 'not-validated'} "
        f"last_poll={connection.last_successful_poll or 'never'}"
    )
    return 0


def panopto_scan_once(args: argparse.Namespace) -> int:
    del args
    app = create_app(Settings())
    settings = app.state.settings
    result = app.state.panopto_discovery.poll(
        datetime.now(UTC),
        manual_session_id=settings.panopto_acceptance_session_id,
    )
    print(
        f"seen={result.seen} matched={result.matched} "
        f"needs_review={result.needs_review}"
    )
    return 0


def panopto_worker_once(args: argparse.Namespace) -> int:
    del args
    app = create_app(Settings())
    worked = app.state.panopto_pipeline.run_next()
    print("processed=1" if worked else "processed=0")
    return 0


def panopto_recover(args: argparse.Namespace) -> int:
    del args
    app = create_app(Settings())
    result = app.state.panopto_pipeline.recover_abandoned_jobs()
    print(
        f"requeued={result.requeued} completed={result.completed} "
        f"needs_review={result.needs_review}"
    )
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

    status = commands.add_parser("canvas-status")
    status.set_defaults(handler=canvas_status)

    worker = commands.add_parser("canvas-worker-once")
    worker.set_defaults(handler=canvas_worker_once)

    recover = commands.add_parser("canvas-recover")
    recover.set_defaults(handler=canvas_recover)

    panopto_secret = commands.add_parser("panopto-set-secret")
    panopto_secret.set_defaults(handler=panopto_set_secret)

    openai_key = commands.add_parser("openai-set-key")
    openai_key.set_defaults(handler=openai_set_key)

    init_prompt = commands.add_parser("panopto-init-prompt")
    init_prompt.set_defaults(handler=panopto_init_prompt)

    approve_prompt = commands.add_parser("panopto-approve-prompt")
    approve_prompt.set_defaults(handler=panopto_approve_prompt)

    panopto_status_command = commands.add_parser("panopto-status")
    panopto_status_command.set_defaults(handler=panopto_status)

    scan_once = commands.add_parser("panopto-scan-once")
    scan_once.set_defaults(handler=panopto_scan_once)

    panopto_worker = commands.add_parser("panopto-worker-once")
    panopto_worker.set_defaults(handler=panopto_worker_once)

    panopto_recovery = commands.add_parser("panopto-recover")
    panopto_recovery.set_defaults(handler=panopto_recover)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(args.handler(args))
