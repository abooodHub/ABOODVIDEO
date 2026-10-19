"""System Health, Liveness, Readiness, and Protected Worker Telemetry Endpoints."""
from typing import Dict, Any, Optional
from fastapi import APIRouter, Response, Depends, HTTPException, Request, Header, status
from redis import Redis
from rq import Queue
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from app.core.config import settings
from app.core.database import get_db
from app.core.redis import get_cache_redis
from app.services.health_service import HealthService
from app.schemas.health import OverallHealthResponse, ServiceHealthResponse
from app.security.auth import get_optional_current_user
from app.security.api_key import get_current_api_key, expand_scopes
from app.models.user import UserRole
from app.workers.transcode_worker import evaluate_worker_heartbeat_status
from app import __version__

import time
from app.security.auth import get_optional_current_user, require_permission, Permission
from app.models.user import User
from app.core.metrics import render_prometheus
router = APIRouter(prefix="/health", tags=["Health & Monitoring"])


@router.get("", response_model=Dict[str, Any])
async def overall_health(response: Response):
    """
    Overall system health endpoint (/api/v1/health).
    """
    data = await HealthService.get_overall_health()
    if data["status"] == "unhealthy":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return data


@router.get("/readiness", response_model=Dict[str, Any])
async def readiness_probe(response: Response):
    """
    Traffic Readiness Probe (/readiness):
    Validates PostgreSQL, Redis pools, and local storage accessibility.
    Returns HTTP 503 if any required external dependency is unreachable.
    """
    data = await HealthService.get_overall_health()
    if data["status"] == "unhealthy":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return data


@router.get("/db", response_model=ServiceHealthResponse)
async def check_database_health(
    response: Response,
    _operator: User = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """Detailed PostgreSQL health check and query latency."""
    res = await HealthService.get_db_health()
    if res.get("status") != "healthy":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ServiceHealthResponse(
        service="PostgreSQL",
        status=res.get("status", "unhealthy"),
        details=res,
    )


@router.get("/redis", response_model=ServiceHealthResponse)
async def check_redis_health(
    response: Response,
    _operator: User = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """Detailed Redis health check across logical DBs (Queue, Session, Cache)."""
    res = await HealthService.get_redis_health()
    if res.get("status") != "healthy":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ServiceHealthResponse(
        service="Redis",
        status=res.get("status", "unhealthy"),
        details=res,
    )


@router.get("/storage", response_model=ServiceHealthResponse)
def check_storage_health(
    response: Response,
    _operator: User = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """Comprehensive storage probe: read/write, inodes, atomic rename, and thresholds."""
    res = HealthService.get_storage_health()
    if res.get("status") == "unhealthy":
        response.status_code = status.HTTP_507_INSUFFICIENT_STORAGE
    return ServiceHealthResponse(
        service="LocalStorage",
        status=res.get("status", "unhealthy"),
        details=res,
    )


@router.get("/worker", response_model=Dict[str, Any])
async def check_worker_health(
    request: Request,
    response: Response,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
    cache_redis: aioredis.Redis = Depends(get_cache_redis),
):
    """
    Detailed Worker Health & Telemetry Probe (/api/v1/health/worker):
    PROTECTED ENDPOINT: Requires either an authenticated Admin Web Session
    or an API Key with 'system:monitor' scope.
    Anonymous callers receive HTTP 401/403.
    Reports worker heartbeat, current queue depth, and status.
    If the worker is stopped, returns HTTP 503 without affecting general /health or /readiness.
    """
    is_authenticated = False

    # Check 1: Admin session cookie
    user = await get_optional_current_user(request, db)
    if user and user.role in (UserRole.OWNER, UserRole.ADMIN):
        is_authenticated = True

    # Check 2: API key with system:monitor or * scope
    if not is_authenticated and authorization and authorization.startswith("Bearer "):
        api_key = await get_current_api_key(request, authorization, db, cache_redis)
        effective = expand_scopes(api_key.scopes)
        if "system:monitor" in effective or "*" in effective:
            is_authenticated = True
        else:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API key lacks required scope 'system:monitor'.",
            )

    if not is_authenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required to inspect worker health telemetry.",
        )
    response.headers["Cache-Control"] = "no-store"

    # Gather worker telemetry from Redis DB 0
    try:
        sync_redis = Redis.from_url(
            settings.REDIS_QUEUE_URL,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        hb_data = evaluate_worker_heartbeat_status(sync_redis)
        queue = Queue("transcode", connection=sync_redis)
        queue_depth = len(queue)
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "error",
            "error": "Worker telemetry is temporarily unavailable.",
            "code": "DEPENDENCY_UNAVAILABLE",
        }

    is_online = hb_data.get("status") == "ONLINE"
    if not is_online:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if is_online else "stopped",
        "worker_status": hb_data.get("status"),
        "worker_id": hb_data.get("worker_id"),
        "last_seen_seconds_ago": hb_data.get("last_seen_seconds_ago"),
        "queue_depth": queue_depth,
        "transcode_concurrency": settings.TRANSCODE_CONCURRENCY,
    }


@router.get("/metrics", include_in_schema=False)
async def metrics_endpoint(
    _operator: User = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """Protected low-cardinality Prometheus metrics."""
    return Response(content=render_prometheus(), media_type="text/plain; version=0.0.4; charset=utf-8", headers={"Cache-Control": "no-store"})



@router.get("/live", response_model=Dict[str, Any])
async def liveness_probe():
    """Process liveness only; dependency outages are handled by readiness."""
    return {"status": "ok", "version": __version__}



@router.get("/reconciler", response_model=Dict[str, Any])
async def check_reconciler_health(
    response: Response,
    _operator: User = Depends(require_permission(Permission.SYSTEM_READ)),
    cache_redis: aioredis.Redis = Depends(get_cache_redis),
):
    """Protected last-seen heartbeat for the periodic maintenance reconciler."""
    response.headers["Cache-Control"] = "no-store"
    try:
        heartbeat = await cache_redis.get(RECONCILER_HEARTBEAT_KEY)
        last_seen = float(heartbeat) if heartbeat else None
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "error", "code": "DEPENDENCY_UNAVAILABLE"}
    age = max(0, int(time.time() - last_seen)) if last_seen is not None else None
    online = age is not None and age <= settings.RECONCILER_INTERVAL_SECONDS * 3
    if not online:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "online" if online else "stopped",
        "last_seen_seconds_ago": age,
        "interval_seconds": settings.RECONCILER_INTERVAL_SECONDS,
    }


RECONCILER_HEARTBEAT_KEY = f"abv:{settings.ENVIRONMENT}:reconciler:heartbeat"
