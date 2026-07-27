import argparse
import getpass
import logging
import threading
from pathlib import Path

import uvicorn

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.repositories import CatalogRepository
from oms_hub.routing import expanded_path
from oms_hub.security.secret_store import KeyringSecretStore
from oms_hub.tracker_import import TrackerImporter
from oms_hub.transcripts.prompt import PromptLoader

logger = logging.getLogger(__name__)


def _repository(settings: Settings) -> CatalogRepository:
    database = Database(settings.database_url)
    database.migrate()
    return CatalogRepository(database)


def import_tracker(args: argparse.Namespace) -> int:
    result = TrackerImporter(_repository(Settings())).import_once(Path(args.path))
    print(
        f"imported={result.imported} issues={result.issues} "
        f"sha256={result.source_sha256}"
    )
    return 0


def _run_worker(stop: threading.Event, worker: object) -> None:
    while not stop.is_set():
        try:
            worker.run_once()  # type: ignore[attr-defined]
        except Exception:
            logger.exception("V2 ingestion worker failed")
        stop.wait(5)


def serve(args: argparse.Namespace) -> int:
    del args
    settings = Settings()
    app = create_app(settings)
    app.state.ingestion_worker.recover_interrupted_jobs()
    app.state.generation_worker.recover_interrupted_jobs()
    stop = threading.Event()
    worker_threads = [
        threading.Thread(
            target=_run_worker,
            args=(stop, worker),
            name=name,
            daemon=True,
        )
        for name, worker in (
            ("oms-v2-ingestion", app.state.ingestion_worker),
            ("oms-study-generation", app.state.generation_worker),
        )
    ]
    for worker_thread in worker_threads:
        worker_thread.start()
    try:
        uvicorn.run(
            app,
            host=settings.dashboard_host,
            port=settings.dashboard_port,
        )
    finally:
        stop.set()
        for worker_thread in worker_threads:
            worker_thread.join(timeout=10)
    return 0


def worker_once(args: argparse.Namespace) -> int:
    del args
    app = create_app(Settings())
    worked = app.state.ingestion_worker.run_once()
    worked = app.state.generation_worker.run_once() or worked
    print("processed=1" if worked else "processed=0")
    return 0


def recover_jobs(args: argparse.Namespace) -> int:
    del args
    app = create_app(Settings())
    recovered = app.state.ingestion_worker.recover_interrupted_jobs()
    recovered += app.state.generation_worker.recover_interrupted_jobs()
    print(f"requeued={recovered}")
    return 0


def openai_set_key(args: argparse.Namespace) -> int:
    del args
    value = getpass.getpass("OpenAI API key: ")
    if not value:
        raise SystemExit("API key cannot be empty")
    KeyringSecretStore().set("openai-api-key", value)
    print("OpenAI API key stored in Windows Credential Manager")
    return 0


def prompt_initialize(args: argparse.Namespace) -> int:
    del args
    settings = Settings()
    path = PromptLoader(
        expanded_path(settings.transcript_prompt_path),
        None,
    ).initialize()
    print(f"prompt={path}")
    return 0


def prompt_fingerprint(args: argparse.Namespace) -> int:
    del args
    settings = Settings()
    prompt = PromptLoader(
        expanded_path(settings.transcript_prompt_path),
        None,
    ).inspect()
    print(f"OMS_HUB_TRANSCRIPT_PROMPT_SHA256={prompt.sha256}")
    return 0


def validate_config(args: argparse.Namespace) -> int:
    del args
    settings = Settings()
    required_paths = {
        "study root": expanded_path(settings.study_root),
        "transcript prompt": expanded_path(settings.transcript_prompt_path),
    }
    if settings.icloud_staging_root is None:
        raise SystemExit("OMS_HUB_ICLOUD_STAGING_ROOT is required")
    required_paths["iCloud staging root"] = expanded_path(
        settings.icloud_staging_root
    )
    missing = [name for name, path in required_paths.items() if not path.exists()]
    if missing:
        raise SystemExit("Missing configured path(s): " + ", ".join(missing))
    if settings.dashboard_host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("OMS_HUB_DASHBOARD_HOST must stay on loopback")
    access_values = (
        settings.public_hostname,
        settings.cloudflare_access_issuer,
        settings.cloudflare_access_audience,
        settings.cloudflare_access_allowed_email,
    )
    if not all(access_values):
        raise SystemExit("Cloudflare hostname, issuer, audience, and email are required")
    prompt = PromptLoader(
        expanded_path(settings.transcript_prompt_path),
        settings.transcript_prompt_sha256,
    ).current()
    Database(settings.database_url).migrate()
    print(f"configuration=valid prompt_sha256={prompt.sha256}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oms-hub")
    commands = parser.add_subparsers(required=True)

    tracker = commands.add_parser("import-tracker")
    tracker.add_argument("path")
    tracker.set_defaults(handler=import_tracker)

    server = commands.add_parser("serve")
    server.set_defaults(handler=serve)

    worker = commands.add_parser("worker-once")
    worker.set_defaults(handler=worker_once)

    recover = commands.add_parser("recover-jobs")
    recover.set_defaults(handler=recover_jobs)

    openai_key = commands.add_parser("openai-set-key")
    openai_key.set_defaults(handler=openai_set_key)

    init_prompt = commands.add_parser("prompt-init")
    init_prompt.set_defaults(handler=prompt_initialize)

    fingerprint_prompt = commands.add_parser("prompt-fingerprint")
    fingerprint_prompt.set_defaults(handler=prompt_fingerprint)

    validate = commands.add_parser("validate-config")
    validate.set_defaults(handler=validate_config)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(args.handler(args))
