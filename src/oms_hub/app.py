from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from oms_hub import __version__
from oms_hub.canvas.api import router as canvas_api_router
from oms_hub.canvas.ingestion import IngestionService
from oms_hub.canvas.pairing import PairingService
from oms_hub.canvas.pipeline import CanvasPipeline
from oms_hub.canvas.repository import CanvasRepository
from oms_hub.config import Settings, get_settings
from oms_hub.db import Database
from oms_hub.files.office import SerialOfficeConverter
from oms_hub.panopto.api import router as panopto_api_router
from oms_hub.panopto.auth import PanoptoTokenProvider
from oms_hub.panopto.browser_service import PanoptoBrowserService
from oms_hub.panopto.client import PanoptoClient
from oms_hub.panopto.discovery import PanoptoDiscovery, PollingPolicy
from oms_hub.panopto.matcher import RecordingMatcher
from oms_hub.panopto.openai_client import OpenAITranscriptCleaner
from oms_hub.panopto.pipeline import TranscriptPipeline
from oms_hub.panopto.prompt import PromptLoader
from oms_hub.panopto.repository import PanoptoRepository
from oms_hub.repositories import CatalogRepository
from oms_hub.security.secret_store import KeyringSecretStore
from oms_hub.web.canvas_routes import router as canvas_web_router
from oms_hub.web.panopto_routes import router as panopto_web_router
from oms_hub.web.routes import router


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    resolved.data_dir.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="OMS II Study Automation Hub", version=__version__)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )

    @app.middleware("http")
    async def reject_cross_site_dashboard_posts(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        origin = request.headers.get("origin")
        allowed_origins = {
            f"http://127.0.0.1:{resolved.dashboard_port}",
            f"http://localhost:{resolved.dashboard_port}",
            "http://testserver",
        }
        if (
            request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and not request.url.path.startswith(("/api/canvas/", "/api/panopto/"))
            and origin
            and origin not in allowed_origins
        ):
            return JSONResponse({"detail": "cross-site request rejected"}, status_code=403)
        return await call_next(request)

    app.state.settings = resolved
    database = Database(resolved.database_url)
    database.create_schema()
    app.state.database = database
    app.state.secrets = KeyringSecretStore()
    app.state.canvas_repository = CanvasRepository(database)
    app.state.canvas_pairing = PairingService(
        app.state.canvas_repository,
        app.state.secrets,
    )
    app.state.canvas_ingestion = IngestionService(app.state.canvas_repository, resolved)
    app.state.canvas_pipeline = CanvasPipeline(
        database,
        resolved,
        SerialOfficeConverter(resolved.office_timeout_seconds),
    )
    catalog = CatalogRepository(database)
    app.state.panopto_repository = PanoptoRepository(
        database,
        resolved.panopto_tenant_url,
    )
    app.state.panopto_tokens = PanoptoTokenProvider(
        resolved.panopto_tenant_url,
        resolved.panopto_client_id or "",
        app.state.secrets,
    )
    app.state.panopto_client = PanoptoClient(
        resolved.panopto_tenant_url,
        app.state.panopto_tokens,
    )
    connection = app.state.panopto_repository.connection()
    app.state.panopto_prompt = PromptLoader(
        resolved.transcript_prompt_path,
        connection.approved_prompt_sha256,
    )
    app.state.openai_cleaner = OpenAITranscriptCleaner(
        app.state.secrets,
        resolved.openai_model,
        resolved.openai_input_usd_per_million,
        resolved.openai_output_usd_per_million,
    )
    app.state.panopto_pipeline = TranscriptPipeline(
        app.state.panopto_repository,
        catalog,
        app.state.panopto_prompt,
        app.state.openai_cleaner,
        resolved,
        panopto=app.state.panopto_client,
    )
    app.state.panopto_discovery = PanoptoDiscovery(
        catalog,
        app.state.panopto_repository,
        app.state.panopto_client,
        RecordingMatcher(resolved.timezone),
        PollingPolicy(
            resolved.timezone,
            resolved.panopto_poll_start,
            resolved.panopto_poll_end,
        ),
        on_match=app.state.panopto_pipeline.ingest_captions,
    )
    app.state.panopto_browser = PanoptoBrowserService(
        catalog,
        app.state.panopto_repository,
        RecordingMatcher(resolved.timezone),
        PollingPolicy(
            resolved.timezone,
            resolved.panopto_poll_start,
            resolved.panopto_poll_end,
        ),
        app.state.panopto_pipeline,
    )
    web_root = Path(__file__).parent / "web"
    app.mount(
        "/static",
        StaticFiles(directory=web_root / "static"),
        name="static",
    )
    app.include_router(router)
    app.include_router(canvas_api_router)
    app.include_router(panopto_api_router)
    app.include_router(canvas_web_router)
    app.include_router(panopto_web_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "service": "oms-study-automation",
            "status": "ok",
            "version": __version__,
        }

    return app
