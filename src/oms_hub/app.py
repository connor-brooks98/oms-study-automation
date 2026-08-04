import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

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
from oms_hub.anki.repository import AnkiCurationRepository
from oms_hub.anki.runtime import AnkiRuntime, WindowsAnkiLauncher
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
from oms_hub.llm.structured import StructuredTextService
from oms_hub.repositories import CatalogRepository
from oms_hub.routing import expanded_path
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
from oms_hub.security.secret_store import KeyringSecretStore
from oms_hub.slides.pipeline import SlidePipeline
from oms_hub.study_generation.ai_settings import StudyAISettingsRepository
from oms_hub.study_generation.native_quiz import NativeQuizPublisher
from oms_hub.study_generation.notebook import StoredNotebookLMGateway
from oms_hub.study_generation.notebook_auth import NotebookCLIAuth
from oms_hub.study_generation.notebook_connection import (
    NotebookConnectionService,
    retire_google_docs_credentials,
)
from oms_hub.study_generation.outline import OutlineService
from oms_hub.study_generation.path_picker import (
    SystemPromptDirectoryPicker,
    SystemPromptPathPicker,
)
from oms_hub.study_generation.prompts import PromptFileService
from oms_hub.study_generation.quiz_images import StudioQuizImageService
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
from oms_hub.web.quarantine_routes import router as quarantine_router
from oms_hub.web.routes import router
from oms_hub.web.settings_routes import router as settings_router
from oms_hub.web.studio_routes import router as studio_router
from oms_hub.web.upload_routes import router as upload_router

logger = logging.getLogger(__name__)


def _run_sync_worker(
    stop: threading.Event,
    worker: object,
    name: str,
) -> None:
    while not stop.is_set():
        try:
            worked = bool(worker.run_once())  # type: ignore[attr-defined]
        except Exception:
            logger.exception("%s worker failed", name)
            worked = False
        stop.wait(0.5 if worked else 5.0)


