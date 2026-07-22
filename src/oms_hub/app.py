from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from oms_hub import __version__
from oms_hub.config import Settings, get_settings
from oms_hub.canvas.api import router as canvas_api_router
from oms_hub.canvas.ingestion import IngestionService
from oms_hub.canvas.pairing import PairingService
from oms_hub.canvas.pipeline import CanvasPipeline
from oms_hub.canvas.repository import CanvasRepository
from oms_hub.db import Database
from oms_hub.security.secret_store import KeyringSecretStore
from oms_hub.files.office import SerialOfficeConverter
from oms_hub.web.routes import router
from oms_hub.web.canvas_routes import router as canvas_web_router


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    resolved.data_dir.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="OMS II Study Automation Hub", version=__version__)
    app.state.settings = resolved
    database = Database(resolved.database_url)
    database.create_schema()
    app.state.database = database
    app.state.canvas_repository = CanvasRepository(database)
    app.state.canvas_pairing = PairingService(
        app.state.canvas_repository,
        KeyringSecretStore(),
    )
    app.state.canvas_ingestion = IngestionService(app.state.canvas_repository, resolved)
    app.state.canvas_pipeline = CanvasPipeline(
        database,
        resolved,
        SerialOfficeConverter(resolved.office_timeout_seconds),
    )
    web_root = Path(__file__).parent / "web"
    app.mount(
        "/static",
        StaticFiles(directory=web_root / "static"),
        name="static",
    )
    app.include_router(router)
    app.include_router(canvas_api_router)
    app.include_router(canvas_web_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "service": "oms-study-automation",
            "status": "ok",
            "version": __version__,
        }

    return app
