import asyncio
import hmac
import json
import logging
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from keyring.errors import KeyringError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from oms_hub import __version__
from oms_hub.anki.ankiconnect import AnkiConnectClient
from oms_hub.anki.apply import ApplyCoordinator, ApplyGateway
from oms_hub.anki.index import AnkiIndex
from oms_hub.anki.pipeline import CurationPipeline, StageArtifactStore
from oms_hub.anki.prompt_catalog import AnkiPromptCatalogService
from oms_hub.anki.prompts import (
    GitPromptSynchronizer,
    PromptSynchronizer,
    StaticPromptSynchronizer,
)
from oms_hub.anki.rehearsal.capture import (
    CaptureAnkiCurationRepository,
    CaptureAuthorization,
    CaptureDenied,
    CaptureEmbeddingClient,
    CaptureSecretStore,
    CaptureStore,
    CaptureStructuredTextGenerator,
    CaptureStructuredTextService,
)
from oms_hub.anki.rehearsal.network import (
    EgressEvidenceLedger,
    EgressPolicy,
    SocketEgressGuard,
)
from oms_hub.anki.rehearsal.runtime import NoopLauncher, ReadOnlyAnkiGateway
from oms_hub.anki.rehearsal.structured import ReplayStructuredTextGenerator
from oms_hub.anki.rehearsal.vectors import ReplayEmbeddingClient
from oms_hub.anki.repository import AnkiCurationRepository
from oms_hub.anki.runtime import AnkiRuntime, WindowsAnkiLauncher
from oms_hub.anki.semantic.domain import EmbeddingClient
from oms_hub.anki.semantic.service import SemanticIndexService
from oms_hub.anki.semantic.store import SemanticSnapshotStore
from oms_hub.anki.semantic.voyage import VoyageEmbeddingClient
from oms_hub.anki.source_index import LectureSourceIndex
from oms_hub.anki.sources import LectureSourceExtractor
from oms_hub.anki.stages import (
    CurationServicesRunner,
    PinnedCurationInputValidator,
)
from oms_hub.anki.tag_policy import TagPolicy
from oms_hub.anki.worker import AnkiCurationWorker
from oms_hub.config import Settings, get_settings
from oms_hub.db import Database
from oms_hub.document_processing.anydoc_adapter import AnydocProcessor
from oms_hub.document_processing.pdf_adapter import PdfProcessor
from oms_hub.document_processing.pptx_locator import PptxLocatorEnricher
from oms_hub.document_processing.router import DocumentProcessorRouter, ParserMode
from oms_hub.document_processing.shadow import DocumentShadowEvaluator, LegacyPptxProcessor
from oms_hub.document_processing.text_adapter import TextProcessor
from oms_hub.document_processing.web_adapter import WebProcessor
from oms_hub.files.office import SerialOfficeConverter
from oms_hub.ingestion.matcher import UploadMatcher
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.ingestion.service import (
    IngestionService as ManualIngestionService,
)
from oms_hub.ingestion.staging import StagingService
from oms_hub.ingestion.worker import IngestionWorker
from oms_hub.llm.anthropic import AnthropicProvider
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.gemini import GeminiProvider
from oms_hub.llm.openai import OpenAIProvider
from oms_hub.llm.openrouter import MedicalAccuracyGate, OpenRouterProvider
from oms_hub.llm.repository import LLMSettingsRepository
from oms_hub.llm.service import LLMService
from oms_hub.llm.structured import StructuredTextGenerator, StructuredTextService
from oms_hub.repositories import CatalogRepository
from oms_hub.routing import expanded_path
from oms_hub.runtime_settings import RuntimeSettingsRepository
from oms_hub.security.access import (
    AccessIdentityForbidden,
    AccessTokenInvalid,
    CloudflareAccessVerifier,
    bearer_token_is_valid,
)
from oms_hub.security.csrf import (
    CsrfProtector,
    browser_csrf_required,
    origin_is_allowed,
)
from oms_hub.security.rate_limit import PublicQuizRateLimiter
from oms_hub.security.secret_store import VOYAGE_API_KEY_SECRET, KeyringSecretStore
from oms_hub.slides.pipeline import SlidePipeline
from oms_hub.study_generation.ai_settings import StudyAISettingsRepository
from oms_hub.study_generation.domain import PromptKind
from oms_hub.study_generation.native_quiz import NativeQuizPublisher
from oms_hub.study_generation.notebook import StoredNotebookLMGateway
from oms_hub.study_generation.notebook_auth import NotebookCLIAuth
from oms_hub.study_generation.notebook_connection import (
    NotebookConnectionService,
    retire_google_docs_credentials,
)
from oms_hub.study_generation.notebook_storage import (
    NotebookStorageError,
    migrate_encrypted_notebook_storage,
)
from oms_hub.study_generation.outline import OutlineService
from oms_hub.study_generation.path_picker import (
    SystemPromptDirectoryPicker,
    SystemPromptPathPicker,
)
from oms_hub.study_generation.practice_answers import PracticeAnswerResolver
from oms_hub.study_generation.practice_extraction import PracticeQuestionExtractor
from oms_hub.study_generation.practice_review import PracticeReviewService
from oms_hub.study_generation.prompts import PromptFileService
from oms_hub.study_generation.quiz_images import StudioQuizImageService
from oms_hub.study_generation.quiz_import_worker import QuizImportWorker
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.study_generation.service import GenerationService
from oms_hub.study_generation.studio_repository import StudioRepository
from oms_hub.study_generation.studio_service import StudioService
from oms_hub.study_generation.studio_worker import StudioWorker
from oms_hub.study_generation.worker import GenerationWorker
from oms_hub.transcripts.pipeline import (
    TranscriptPipeline as V2TranscriptPipeline,
)
from oms_hub.transcripts.prompt import PromptLoader as V2PromptLoader
from oms_hub.web.anki_agent_routes import router as anki_agent_router
from oms_hub.web.anki_routes import router as anki_router
from oms_hub.web.artifact_routes import router as artifact_router
from oms_hub.web.generation_routes import (
    anki_prompt_router,
    lecture_router,
    notebook_router,
)
from oms_hub.web.generation_routes import (
    router as generation_router,
)
from oms_hub.web.public_quiz_routes import router as public_quiz_router
from oms_hub.web.published_quiz_routes import router as published_quiz_router
from oms_hub.web.quarantine_routes import router as quarantine_router
from oms_hub.web.routes import router
from oms_hub.web.settings_routes import api_router as settings_api_router
from oms_hub.web.settings_routes import router as settings_router
from oms_hub.web.studio_routes import router as studio_router
from oms_hub.web.upload_routes import router as upload_router
from oms_hub.workers import SyncWorker