@asynccontextmanager
async def _app_lifespan(app: FastAPI) -> AsyncIterator[None]:
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
    active_sync_workers = tuple(
        (name, worker) for name, worker in sync_workers if worker is not None
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
        await asyncio.gather(
            *(asyncio.to_thread(thread.join, 10) for thread in threads)
        )
        if anki_worker is not None:
            await anki_worker.stop()
        embedder = getattr(app.state, "anki_embedder", None)
        if embedder is not None:
            await embedder.aclose()
        runtime = getattr(app.state, "anki_runtime", None)
        if runtime is not None:
            await runtime.aclose()
        database = getattr(app.state, "database", None)
        if database is not None:
            database.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    resolved.data_dir.mkdir(parents=True, exist_ok=True)
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
        is_agent_host = bool(
            resolved.anki_agent_hostname and host == resolved.anki_agent_hostname
        )
        is_agent_path = request.url.path.startswith("/agent/v1/")
        is_public_quiz = (
            request.url.path == "/public/quizzes"
            or request.url.path.startswith("/public/quizzes/")
        )

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
            response.headers["Permissions-Policy"] = (
                "camera=(), microphone=(), geolocation=()"
            )
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
                if (
                    request_bytes < 0
                    or request_bytes > resolved.anki_agent_max_request_bytes
                ):
                    return harden(
                        JSONResponse(
                            {"detail": "agent request is too large"},
                            status_code=413,
                        )
                    )
            try:
                expected_token = request.app.state.secrets.get(
                    resolved.anki_agent_token_key
                )
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
                return harden(
                    JSONResponse(
                        {"detail": "Local access is disabled"}, status_code=403
                    )
                )
        else:
            return harden(
                JSONResponse({"detail": "Host is not allowed"}, status_code=400)
            )

        origin = request.headers.get("origin")
        allowed_origins = {
            f"http://127.0.0.1:{resolved.dashboard_port}",
            f"http://localhost:{resolved.dashboard_port}",
            "http://testserver",
        }
        if resolved.public_hostname:
            allowed_origins.add(f"https://{resolved.public_hostname}")
        allowed_origin = origin_is_allowed(origin, allowed_origins)
        same_origin_fetch = (
            request.headers.get("sec-fetch-site", "").casefold() == "same-origin"
        )
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

    app.state.settings = resolved
    database = Database(resolved.database_url)
    database.migrate()
    app.state.database = database
    app.state.anki_runtime = (
        AnkiRuntime(
            AnkiConnectClient(resolved.anki_connect_url),
            WindowsAnkiLauncher(resolved.anki_executable_path),
            startup_attempts=resolved.anki_startup_attempts,
            startup_poll_seconds=resolved.anki_startup_poll_seconds,
        )
        if resolved.anki_enabled
        else None
    )
    app.state.secrets = KeyringSecretStore()
    retire_google_docs_credentials(resolved.data_dir, app.state.secrets)
    app.state.study_ai_settings = StudyAISettingsRepository(database)
    app.state.anki_repository = AnkiCurationRepository(database)
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
    app.state.llm_service = LLMService(
        app.state.llm_settings,
        app.state.secrets,
        {
            ProviderName.OPENAI: OpenAIProvider(
                input_usd_per_million=resolved.openai_input_usd_per_million,
                output_usd_per_million=resolved.openai_output_usd_per_million,
            ),
            ProviderName.GEMINI: GeminiProvider(),
            ProviderName.ANTHROPIC: AnthropicProvider(),
            ProviderName.OPENROUTER: OpenRouterProvider(),
        },
    )
    app.state.medical_accuracy_gate = MedicalAccuracyGate(
        app.state.study_ai_settings,
        app.state.llm_service,
    )
    app.state.catalog_repository = CatalogRepository(database)
    app.state.ingestion_repository = IngestionRepository(database)
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
    app.state.notebook_auth = NotebookCLIAuth(notebook_storage_path)
    app.state.notebook_connection = NotebookConnectionService(
        app.state.generation_repository,
        app.state.notebook_auth,
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
    )
    app.state.transcript_prompt = V2PromptLoader(
        expanded_path(resolved.transcript_prompt_path),
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
    app.state.studio_service = StudioService(
        app.state.studio_repository,
        resolved.data_dir / "studio-sources",
        resolved.max_upload_file_bytes,
    )
    app.state.studio_quiz_image_service = StudioQuizImageService(
        app.state.studio_repository,
        resolved.data_dir / "studio-quiz-media",
    )
    app.state.studio_worker = StudioWorker(
        app.state.studio_repository,
        notebook_gateway,
        SerialOfficeConverter(resolved.office_timeout_seconds),
        app.state.notebook_connection,
        app.state.generation_repository,
        app.state.studio_quiz_image_service,
    )
    app.state.anki_curation_worker = None
    app.state.anki_embedder = None
    app.state.anki_companion_index = None
    app.state.anki_semantic_store = None
    app.state.anki_source_index = None
    if resolved.anki_enabled:
        runtime = app.state.anki_runtime
        assert isinstance(runtime, AnkiRuntime)
        embedder = VoyageEmbeddingClient(
            app.state.secrets,
            model=resolved.anki_semantic_model,
            dimensions=resolved.anki_semantic_dimensions,
            batch_size=resolved.anki_semantic_batch_size,
            api_key=resolved.voyage_api_key_value,
        )
        anki_root = resolved.resolved_anki_data_dir
        companion = AnkiIndex(anki_root / "companion")
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

        structured = StructuredTextService(app.state.llm_service)
        prompt_sync: PromptSynchronizer
        if resolved.anki_prompt_git_sync:
            if resolved.anki_prompt_directory is None:
                raise ValueError(
                    "Anki prompt Git sync requires "
                    "OMS_HUB_ANKI_PROMPT_DIRECTORY"
                )
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
        )
        app.state.anki_embedder = embedder
        app.state.anki_companion_index = companion
        app.state.anki_semantic_store = semantic_store
        app.state.anki_source_index = source_index
        app.state.anki_prompt_catalog = prompt_catalog
        app.state.anki_curation_pipeline = pipeline
        app.state.anki_apply_coordinator = ApplyCoordinator(
            app.state.anki_repository,
            cast(ApplyGateway, runtime.gateway),
            runtime=runtime,
        )
        app.state.anki_curation_worker = AnkiCurationWorker(
            app.state.anki_repository,
            pipeline,
            worker_id="study-hub",
            lease_seconds=resolved.anki_worker_lease_seconds,
            poll_seconds=resolved.anki_worker_poll_seconds,
            max_stage_attempts=(resolved.anki_worker_max_stage_attempts),
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
    app.include_router(upload_router)
    app.include_router(quarantine_router)
    app.include_router(generation_router)
    app.include_router(anki_prompt_router)
    app.include_router(notebook_router)
    app.include_router(lecture_router)
    app.include_router(studio_router)
    app.include_router(public_quiz_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "service": "oms-study-automation",
            "status": "ok",
            "version": __version__,
        }

    return app
