"""CSRF Protection Utilities and Dependency."""
import hmac
import secrets
from fastapi import Request, HTTPException, status
from app.core.config import settings


def generate_csrf_token() -> str:
    """Generate cryptographically secure URL-safe CSRF token."""
    return secrets.token_urlsafe(32)


async def check_csrf_protection(request: Request) -> None:
    """
    Validate CSRF token for state-changing requests using cookie-to-header token verification.
    Protects: POST, PUT, PATCH, DELETE.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return

    # Skip login route itself (session is not yet established)
    if request.url.path in (
        f"{settings.API_V1_STR}/auth/login",
    ):
        return

    # Only enforce if request relies on session cookie authentication
    has_session_cookie = settings.SESSION_COOKIE_NAME in request.cookies
    if not has_session_cookie:
        # If request has Bearer token or test header, CSRF via cookie is not used
        return

    csrf_cookie = request.cookies.get(settings.CSRF_COOKIE_NAME)
    csrf_header = request.headers.get(settings.CSRF_HEADER_NAME) or request.headers.get("x-csrf-token")

    if not csrf_cookie or not csrf_header:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token missing in request cookie or header",
        )

    if not hmac.compare_digest(csrf_cookie, csrf_header):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token mismatch",
        )


def validate_csrf_header(request: Request, x_csrf_token: str | None = None) -> None:
    """Explicitly validate CSRF token from header or parameter."""
    has_session_cookie = settings.SESSION_COOKIE_NAME in request.cookies
    if not has_session_cookie:
        return

    csrf_cookie = request.cookies.get(settings.CSRF_COOKIE_NAME)
    csrf_header = x_csrf_token or request.headers.get(settings.CSRF_HEADER_NAME) or request.headers.get("x-csrf-token")

    if not csrf_cookie or not csrf_header:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token missing in request cookie or header",
        )

    if not hmac.compare_digest(csrf_cookie, csrf_header):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token mismatch",
        )

