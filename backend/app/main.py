"""ABOODVIDEO FastAPI Application Factory, Lifespan, and Middleware."""
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from app.core.config import settings
from app.core.logging import logger
from app.core.database import close_db_connections
from app.core.redis import close_redis_connections
from app.core.exceptions import register_exception_handlers
from app.core.middleware import (
    RequestIDMiddleware,
    SecurityHeadersMiddleware,
    RequestLoggingMiddleware,
    PublicHTTPSRedirectMiddleware,
)
from app.api.router import api_router
from app.api.wp_private_media import router as wp_private_media_router
from app.api.admin_routes import router as admin_ui_router
from app.api.watch_routes import router as watch_ui_router
from app.api.embed_routes import router as embed_ui_router
from app.api.viewer_routes import router as viewer_ui_router
from app import __version__


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application Lifespan Management:
    Handles safe startup initialization and Rule 13 graceful shutdown.
    """
    logger.info(f"=== Starting {settings.PROJECT_NAME} v{__version__} ({settings.ENVIRONMENT}) ===")
    logger.info(f"Configured Uvicorn Concurrency: {settings.WEB_CONCURRENCY}")
    logger.info(f"Storage base directory: {settings.STORAGE_BASE_PATH}")

    yield

    logger.info("=== Initiating Graceful Application Shutdown ===")
    await close_db_connections()
    await close_redis_connections()
    logger.info("=== Graceful Shutdown Completed ===")


def create_application() -> FastAPI:
    app = FastAPI(
        title=settings.PROJECT_NAME,
        version=__version__,
        description="Private Personal Video Hosting & Streaming Platform (Owner Controlled)",
        docs_url="/docs" if settings.DEBUG or settings.ENVIRONMENT != "production" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.DEBUG or settings.ENVIRONMENT != "production" else None,
        lifespan=lifespan,
    )

    # 1. Register Uniform Exception Handlers
    register_exception_handlers(app)

    # 1a. Keep missing browser pages empty; API/static errors retain JSON.
    #     Must be registered after register_exception_handlers so it does not shadow
    #     the API exception handlers that run first for /api/ prefixes.
    from fastapi import Request as _Request
    from fastapi.responses import JSONResponse as _JSONResponse

    @app.exception_handler(404)
    async def global_not_found_handler(_req: _Request, _exc):
        path = _req.url.path
        # Let API and static asset requests keep receiving JSON / file responses.
        if path.startswith("/api/") or path.startswith("/static/"):
            return _JSONResponse(status_code=404, content={"detail": "Not Found"})
        return Response(status_code=404, headers={"Cache-Control": "no-store"})



    # 2. Request Tracking & Observability Middlewares
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(PublicHTTPSRedirectMiddleware)
    app.add_middleware(RequestIDMiddleware)

    # 3. CORS Middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        allow_headers=["*"],
    )

    # 4. Security: Trusted Hosts Middleware
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.ALLOWED_HOSTS,
    )

    # 5. Include API Routers
    app.include_router(api_router, prefix=settings.API_V1_STR)
    app.include_router(wp_private_media_router)

    # 6. Include Admin UI Routes (Server-side Protected)
    app.include_router(admin_ui_router)

    # 7. Include Watch Routes (Viewer playback & Owner preview)
    app.include_router(watch_ui_router)
    app.include_router(embed_ui_router)

    # 8. Keep protected library previews and explicit share URLs available.
    app.include_router(viewer_ui_router, prefix="/preview")

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def hidden_root():
        # Return directly without rendering a page or redirecting.
        return Response(status_code=404, headers={"Cache-Control": "no-store"})

    # 9. Static Files Mount (Self-hosted assets and strict CSP scripts)
    from fastapi.staticfiles import StaticFiles
    static_dir = Path(__file__).resolve().parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # 10. Root Liveness & Readiness Endpoints
    @app.get("/health", include_in_schema=False)
    async def root_health():
        return {"status": "ok", "alive": True, "version": __version__}

    @app.get("/readiness", include_in_schema=False)
    async def root_readiness(response: Response):
        from app.services.health_service import HealthService
        from fastapi import status
        data = await HealthService.get_overall_health()
        res = {
            "status": data["status"],
            "version": data["version"],
            "database": data["services"].get("database"),
            "redis": data["services"].get("redis"),
            "storage": data["services"].get("storage"),
            "services": data["services"],
        }
        if data["status"] == "unhealthy":
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return res

    return app


app = create_application()