logger = logging.getLogger(__name__)


class _RehearsalSecretStore:
    """A capability-free secret facade for isolated rehearsal processes."""

    def get(self, key: str) -> str | None:
        del key
        return None

    def set(self, key: str, value: str) -> None:
        del key, value
        raise RuntimeError("credential mutation is disabled during rehearsal")

    def delete(self, key: str) -> None:
        del key
        raise RuntimeError("credential mutation is disabled during rehearsal")


def _is_rehearsal_credential_mutation(request: Request) -> bool:
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    path = request.url.path
    return path == "/settings/anki/voyage/credential" or (
        path.startswith("/settings/ai/") and path.endswith("/credential")
    )


def _run_sync_worker(
    stop: threading.Event,
    worker: SyncWorker,
    name: str,
) -> None:
    while not stop.is_set():
        try:
            worked = worker.run_once()
        except Exception:
            logger.exception("%s worker failed", name)
            worked = False
        stop.wait(0.5 if worked else 5.0)


def _capture_dependencies(
    resolved: Settings,
) -> tuple[CaptureAuthorization | None, CaptureStore | None, object | None]:
    """Create capture credentials only after the parent-prepared private root is verified."""
    if resolved.anki_rehearsal_capture_store is None:
        return None, None, None
    if resolved.anki_rehearsal_mode != "shadow":
        raise ValueError("capture dependencies require shadow rehearsal mode")
    assert resolved.anki_rehearsal_capture_authorization_manifest is not None
    assert resolved.anki_rehearsal_capture_authorization_sha256 is not None
    assert resolved.anki_rehearsal_capture_candidate_commit is not None
    assert resolved.anki_rehearsal_capture_candidate_tree is not None
    assert resolved.anki_rehearsal_capture_capsule_manifest_sha256 is not None
    assert resolved.anki_rehearsal_capture_failed_job_id is not None
    authorization = CaptureAuthorization.load(
        resolved.anki_rehearsal_capture_authorization_manifest,
        resolved.anki_rehearsal_capture_authorization_sha256,
        commit=resolved.anki_rehearsal_capture_candidate_commit,
        tree=resolved.anki_rehearsal_capture_candidate_tree,
        capsule_sha256=resolved.anki_rehearsal_capture_capsule_manifest_sha256,
        failed_job_id=resolved.anki_rehearsal_capture_failed_job_id,
    )
    store = CaptureStore(resolved.anki_rehearsal_capture_store, authorization)
    store.verify_prepared()
    secret_keys = {
        "openai": "openai-api-key",
        "gemini": "gemini-api-key",
        "anthropic": "anthropic-api-key",
        "openrouter": "openrouter-api-key",
    }
    allowed = frozenset(
        {VOYAGE_API_KEY_SECRET}
        | {secret_keys[row["provider"]] for row in authorization.document["structured"]}
    )
    return authorization, store, CaptureSecretStore(KeyringSecretStore(), allowed)


def _provider_clients(
    resolved: Settings, capture_store: CaptureStore | None
) -> tuple[dict[ProviderName, Any], tuple[httpx.Client, ...]]:
    if capture_store is None:
        return {
            ProviderName.OPENAI: OpenAIProvider(
                input_usd_per_million=resolved.openai_input_usd_per_million,
                output_usd_per_million=resolved.openai_output_usd_per_million,
            ),
            ProviderName.GEMINI: GeminiProvider(),
            ProviderName.ANTHROPIC: AnthropicProvider(),
            ProviderName.OPENROUTER: OpenRouterProvider(),
        }, ()
    if resolved.anki_rehearsal_mode != "shadow":
        raise ValueError("capture providers require shadow rehearsal mode")
    clients = tuple(httpx.Client(timeout=300.0, trust_env=False) for _ in range(4))
    return {
        ProviderName.OPENAI: OpenAIProvider(
            input_usd_per_million=resolved.openai_input_usd_per_million,
            output_usd_per_million=resolved.openai_output_usd_per_million,
            http=clients[0],
        ),
        ProviderName.GEMINI: GeminiProvider(http=clients[1]),
        ProviderName.ANTHROPIC: AnthropicProvider(http=clients[2]),
        ProviderName.OPENROUTER: OpenRouterProvider(http=clients[3]),
    }, clients


def _anki_curation_repository(
    database: Database, capture_store: CaptureStore | None
) -> AnkiCurationRepository:
    return (
        CaptureAnkiCurationRepository(database)
        if capture_store is not None
        else AnkiCurationRepository(
            database,
            supported_envelope_versions=frozenset({1, 2}),
        )
    )


def _stage_attempt_limit(resolved: Settings, capture_store: CaptureStore | None) -> int:
    """Capture never retries a stage after its one authorized live dispatch path."""
    return 1 if capture_store is not None else resolved.anki_worker_max_stage_attempts


_CAPTURE_CAPABILITY_HEADER = "x-oms-capture-capability"


class _BufferedResponse(Protocol):
    status_code: int
    body_iterator: AsyncIterator[bytes]


