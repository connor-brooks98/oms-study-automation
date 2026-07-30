import argparse
import asyncio
import getpass
import json
import logging
import threading
import tracemalloc
from dataclasses import asdict
from pathlib import Path

import uvicorn

from oms_hub.anki.ankiconnect import AnkiConnectClient
from oms_hub.anki.index import AnkiIndex
from oms_hub.anki.maintenance import (
    LocalIndexMaintainer,
    LocalIndexRefreshResult,
)
from oms_hub.anki.runtime import AnkiRuntime, WindowsAnkiLauncher
from oms_hub.anki.semantic.service import SemanticIndexService
from oms_hub.anki.semantic.store import SemanticSnapshotStore
from oms_hub.anki.semantic.voyage import VoyageEmbeddingClient
from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.repositories import CatalogRepository
from oms_hub.routing import expanded_path
from oms_hub.security.secret_store import (
    VOYAGE_API_KEY_SECRET,
    KeyringSecretStore,
)
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


def voyage_set_key(args: argparse.Namespace) -> int:
    del args
    value = getpass.getpass("Voyage API key: ")
    if not value:
        raise SystemExit("API key cannot be empty")
    KeyringSecretStore().set(VOYAGE_API_KEY_SECRET, value)
    print("Voyage API key stored in Windows Credential Manager")
    return 0


async def _refresh_local_anki_index(
    settings: Settings,
    query: str,
) -> LocalIndexRefreshResult:
    if not settings.anki_enabled:
        raise RuntimeError(
            "Anki curation is disabled; set OMS_HUB_ANKI_ENABLED=true"
        )
    owns_trace = not tracemalloc.is_tracing()
    if owns_trace:
        tracemalloc.start()
    gateway: AnkiConnectClient | None = None
    runtime: AnkiRuntime | None = None
    embedder: VoyageEmbeddingClient | None = None
    try:
        secrets = KeyringSecretStore()
        gateway = AnkiConnectClient(settings.anki_connect_url)
        runtime = AnkiRuntime(
            gateway,
            WindowsAnkiLauncher(settings.anki_executable_path),
            startup_attempts=settings.anki_startup_attempts,
            startup_poll_seconds=settings.anki_startup_poll_seconds,
        )
        embedder = VoyageEmbeddingClient(
            secrets,
            model=settings.anki_semantic_model,
            dimensions=settings.anki_semantic_dimensions,
            batch_size=settings.anki_semantic_batch_size,
            api_key=settings.voyage_api_key_value,
        )
        root = settings.resolved_anki_data_dir
        companion = AnkiIndex(root / "companion")
        semantic_store = SemanticSnapshotStore(root / "semantic")
        semantic = SemanticIndexService(
            semantic_store,
            embedder,
            model=settings.anki_semantic_model,
            dimensions=settings.anki_semantic_dimensions,
            min_coverage=settings.anki_semantic_min_coverage,
            query_cache_size=settings.anki_semantic_query_cache_size,
        )
        maintainer = LocalIndexMaintainer(
            runtime,
            gateway,
            companion,
            semantic,
            semantic_store,
            semantic_model=settings.anki_semantic_model,
            semantic_dimensions=settings.anki_semantic_dimensions,
            min_coverage=settings.anki_semantic_min_coverage,
            peak_memory_bytes=lambda: tracemalloc.get_traced_memory()[1],
        )
        return await maintainer.refresh(query=query)
    finally:
        if embedder is not None:
            await embedder.aclose()
        if runtime is not None:
            await runtime.aclose()
        elif gateway is not None:
            await gateway.aclose()
        if owns_trace:
            tracemalloc.stop()


def anki_index_refresh(args: argparse.Namespace) -> int:
    query = _anki_index_query(args)
    try:
        result = asyncio.run(
            _refresh_local_anki_index(Settings(), query)
        )
    except RuntimeError as exc:
        raise SystemExit(f"Anki index refresh failed: {exc}") from exc
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


def _anki_index_query(args: argparse.Namespace) -> str:
    deck = getattr(args, "deck", None)
    if deck is not None:
        normalized_deck = str(deck).strip()
        if not normalized_deck:
            raise SystemExit("Anki deck name cannot be empty")
        if '"' in normalized_deck:
            raise SystemExit(
                "Deck names containing double quotes must use --query"
            )
        return f'deck:"{normalized_deck}"'
    query = getattr(args, "query", "")
    return "" if query is None else str(query)


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

    voyage_key = commands.add_parser("voyage-set-key")
    voyage_key.set_defaults(handler=voyage_set_key)

    anki_index = commands.add_parser("anki-index-refresh")
    anki_scope = anki_index.add_mutually_exclusive_group()
    anki_scope.add_argument(
        "--query",
        default=None,
        help=(
            "Optional Anki search query limiting the indexed note universe."
        ),
    )
    anki_scope.add_argument(
        "--deck",
        help=(
            "Deck name to index, including its child decks. Preferred over "
            "--query in Windows PowerShell."
        ),
    )
    anki_index.set_defaults(handler=anki_index_refresh)

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
