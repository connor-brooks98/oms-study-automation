from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from oms_hub import __version__
from oms_hub.config import Settings, get_settings
from oms_hub.db import Database
from oms_hub.files.office import SerialOfficeConverter
from oms_hub.ingestion.matcher import UploadMatcher
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.ingestion.service import IngestionService as ManualIngestionService
from oms_hub.ingestion.staging import StagingService
from oms_hub.ingestion.worker import IngestionWorker
from oms_hub.llm.anthropic import AnthropicProvider
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.gemini import GeminiProvider
from oms_hub.llm.openai import OpenAIProvider
from oms_hub.llm.repository import LLMSettingsRepository
from oms_hub.llm.service import LLMService
from oms_hub.repositories import CatalogRepository
from oms_hub.routing import expanded_path
from oms_hub.security.access import (
    AccessIdentityForbidden,
    AccessTokenInvalid,
    CloudflareAccessVerifier,
)
from oms_hub.security.csrf import CsrfProtector, origin_is_allowed
from oms_hub.security.secret_store import KeyringSecretStore
from oms_hub.slides.pipeline import SlidePipeline
from oms_hub.transcripts.pipeline import TranscriptPipeline as V2TranscriptPipeline
from oms_hub.transcripts.prompt import PromptLoader as V2PromptLoader
from oms_hub.web.artifact_routes import router as artifact_router
from oms_hub.web.quarantine_routes import router as quarantine_router
from oms_hub.web.routes import router
from oms_hub.web.settings_routes import router as settings_router
from oms_hub.web.upload_routes import router as upload_router


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    resolved.data_dir.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="OMS II Study Automation Hub", version=__version__)
    allowed_hosts = ["127.0.0.1", "localhost", "testserver"]
    if resolved.public_hostname:
        allowed_hosts.append(resolved.public_hostname)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=allowed_hosts,
    )
    csrf = CsrfProtector.from_data_dir(resolved.data_dir)
    app.state.csrf = csrf
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
            ):
                response.headers.setdefault("Cache-Control", "no-store")
            if is_public:
                response.headers["Strict-Transport-Security"] = (
                    "max-age=31536000; includeSubDomains"
                )
            return response

        if is_public:
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
                    JSONResponse({"detail": "Local access is disabled"}, status_code=403)
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
        is_mutation = request.method in {"POST", "PUT", "PATCH", "DELETE"}
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
                    ("application/x-www-form-urlencoded", "multipart/form-data")
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
    app.state.secrets = KeyringSecretStore()
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
        },
    )
    app.state.catalog_repository = CatalogRepository(database)
    app.state.ingestion_repository = IngestionRepository(database)
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
    web_root = Path(__file__).parent / "web"
    app.mount(
        "/static",
        StaticFiles(directory=web_root / "static"),
        name="static",
    )
    app.include_router(router)
    app.include_router(artifact_router)
    app.include_router(settings_router)
    app.include_router(upload_router)
    app.include_router(quarantine_router)
    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "service": "oms-study-automation",
            "status": "ok",
            "version": __version__,
        }

    return app
