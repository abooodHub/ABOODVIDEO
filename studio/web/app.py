"""FastAPI Web application server for ABOODVIDEO Studio."""
import sys
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from studio.web.routes.api import router as api_router
from studio.web.routes.batch_api import router as batch_api_router
from studio.config import VERSION
from studio.web.local_security import local_request_error
from shared.i18n import configure_environment, WEB_DIR
from shared.i18n.middleware import LocaleMiddleware
from studio.config import load_studio_config


def get_base_web_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "studio" / "web"
    return Path(__file__).resolve().parent


BASE_WEB_DIR = get_base_web_dir()
STATIC_DIR = BASE_WEB_DIR / "static"
TEMPLATES_DIR = BASE_WEB_DIR / "templates"

app = FastAPI(
    title="ABOODVIDEO Studio",
    version=VERSION,
    description="Local client-side transcoding, subtitle, ASS font, and package upload tool",
)

STUDIO_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self' ws://127.0.0.1:*; object-src 'none'; frame-ancestors 'none'"
)


@app.middleware("http")
async def studio_security_headers(request: Request, call_next):
    error = local_request_error(request.headers, scheme=request.url.scheme,
                                mutating=request.method not in {"GET", "HEAD", "OPTIONS"})
    if error:
        response = JSONResponse({"detail": "Studio accepts requests from its local window only."},
                                status_code=400 if error == "invalid_host" else 403)
    else:
        response = await call_next(request)
    response.headers["Content-Security-Policy"] = STUDIO_CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response

app.add_middleware(LocaleMiddleware, fallback=lambda: load_studio_config().get('language', 'ar'))
app.mount('/static/i18n', StaticFiles(directory=str(WEB_DIR)), name='i18n')
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
configure_environment(templates.env)

app.include_router(api_router)
app.include_router(batch_api_router)


@app.get("/", response_class=HTMLResponse)
async def studio_dashboard(request: Request):
    """Main Studio Dashboard view."""
    return templates.TemplateResponse(
        request=request,
        name="index_v3.html",
        context={
            "version": f"v{VERSION}",
        },
    )
