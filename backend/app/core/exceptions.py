"""Custom Exceptions and Uniform Error Response Handlers."""
from typing import Any, Dict, Optional
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from app.core.logging import logger


class AboodVideoException(Exception):
    """Base exception for application-level errors."""

    def __init__(
        self,
        message: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        details: Optional[Dict[str, Any]] = None,
        error_code: Optional[str] = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        self.error_code = error_code


class StorageException(AboodVideoException):
    """Raised on storage or disk threshold violations."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            details=details,
        )


class AuthenticationException(AboodVideoException):
    """Raised on authentication failures."""

    def __init__(self, message: str = "Authentication required", error_code: str = "AUTH_SESSION_EXPIRED"):
        super().__init__(
            message=message,
            status_code=status.HTTP_401_UNAUTHORIZED,
            error_code=error_code,
        )


class PermissionDeniedException(AboodVideoException):
    """Raised on authorization / role violations."""

    def __init__(self, message: str = "Permission denied"):
        super().__init__(
            message=message,
            status_code=status.HTTP_403_FORBIDDEN,
            error_code="AUTH_FORBIDDEN",
        )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AboodVideoException)
    async def app_exception_handler(request: Request, exc: AboodVideoException):
        route = request.scope.get("route")
        route_path = getattr(route, "path", "unmatched")
        logger.warning(
            f"handled_api_exception code={exc.error_code or exc.__class__.__name__} "
            f"route={route_path} status={exc.status_code} "
            f"request_id={getattr(request.state, 'request_id', 'unknown')}"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "error": {
                    "code": exc.error_code or exc.__class__.__name__,
                    "message": exc.message,
                    "details": exc.details,
                    "request_id": getattr(request.state, "request_id", None),
                },
            },
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        """Normalize only authentication responses during Phase 6.0."""
        if not request.url.path.startswith("/api/v1/auth/"):
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)

        code = {
            status.HTTP_401_UNAUTHORIZED: "AUTH_INVALID_CREDENTIALS" if request.url.path.endswith("/login") else "AUTH_SESSION_EXPIRED",
            status.HTTP_403_FORBIDDEN: "AUTH_FORBIDDEN",
            status.HTTP_429_TOO_MANY_REQUESTS: "AUTH_RATE_LIMITED",
            status.HTTP_503_SERVICE_UNAVAILABLE: "SYSTEM_UNAVAILABLE",
        }.get(exc.status_code, "SYSTEM_UNAVAILABLE")
        is_login = request.url.path.endswith("/login")
        message = "Invalid username or password." if is_login and code in {"AUTH_INVALID_CREDENTIALS", "AUTH_FORBIDDEN"} else str(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            headers=exc.headers,
            content={
                "success": False,
                "error": {
                    "code": code,
                    "message": message,
                    "request_id": getattr(request.state, "request_id", None),
                },
            },
        )

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        route = request.scope.get("route")
        route_path = getattr(route, "path", "unmatched")
        logger.error(
            f"unhandled_server_error error_type={type(exc).__name__} route={route_path} "
            f"request_id={getattr(request.state, 'request_id', 'unknown')}"
        )
        auth_request = request.url.path.startswith("/api/v1/auth/")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE if auth_request else status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": {
                    "code": "SYSTEM_UNAVAILABLE" if auth_request else "INTERNAL_SERVER_ERROR",
                    "message": "Authentication service is temporarily unavailable. Please try again later." if auth_request else "An internal server error occurred. Please contact administrator.",
                    "request_id": getattr(request.state, "request_id", None),
                },
            },
        )
