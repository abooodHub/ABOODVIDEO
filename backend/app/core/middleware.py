"""Custom Middlewares for Request Tracking, Security Headers, and Sanitized Observability."""
import os
import secrets
import re
import time
import uuid
from contextvars import ContextVar
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.core.config import settings
from app.core.logging import logger, hash_client_ip, sanitize_path_for_logging
from app.core.network import get_real_client_ip, is_trusted_proxy

# ContextVar for tracing request ID across async tasks
request_id_ctx: ContextVar[str] = ContextVar("request_id_ctx", default="")

REQUEST_ID_REGEX = re.compile(r"^[a-zA-Z0-9_\-]{8,64}$")


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Ensures every incoming HTTP request has a validated, sanitized Request ID.
    Rejects malformed/injected IDs and substitutes a clean UUIDv4.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        incoming_id = request.headers.get("X-Request-ID", "").strip()
        if incoming_id and REQUEST_ID_REGEX.match(incoming_id):
            request_id = incoming_id
        else:
            request_id = uuid.uuid4().hex

        request.state.request_id = request_id
        token = request_id_ctx.set(request_id)

        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_id_ctx.reset(token)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Applies production security headers, Content-Security-Policy (CSP),
    and conditional HSTS enforcement.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        playback_secret = getattr(request.state, "playback_cookie_refresh", None)
        if playback_secret and 200 <= response.status_code < 400:
            from app.security.playback_session import build_playback_cookie
            # Match the watch-page cookie attributes. Failed authorization,
            # quality checks and missing assets must never renew credentials.
            response.headers.append("Set-Cookie", build_playback_cookie(
                playback_secret, settings.PLAYBACK_SESSION_TTL_SECONDS,
            ))

        csp_nonce = response.headers.get("X-AV-CSP-Nonce") or secrets.token_urlsafe(18)
        if "X-AV-CSP-Nonce" in response.headers:
            del response.headers["X-AV-CSP-Nonce"]

        # Baseline Security Headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        path = request.url.path.rstrip("/") or "/"
        if path in {"/", "/preview", "/admin"} or path.startswith("/admin/"):
            response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        embed_path = request.url.path.startswith("/embed/")
        if not embed_path:
            response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )

        # Content Security Policy: frame-ancestors configuration
        if embed_path:
            frame_ancestors_directive = getattr(request.state, "embed_frame_ancestors", "frame-ancestors 'self'")
        else:
            frame_ancestors_directive = "frame-ancestors 'none'"

        # Hardened Production Content Security Policy (Strict self-contained, no external CDN/font origins)
        # worker-src, media-src, and wasm-unsafe-eval support Hls.js web workers, MSE blob buffers, and JASSUB libass WASM
        csp_directives = [
            "default-src 'self'",
            "script-src 'self' 'wasm-unsafe-eval'",
            f"style-src 'self' 'nonce-{csp_nonce}'",
            "font-src 'self' blob: data:",
            "img-src 'self' data: blob:",
            "media-src 'self' blob:",
            "connect-src 'self' blob:",
            "worker-src 'self' blob:",
            "object-src 'none'",
            "base-uri 'self'",
            frame_ancestors_directive,
            "form-action 'self'",
        ]
        response.headers["Content-Security-Policy"] = "; ".join(csp_directives)

        # Conditional HSTS: Only emitted if HTTPS is active and configured
        peer_ip = request.client.host if request.client else None
        proto_hdr = request.headers.get("X-Forwarded-Proto", "").lower() if is_trusted_proxy(peer_ip) else ""
        is_https = request.url.scheme == "https" or proto_hdr == "https"
        if settings.SECURITY_MODE == "public" and settings.FORCE_HTTPS and is_https:
            hsts_val = f"max-age={settings.HSTS_MAX_AGE_SECONDS}"
            if settings.HSTS_INCLUDE_SUBDOMAINS:
                hsts_val += "; includeSubDomains"
            response.headers["Strict-Transport-Security"] = hsts_val

        return response


class PublicHTTPSRedirectMiddleware(BaseHTTPMiddleware):
    """Redirect cleartext traffic only for explicitly public deployments."""

    async def dispatch(self, request: Request, call_next) -> Response:
        peer_ip = request.client.host if request.client else None
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "").lower() if is_trusted_proxy(peer_ip) else ""
        is_https = request.url.scheme == "https" or forwarded_proto == "https"
        # The hidden root must return its empty 404 on either scheme.
        if settings.SECURITY_MODE == "public" and not is_https and request.url.path != "/":
            https_url = request.url.replace(scheme="https")
            return RedirectResponse(url=str(https_url), status_code=307)
        return await call_next(request)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Sanitized single-line access logger with HMAC-SHA256 client IP hashing
    and sensitive path redaction.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        start_time = time.perf_counter()
        response = await call_next(request)
        duration_seconds = time.perf_counter() - start_time
        duration_ms = round(duration_seconds * 1000, 2)
        from app.core.metrics import observe_request
        observe_request(request.method, getattr(request.scope.get("route"), "path", "unmatched"), response.status_code, duration_seconds)

        client_ip = get_real_client_ip(request)
        ip_hash = hash_client_ip(client_ip)
        req_id = getattr(request.state, "request_id", "unknown")
        sanitized_path = sanitize_path_for_logging(request.url.path)

        # Exclude high-frequency health probes from debug spam
        if request.url.path not in ("/health", "/readiness"):
            logger.info(
                f"{request.method} {sanitized_path} {response.status_code} in {duration_ms}ms "
                f"[req_id={req_id} ip_hash={ip_hash[:12]}]"
            )

        return response