def _install_capture_control_plane(
    app: FastAPI, store: CaptureStore, capability: str
) -> None:
    """Install the capture-only, capability-gated ASGI boundary last/outermost.

    It buffers only the tiny JSON responses permitted during capture so a
    successful job id can be durably audited before any response reaches the
    loopback client.  No ordinary app receives this middleware.
    """
    store.initialize_server_audit(capability)
    lock = threading.RLock()
    post_claimed = False
    created_job_id: str | None = None
    capture_v3 = False
    state = {"poisoned": False}
    app.state.anki_capture_control_state = state

    def poison() -> None:
        state["poisoned"] = True
        try:
            store.poison_server_audit()
        except CaptureDenied:
            # The in-memory poison keeps this child closed; parent completion
            # also fails because no complete audit can cover its transcript.
            return

    def raw_path(request: Request) -> str:
        value = request.scope.get("raw_path", b"")
        if not isinstance(value, bytes):
            return "<invalid-raw-path>"
        try:
            decoded = value.decode("ascii")
        except UnicodeDecodeError:
            return "<invalid-raw-path>"
        return decoded if decoded.startswith("/") else "<invalid-raw-path>"

    def query_state(request: Request) -> str:
        if "query_string" not in request.scope:
            return "<invalid-query-string>"
        value = request.scope["query_string"]
        if not isinstance(value, bytes):
            return "<invalid-query-string>"
        if not value:
            return "empty"
        try:
            value.decode("ascii")
        except UnicodeDecodeError:
            return "<invalid-query-string>"
        return "present"

    def deny(
        *,
        method: str,
        raw: str,
        canonical: str,
        authenticated: bool,
        status: int,
        query: str,
        job_id: str | None = None,
    ) -> Response:
        try:
            store.record_server_request(
                method=method,
                raw_path=raw,
                canonical_path=canonical,
                authenticated=authenticated,
                allowed=False,
                status=status,
                job_id=job_id,
                query_state=query,
            )
        except CaptureDenied:
            poison()
            return JSONResponse({"detail": "capture audit unavailable"}, status_code=500)
        return JSONResponse({"detail": "capture route is unavailable"}, status_code=status)

    @app.middleware("http")
    async def capture_control_plane(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        nonlocal post_claimed, created_job_id, capture_v3
        if state["poisoned"]:
            return JSONResponse({"detail": "capture audit unavailable"}, status_code=500)
        method = request.method.upper()
        raw = raw_path(request)
        query = query_state(request)
        canonical = request.scope.get("path")
        if not isinstance(canonical, str) or not canonical.startswith("/"):
            canonical = "<invalid-canonical-path>"
        supplied = request.headers.get(_CAPTURE_CAPABILITY_HEADER)
        authenticated = isinstance(supplied, str) and hmac.compare_digest(supplied, capability)
        if not authenticated:
            return deny(
                method=method,
                raw=raw,
                canonical=canonical,
                authenticated=False,
                status=401,
                query=query,
            )
        status_job_id: str | None = None
        is_create = query == "empty" and method == "POST" and raw == canonical == "/api/anki/jobs"
        is_health = query == "empty" and method == "GET" and raw == canonical == "/health"
        is_status = (
            query == "empty"
            and method == "GET"
            and raw == canonical
            and raw.startswith("/api/anki/jobs/")
        )
        if is_status:
            suffix = raw.removeprefix("/api/anki/jobs/")
            try:
                status_job_id = str(UUID(suffix))
            except ValueError:
                is_status = False
            else:
                is_status = suffix == status_job_id
        review_path = (
            None if created_job_id is None else f"/api/anki/jobs/{created_job_id}/review"
        )
        apply_path = (
            None if created_job_id is None else f"/api/anki/jobs/{created_job_id}/apply"
        )
        is_v3_review = (
            capture_v3
            and query == "empty"
            and method == "GET"
            and raw == canonical == review_path
        )
        is_v3_apply = (
            capture_v3
            and query == "empty"
            and method == "POST"
            and raw == canonical == apply_path
        )
        if is_v3_review or is_v3_apply:
            status_job_id = created_job_id
        with lock:
            if is_create:
                if post_claimed:
                    return deny(
                        method=method,
                        raw=raw,
                        canonical=canonical,
                        authenticated=True,
                        status=409,
                        query=query,
                    )
                post_claimed = True
            elif is_status and status_job_id != created_job_id:
                return deny(
                    method=method,
                    raw=raw,
                    canonical=canonical,
                    authenticated=True,
                    status=404,
                    query=query,
                    job_id=status_job_id,
                )
            elif not (is_health or is_status or is_v3_review or is_v3_apply):
                return deny(
                    method=method,
                    raw=raw,
                    canonical=canonical,
                    authenticated=True,
                    status=404,
                    query=query,
                )
        response = await call_next(request)
        buffered_response = cast(_BufferedResponse, response)
        body = b""
        try:
            body = b"".join([chunk async for chunk in buffered_response.body_iterator])
            if is_create and buffered_response.status_code == 201:
                value = json.loads(body)
                candidate = value.get("id") if isinstance(value, dict) else None
                created = str(UUID(candidate)) if isinstance(candidate, str) else None
                if created is None or candidate != created:
                    raise ValueError("capture job response lacks a canonical id")
                with lock:
                    if created_job_id is not None:
                        raise ValueError("capture created job identity is already bound")
                    created_job_id = created
                    repository = getattr(app.state, "anki_repository", None)
                    capture_v3 = bool(
                        repository is not None
                        and repository.require_job(UUID(created)).pipeline_contract_version.value
                        == "card_centric_v3"
                    )
                status_job_id = created
            if is_status and status_job_id is None:
                raise ValueError("capture status request identity is unavailable")
            store.record_server_request(
                method=method,
                raw_path=raw,
                canonical_path=canonical,
                authenticated=True,
                allowed=True,
                status=buffered_response.status_code,
                job_id=status_job_id,
                query_state=query,
            )
        except (CaptureDenied, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            poison()
            return JSONResponse({"detail": "capture audit unavailable"}, status_code=500)

        async def replay_body() -> AsyncIterator[bytes]:
            yield body

        buffered_response.body_iterator = replay_body()
        return response


@asynccontextmanager
async def _app_lifespan(app: FastAPI) -> AsyncIterator[None]:
    egress_guard = getattr(app.state, "anki_rehearsal_egress_guard", None)
    if egress_guard is not None:
        egress_guard.install()
    anki_worker = getattr(app.state, "anki_curation_worker", None)
    if anki_worker is not None:
        await anki_worker.start()
    sync_workers = tuple(
        (name, getattr(app.state, name, None))
        for name in (
            "ingestion_worker",
            "generation_worker",
            "studio_worker",
        )
    )
    active_sync_workers = (
        ()
        if getattr(app.state, "anki_rehearsal_mode", "off") != "off"
        else tuple((name, worker) for name, worker in sync_workers if worker is not None)
    )
    for _name, sync_worker in active_sync_workers:
        recover = getattr(sync_worker, "recover_interrupted_jobs", None)
        if callable(recover):
            recover()
    stop = threading.Event()
    threads = tuple(
        threading.Thread(
            target=_run_sync_worker,
            args=(stop, sync_worker, name),
            name=f"oms-{name}",
            daemon=True,
        )
        for name, sync_worker in active_sync_workers
    )
    for thread in threads:
        thread.start()
    app.state.worker_threads = threads
    try:
        yield
    finally:
        stop.set()
        try:
            await asyncio.gather(*(asyncio.to_thread(thread.join, 10) for thread in threads))
        finally:
            try:
                if anki_worker is not None:
                    await anki_worker.stop()
            finally:
                try:
                    embedder = getattr(app.state, "anki_embedder", None)
                    if embedder is not None:
                        await embedder.aclose()
                finally:
                    try:
                        runtime = getattr(app.state, "anki_runtime", None)
                        if runtime is not None:
                            await runtime.aclose()
                    finally:
                        try:
                            for client in getattr(app.state, "anki_capture_http_clients", ()):
                                client.close()
                        finally:
                            try:
                                capture_control = getattr(
                                    app.state, "anki_capture_control_state", None
                                )
                                capture_store = getattr(app.state, "anki_capture_store", None)
                                if (
                                    isinstance(capture_control, dict)
                                    and capture_control.get("poisoned") is True
                                    and isinstance(capture_store, CaptureStore)
                                ):
                                    capture_store.poison_server_audit()
                            finally:
                                try:
                                    database = getattr(app.state, "database", None)
                                    if database is not None:
                                        database.close()
                                finally:
                                    if egress_guard is not None:
                                        egress_guard.uninstall()


def create_app(settings: Settings | None = None) -> FastAPI:
    # Bootstrap from the immutable deployment configuration exactly once.  The
    # runtime repository may then apply its single allowlisted, staged value
    # before middleware and Anki clients observe Settings.
    base_settings = settings or get_settings()
    base_settings.data_dir.mkdir(parents=True, exist_ok=True)
    database = Database(base_settings.database_url)
    database.migrate()
    runtime_settings = RuntimeSettingsRepository(database, base_settings)
    resolved = runtime_settings.effective_settings()
    app = FastAPI(
        title="OMS II Study Automation Hub",
        version=__version__,
        lifespan=_app_lifespan,
    )
    allowed_hosts = ["127.0.0.1", "localhost", "testserver"]
    if resolved.public_hostname:
        allowed_hosts.append(resolved.public_hostname)
    if resolved.anki_agent_hostname:
        allowed_hosts.append(resolved.anki_agent_hostname)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=allowed_hosts,
    )
    csrf = CsrfProtector.from_data_dir(resolved.data_dir)
    app.state.csrf = csrf
    app.state.public_quiz_rate_limiter = PublicQuizRateLimiter()
    access_values = (
        resolved.cloudflare_access_issuer,
        resolved.cloudflare_access_audience,
        resolved.cloudflare_access_allowed_email,
    )
    if all(access_values):
        assert resolved.cloudflare_access_issuer is not None
        assert resolved.cloudflare_access_audience is not None
        assert resolved.cloudflare_access_allowed_email is not None
        app.state.access_verifier = CloudflareAccessVerifier(
            issuer=resolved.cloudflare_access_issuer,
            audience=resolved.cloudflare_access_audience,
            allowed_email=resolved.cloudflare_access_allowed_email,
        )
    else:
        app.state.access_verifier = None

    @app.middleware("http")
    async def secure_dashboard_requests(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        host = (request.url.hostname or "").lower().rstrip(".")
        local_hosts = {"127.0.0.1", "localhost", "testserver"}
        is_public = bool(resolved.public_hostname and host == resolved.public_hostname)
        is_agent_host = bool(resolved.anki_agent_hostname and host == resolved.anki_agent_hostname)
        is_agent_path = request.url.path.startswith("/agent/v1/")
        is_public_quiz = request.url.path in {
            "/public/quizzes",
            "/public/practice-questions",
        } or request.url.path.startswith("/public/quizzes/")

        def harden(response: Response) -> Response:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "base-uri 'none'; "
                "connect-src 'self'; "
                "font-src 'self' https://fonts.gstatic.com; "
                "form-action 'self'; "
                "frame-ancestors 'none'; "
                "img-src 'self' data:; "
                "object-src 'none'; "
                "script-src 'self'; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com"
            )
            response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            content_type = response.headers.get("content-type", "")
            if (
                response.status_code in {401, 403, 503}
                or "text/html" in content_type
                or request.url.path.startswith("/artifacts/")
                or is_public_quiz
            ):
                response.headers.setdefault("Cache-Control", "no-store")
            if is_public:
                response.headers["Strict-Transport-Security"] = (
                    "max-age=31536000; includeSubDomains"
                )
            return response

        if (
            resolved.anki_rehearsal_mode != "off"
            and _is_rehearsal_credential_mutation(request)
        ):
            return harden(
                JSONResponse(
                    {"detail": "credential mutation is disabled during rehearsal"},
                    status_code=423,
                )
            )

        if is_agent_path and not is_agent_host:
            return harden(JSONResponse({"detail": "Not Found"}, status_code=404))
        if is_agent_host:
            if not is_agent_path:
                return harden(JSONResponse({"detail": "Not Found"}, status_code=404))
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                content_length = request.headers.get("content-length")
                if content_length is None:
                    return harden(
                        JSONResponse(
                            {"detail": "Content-Length is required"},
                            status_code=411,
                        )
                    )
                try:
                    request_bytes = int(content_length)
                except ValueError:
                    return harden(
                        JSONResponse(
                            {"detail": "Content-Length is invalid"},
                            status_code=400,
                        )
                    )
                if request_bytes < 0 or request_bytes > resolved.anki_agent_max_request_bytes:
                    return harden(
                        JSONResponse(
                            {"detail": "agent request is too large"},
                            status_code=413,
                        )
                    )
            try:
                expected_token = request.app.state.secrets.get(resolved.anki_agent_token_key)
            except KeyringError:
                return harden(
                    JSONResponse(
                        {"detail": "agent credential store is unavailable"},
                        status_code=503,
                    )
                )
            if not bearer_token_is_valid(
                request.headers.get("authorization"),
                expected_token,
            ):
                return harden(
                    JSONResponse(
                        {"detail": "agent authentication is required"},
                        status_code=401,
                    )
                )
            agent_id = request.headers.get("x-oms-agent-id", "").strip()
            if not agent_id or len(agent_id) > 100:
                return harden(
                    JSONResponse(
                        {"detail": "agent identity is required"},
                        status_code=400,
                    )
                )
            request.state.agent_id = agent_id
            return harden(await call_next(request))
        if is_public:
            if not is_public_quiz:
                verifier = request.app.state.access_verifier
                if verifier is None:
                    return harden(
                        JSONResponse(
                            {"detail": "Cloudflare Access is not configured"},
                            status_code=503,
                        )
                    )
                assertion = request.headers.get("Cf-Access-Jwt-Assertion")
                if not assertion:
                    return harden(
                        JSONResponse(
                            {"detail": "Cloudflare Access identity is required"},
                            status_code=401,
                        )
                    )
                try:
                    request.state.access_identity = verifier.verify(assertion)
                except AccessIdentityForbidden:
                    return harden(
                        JSONResponse(
                            {"detail": "Cloudflare Access identity is not allowed"},
                            status_code=403,
                        )
                    )
                except AccessTokenInvalid:
                    return harden(
                        JSONResponse(
                            {"detail": "Cloudflare Access identity is invalid"},
                            status_code=401,
                        )
                    )
        elif host in local_hosts:
            if not resolved.allow_local_access:
                return harden(JSONResponse({"detail": "Local access is disabled"}, status_code=403))
        else:
            return harden(JSONResponse({"detail": "Host is not allowed"}, status_code=400))

        origin = request.headers.get("origin")
        allowed_origins = {
            f"http://127.0.0.1:{resolved.dashboard_port}",
            f"http://localhost:{resolved.dashboard_port}",
            "http://testserver",
        }
        if resolved.public_hostname:
            allowed_origins.add(f"https://{resolved.public_hostname}")
        allowed_origin = origin_is_allowed(origin, allowed_origins)
        same_origin_fetch = request.headers.get("sec-fetch-site", "").casefold() == "same-origin"
        is_mutation = browser_csrf_required(request.method, request.url.path)
        if is_mutation:
            if origin and not (allowed_origin or same_origin_fetch):
                return harden(
                    JSONResponse(
                        {"detail": "cross-site request rejected"},
                        status_code=403,
                    )
                )
            content_type = request.headers.get("content-type", "")
            trusted_native_form = bool(
                (allowed_origin or same_origin_fetch)
                and content_type.startswith(
                    (
                        "application/x-www-form-urlencoded",
                        "multipart/form-data",
                    )
                )
            )
            valid_token = csrf.verify(
                request.cookies.get(csrf.cookie_name),
                request.headers.get(csrf.header_name),
            )
            if is_public and not (valid_token or trusted_native_form):
                return harden(
                    JSONResponse(
                        {"detail": "request verification failed"},
                        status_code=403,
                    )
                )

        csrf_token = request.cookies.get(csrf.cookie_name)
        if not csrf.verify(csrf_token, csrf_token):
            csrf_token = csrf.issue()
        assert csrf_token is not None
        request.state.csrf_token = csrf_token
        response = await call_next(request)
        if request.cookies.get(csrf.cookie_name) != csrf_token:
            response.set_cookie(
                csrf.cookie_name,
                csrf_token,
                secure=is_public,
                httponly=False,
                samesite="strict",
                path="/",
            )
        return harden(response)

    app.state.base_settings = base_settings
    app.state.settings = resolved
    app.state.anki_rehearsal_mode = resolved.anki_rehearsal_mode
    app.state.anki_rehearsal_evidence_directory = None
    app.state.anki_rehearsal_run_nonce = None
    egress_evidence: EgressEvidenceLedger | None = None
    if resolved.anki_rehearsal_mode != "off":
        assert resolved.anki_rehearsal_overlay_dir is not None
        run_nonce = os.environ.get("OMS_HUB_ANKI_REHEARSAL_RUN_NONCE")
        if not run_nonce:
            raise ValueError("rehearsal runtime evidence requires a run nonce")
        evidence_directory = (
            resolved.anki_rehearsal_overlay_dir / "rehearsal" / "runtime-evidence"
        )
        app.state.anki_rehearsal_evidence_directory = evidence_directory
        app.state.anki_rehearsal_run_nonce = run_nonce
        egress_evidence = EgressEvidenceLedger(
            evidence_directory,
            mode=resolved.anki_rehearsal_mode,
            run_nonce=run_nonce,
        )
    if resolved.anki_rehearsal_mode == "deterministic":
        app.state.anki_rehearsal_egress_guard = SocketEgressGuard(
            EgressPolicy.deterministic(egress_evidence)
        )
    elif resolved.anki_rehearsal_mode == "shadow":
        raw_pins = json.loads(resolved.anki_rehearsal_egress_pins_json or "{}")
        if not isinstance(raw_pins, dict) or any(
            not isinstance(host, str)
            or not isinstance(addresses, list)
            or any(not isinstance(address, str) for address in addresses)
            for host, addresses in raw_pins.items()
        ):
            raise ValueError("shadow egress pins must be a host-to-address-list object")
        app.state.anki_rehearsal_egress_guard = SocketEgressGuard(
            EgressPolicy.shadow(
                {host: set(addresses) for host, addresses in raw_pins.items()},
                egress_evidence,
            )
        )
    else:
        app.state.anki_rehearsal_egress_guard = None
    app.state.database = database
    app.state.runtime_settings = runtime_settings
    app.state.anki_runtime = (
        AnkiRuntime(
            AnkiConnectClient(resolved.anki_connect_url),
            WindowsAnkiLauncher(resolved.anki_executable_path),
            startup_attempts=resolved.anki_startup_attempts,
            startup_poll_seconds=resolved.anki_startup_poll_seconds,
        )
        if resolved.anki_enabled and resolved.anki_rehearsal_mode == "off"
        else None
    )
    capture_authorization, capture_store, capture_secrets = _capture_dependencies(resolved)
    if capture_secrets is not None:
        app.state.secrets = capture_secrets
    else:
        app.state.secrets = (
            _RehearsalSecretStore()
            if resolved.anki_rehearsal_mode != "off"
            else KeyringSecretStore()
        )
    app.state.notebook_storage_migration_error = None
    if resolved.anki_rehearsal_mode == "off":
        try:
            retire_google_docs_credentials(resolved.data_dir, app.state.secrets)
        except KeyringError:
            logger.warning(
                "Legacy Google Docs credentials could not be retired; "
                "continuing startup."
            )
    app.state.study_ai_settings = StudyAISettingsRepository(database)
    app.state.anki_repository = _anki_curation_repository(database, capture_store)
    app.state.anki_tag_policy = TagPolicy(
        pipeline_owned_roots=("OMS",),
        approved_optional_roots=("AnkiHub_Optional::LMU_OMS_II",),
        source_managed_roots=(
            "#AK_Step",
            "#Pathoma",
            "#Sketchy",
            "#FirstAid",
            "#BoardsAndBeyond",
            "#OME",
            "#UWorld",
            "AnkiHub_",
        ),
        version="tags-v1",
    )
    app.state.anki_apply_coordinator = None
    app.state.llm_settings = LLMSettingsRepository(
        database,
        default_openai_model=resolved.openai_model,
    )
    provider_clients, capture_http_clients = _provider_clients(resolved, capture_store)
    app.state.anki_capture_http_clients = capture_http_clients
    app.state.llm_service = LLMService(
        app.state.llm_settings,
        app.state.secrets,
        provider_clients,
    )
    app.state.anki_capture_authorization = capture_authorization
    app.state.anki_capture_store = capture_store
    app.state.medical_accuracy_gate = MedicalAccuracyGate(
        app.state.study_ai_settings,
        app.state.llm_service,
    )
    app.state.catalog_repository = CatalogRepository(database)
    app.state.ingestion_repository = IngestionRepository(
        database,
        artifact_v2_root=expanded_path(resolved.data_dir) / "artifacts" / "v2",
        study_root=expanded_path(resolved.study_root),
        icloud_root=(
            expanded_path(resolved.icloud_staging_root)
            if resolved.icloud_staging_root is not None
            else None
        ),
    )
    app.state.generation_repository = GenerationRepository(
        database,
        app.state.medical_accuracy_gate,
    )
    prompt_fallback = resolved.anki_prompt_directory

    def active_anki_prompt_directory() -> Path | None:
        saved = app.state.generation_repository.anki_prompt_directory()
        return Path(saved) if saved else prompt_fallback

    app.state.anki_prompt_catalog = AnkiPromptCatalogService(
        active_anki_prompt_directory,
    )
    notebook_storage_path = resolved.data_dir / "google" / "notebooklm-storage.json"
    try:
        app.state.notebook_storage_migrated = (
            False
            if resolved.anki_rehearsal_mode != "off"
            else migrate_encrypted_notebook_storage(
                notebook_storage_path.with_suffix(".enc"),
                notebook_storage_path,
                app.state.secrets,
            )
        )
    except NotebookStorageError as error:
        # NotebookLM is optional. Keep the encrypted rollback artifact and let
        # the Hub start disconnected so Settings remains available to reconnect.
        app.state.notebook_storage_migrated = False
        app.state.notebook_storage_migration_error = str(error)
    app.state.notebook_auth = NotebookCLIAuth(notebook_storage_path)
    app.state.notebook_connection = NotebookConnectionService(
        app.state.generation_repository,
        app.state.notebook_auth,
    )
    if app.state.notebook_storage_migration_error is not None:
        app.state.notebook_connection.invalidate(
            app.state.notebook_storage_migration_error
        )
    app.state.prompt_path_picker = SystemPromptPathPicker()
    app.state.prompt_directory_picker = SystemPromptDirectoryPicker()
    app.state.upload_staging = StagingService(
        resolved.data_dir / "staging",
        resolved.max_upload_file_bytes,
        resolved.max_upload_batch_bytes,
        resolved.upload_session_hours,
    )
    app.state.ingestion_service = ManualIngestionService(
        app.state.ingestion_repository,
        app.state.catalog_repository,
        UploadMatcher(),
        app.state.upload_staging,
    )
    app.state.slide_pipeline = SlidePipeline(
        database,
        resolved,
        SerialOfficeConverter(resolved.office_timeout_seconds),
        DocumentShadowEvaluator(
            AnydocProcessor(PptxLocatorEnricher()),
            LegacyPptxProcessor(),
        ),
    )
    saved_transcript_prompt = app.state.generation_repository.prompt_path(PromptKind.TRANSCRIPT)
    transcript_prompt_path = (
        Path(saved_transcript_prompt)
        if saved_transcript_prompt
        else resolved.transcript_prompt_path
    )
    app.state.transcript_prompt = V2PromptLoader(
        (expanded_path(transcript_prompt_path) if transcript_prompt_path is not None else None),
        resolved.transcript_prompt_sha256,
    )
    app.state.transcript_pipeline = V2TranscriptPipeline(
        database,
        resolved,
        app.state.transcript_prompt,
        app.state.llm_service,
    )
    app.state.ingestion_worker = IngestionWorker(
        app.state.ingestion_repository,
        app.state.slide_pipeline,
        app.state.transcript_pipeline,
    )
    prompt_files = PromptFileService(app.state.generation_repository)
    app.state.generation_service = GenerationService(
        app.state.catalog_repository,
        app.state.ingestion_repository,
        app.state.generation_repository,
        prompt_files,
        app.state.notebook_connection,
    )
    notebook_gateway = StoredNotebookLMGateway(
        notebook_storage_path,
        app.state.generation_repository,
    )
    app.state.notebook_gateway = notebook_gateway
    app.state.generation_worker = GenerationWorker(
        app.state.generation_repository,
        app.state.catalog_repository,
        app.state.ingestion_repository,
        prompt_files,
        notebook_gateway,
        OutlineService(resolved, app.state.generation_repository),
        NativeQuizPublisher(
            app.state.generation_repository,
            resolved,
        ),
        app.state.notebook_connection,
    )
    app.state.studio_repository = StudioRepository(database)
    app.state.practice_review = PracticeReviewService(app.state.studio_repository)
    app.state.generation_repository.practice_review = app.state.practice_review
    app.state.studio_service = StudioService(
        app.state.studio_repository,
        resolved.data_dir / "studio-sources",
        resolved.max_upload_file_bytes,
    )
    app.state.studio_quiz_image_service = StudioQuizImageService(
        app.state.studio_repository,
        resolved.data_dir / "studio-quiz-media",
    )
    app.state.practice_review.set_image_service(app.state.studio_quiz_image_service)
    # Direct import deliberately parses immutable local snapshots.  The URL
    # snapshotter is shared with StudioService only for safe web-image assets;
    # acquisition itself has already completed before a run is queued.
    app.state.document_processor_router = DocumentProcessorRouter(
        primary=AnydocProcessor(PptxLocatorEnricher()),
        fallbacks=(
            PdfProcessor(),
            WebProcessor(app.state.studio_service.url_snapshot_service),
            TextProcessor(),
        ),
        mode=ParserMode.ANYDOC,
    )
    app.state.quiz_import_worker = QuizImportWorker(
        app.state.studio_repository,
        app.state.document_processor_router,
        PracticeQuestionExtractor(app.state.llm_service),
        PracticeAnswerResolver(notebook_gateway, app.state.llm_service),
        notebook_gateway,
        resolved.data_dir / "studio-import-assets",
    )
    app.state.studio_worker = StudioWorker(
        app.state.studio_repository,
        notebook_gateway,
        SerialOfficeConverter(resolved.office_timeout_seconds),
        app.state.notebook_connection,
        app.state.generation_repository,
        app.state.studio_quiz_image_service,
        app.state.quiz_import_worker,
    )
    app.state.anki_curation_worker = None
    app.state.anki_embedder = None
    app.state.anki_companion_index = None
    app.state.anki_semantic_store = None
    app.state.anki_source_index = None
    if resolved.anki_enabled:
        anki_root = resolved.resolved_anki_data_dir
        companion = AnkiIndex(anki_root / "companion")
        embedder: EmbeddingClient
        replay_root = resolved.anki_rehearsal_replay_dir
        if resolved.anki_rehearsal_mode == "off":
            runtime = app.state.anki_runtime
            assert isinstance(runtime, AnkiRuntime)
            embedder = VoyageEmbeddingClient(
                app.state.secrets,
                model=resolved.anki_semantic_model,
                dimensions=resolved.anki_semantic_dimensions,
                batch_size=resolved.anki_semantic_batch_size,
                api_key=resolved.voyage_api_key_value,
            )
        elif capture_store is not None:
            runtime = AnkiRuntime(
                ReadOnlyAnkiGateway(
                    companion,
                    evidence_directory=app.state.anki_rehearsal_evidence_directory,
                    run_nonce=app.state.anki_rehearsal_run_nonce,
                ),
                NoopLauncher(),
                startup_attempts=1,
                startup_poll_seconds=0.01,
            )
            app.state.anki_runtime = runtime
            live_voyage = VoyageEmbeddingClient(
                app.state.secrets,
                model=resolved.anki_semantic_model,
                dimensions=resolved.anki_semantic_dimensions,
                batch_size=1_000,
                max_attempts=1,
                split_on_limit=False,
            )
            embedder = CaptureEmbeddingClient(live_voyage, capture_store)
        else:
            assert replay_root is not None
            runtime = AnkiRuntime(
                ReadOnlyAnkiGateway(
                    companion,
                    evidence_directory=app.state.anki_rehearsal_evidence_directory,
                    run_nonce=app.state.anki_rehearsal_run_nonce,
                ),
                NoopLauncher(),
                startup_attempts=1,
                startup_poll_seconds=0.01,
            )
            app.state.anki_runtime = runtime
            embedder = ReplayEmbeddingClient(
                replay_root / "vectors",
                model=resolved.anki_semantic_model,
                dimensions=resolved.anki_semantic_dimensions,
            )
        semantic_store = SemanticSnapshotStore(anki_root / "semantic")
        semantic = SemanticIndexService(
            semantic_store,
            embedder,
            model=resolved.anki_semantic_model,
            dimensions=resolved.anki_semantic_dimensions,
            min_coverage=resolved.anki_semantic_min_coverage,
            query_cache_size=resolved.anki_semantic_query_cache_size,
        )

        def source_index(job_id: object) -> LectureSourceIndex:
            from uuid import UUID

            if not isinstance(job_id, UUID):
                raise TypeError("job ID is invalid")
            return LectureSourceIndex(
                anki_root / "jobs" / str(job_id) / "source-index",
                embedder,
                model=resolved.anki_semantic_model,
                dimensions=resolved.anki_semantic_dimensions,
                query_cache_size=resolved.anki_semantic_query_cache_size,
            )

        structured_generator: StructuredTextGenerator
        if capture_store is not None:
            capture_endpoints = {
                ProviderName.OPENAI: OpenAIProvider.url,
                ProviderName.GEMINI: GeminiProvider.base_url,
                ProviderName.ANTHROPIC: AnthropicProvider.url,
                ProviderName.OPENROUTER: OpenRouterProvider.chat_url,
            }
            structured_generator = CaptureStructuredTextGenerator(
                app.state.llm_service,
                capture_store,
                capture_endpoints,
            )
        elif resolved.anki_rehearsal_mode == "deterministic":
            assert replay_root is not None
            structured_generator = ReplayStructuredTextGenerator(
                replay_root / "structured.json", require_attempt_identity=True
            )
        else:
            structured_generator = app.state.llm_service
        structured = (
            CaptureStructuredTextService(
                structured_generator, capture_authorization, capture_endpoints
            )
            if capture_store is not None and capture_authorization is not None
            else StructuredTextService(structured_generator)
        )
        from oms_hub.anki.card_centric_fixture_service import ProductionFixtureClassifier

        app.state.card_centric_fixture_classifier = ProductionFixtureClassifier(structured)
        prompt_sync: PromptSynchronizer
        if resolved.anki_prompt_git_sync:
            if resolved.anki_prompt_directory is None:
                raise ValueError("Anki prompt Git sync requires OMS_HUB_ANKI_PROMPT_DIRECTORY")
            prompt_sync = GitPromptSynchronizer(
                resolved.anki_prompt_directory,
                timeout_seconds=resolved.anki_prompt_git_timeout_seconds,
            )
        else:
            prompt_sync = StaticPromptSynchronizer()
        prompt_catalog = app.state.anki_prompt_catalog
        runner = CurationServicesRunner(
            runtime=runtime,
            repository=app.state.anki_repository,
            source_extractor=LectureSourceExtractor(
                app.state.ingestion_repository,
                outlines=app.state.generation_repository,
            ),
            source_indexes=source_index,
            companion=companion,
            semantic=semantic,
            structured=structured,
            embedder=embedder,
            focused_retrieval_limit=(resolved.anki_focused_retrieval_limit),
            global_retrieval_limit=resolved.anki_global_retrieval_limit,
            llm_settings=app.state.llm_settings,
            prompts=prompt_catalog,
            prompt_sync=prompt_sync,
        )
        validator = PinnedCurationInputValidator(
            app.state.anki_repository,
            app.state.ingestion_repository,
            companion,
            semantic_store,
            source_index,
            outlines=app.state.generation_repository,
            semantic_model=resolved.anki_semantic_model,
            semantic_dimensions=resolved.anki_semantic_dimensions,
        )
        pipeline = CurationPipeline(
            app.state.anki_repository,
            StageArtifactStore(anki_root / "artifacts"),
            runner,
            input_validator=validator,
            provider_mode=(
                "shadow" if resolved.anki_rehearsal_mode == "shadow" else "canonical"
            ),
        )
        app.state.anki_embedder = embedder
        app.state.anki_companion_index = companion
        app.state.anki_semantic_store = semantic_store
        app.state.anki_source_index = source_index
        app.state.anki_prompt_catalog = prompt_catalog
        app.state.anki_curation_pipeline = pipeline
        if resolved.anki_rehearsal_mode == "off":
            app.state.anki_apply_coordinator = ApplyCoordinator(
                app.state.anki_repository,
                cast(ApplyGateway, runtime.gateway),
                runtime=runtime,
                supported_envelope_versions=frozenset({1, 2}),
            )
        app.state.anki_curation_worker = AnkiCurationWorker(
            app.state.anki_repository,
            pipeline,
            worker_id="study-hub",
            lease_seconds=resolved.anki_worker_lease_seconds,
            poll_seconds=resolved.anki_worker_poll_seconds,
            max_stage_attempts=_stage_attempt_limit(resolved, capture_store),
        )
    web_root = Path(__file__).parent / "web"
    app.mount(
        "/static",
        StaticFiles(directory=web_root / "static"),
        name="static",
    )
    app.include_router(router)
    app.include_router(anki_router)
    app.include_router(anki_agent_router)
    app.include_router(artifact_router)
    app.include_router(settings_router)
    app.include_router(settings_api_router)
    app.include_router(upload_router)
    app.include_router(quarantine_router)
    app.include_router(generation_router)
    app.include_router(anki_prompt_router)
    app.include_router(notebook_router)
    app.include_router(lecture_router)
    app.include_router(studio_router)
    app.include_router(published_quiz_router)
    app.include_router(public_quiz_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        payload = {
            "service": "oms-study-automation",
            "status": "ok",
            "version": __version__,
            "deployment_root": (
                str(resolved.deployment_root)
                if resolved.deployment_root is not None
                else "unreported"
            ),
            "build_revision": resolved.build_revision or "unreported",
        }
        if resolved.anki_rehearsal_mode != "off":
            payload.update(
                {
                    "rehearsal_nonce": app.state.anki_rehearsal_run_nonce,
                    "rehearsal_pid": str(os.getpid()),
                    "rehearsal_source": os.environ.get(
                        "OMS_HUB_ANKI_REHEARSAL_SOURCE_ROOT", "unreported"
                    ),
                    "rehearsal_source_tree_sha256": os.environ.get(
                        "OMS_HUB_ANKI_REHEARSAL_SOURCE_TREE_SHA256", "unreported"
                    ),
                    "rehearsal_commit": os.environ.get(
                        "OMS_HUB_ANKI_REHEARSAL_SOURCE_COMMIT", "unreported"
                    ),
                    "rehearsal_tree": os.environ.get(
                        "OMS_HUB_ANKI_REHEARSAL_SOURCE_TREE", "unreported"
                    ),
                }
            )
        return payload

    if capture_store is not None:
        capability = os.environ.get("OMS_HUB_ANKI_REHEARSAL_CAPTURE_CAPABILITY")
        if capability is None:
            raise ValueError("capture control-plane capability is required")
        _install_capture_control_plane(app, capture_store, capability)

    return app
