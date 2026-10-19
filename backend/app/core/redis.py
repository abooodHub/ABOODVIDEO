"""Redis Connection Manager with Strict Logical Separation."""
import time
from typing import Dict, Any, Optional
import redis.asyncio as aioredis
from app.core.config import settings
from app.core.logging import logger

# Async connection pools
_queue_pool: Optional[aioredis.ConnectionPool] = None
_session_pool: Optional[aioredis.ConnectionPool] = None
_cache_pool: Optional[aioredis.ConnectionPool] = None


def get_queue_redis_pool() -> aioredis.ConnectionPool:
    global _queue_pool
    if _queue_pool is None:
        _queue_pool = aioredis.ConnectionPool.from_url(
            settings.REDIS_QUEUE_URL,
            max_connections=settings.REDIS_POOL_MAX_CONNECTIONS,
            socket_connect_timeout=2,
            socket_timeout=2,
            decode_responses=True,
        )
    return _queue_pool


def get_session_redis_pool() -> aioredis.ConnectionPool:
    global _session_pool
    if _session_pool is None:
        _session_pool = aioredis.ConnectionPool.from_url(
            settings.REDIS_SESSION_URL,
            max_connections=settings.REDIS_POOL_MAX_CONNECTIONS,
            socket_connect_timeout=2,
            socket_timeout=2,
            decode_responses=True,
        )
    return _session_pool


def get_cache_redis_pool() -> aioredis.ConnectionPool:
    global _cache_pool
    if _cache_pool is None:
        _cache_pool = aioredis.ConnectionPool.from_url(
            settings.REDIS_CACHE_URL,
            max_connections=settings.REDIS_POOL_MAX_CONNECTIONS,
            socket_connect_timeout=2,
            socket_timeout=2,
            decode_responses=True,
        )
    return _cache_pool


def get_queue_client() -> aioredis.Redis:
    return aioredis.Redis(connection_pool=get_queue_redis_pool())


def get_session_client() -> aioredis.Redis:
    return aioredis.Redis(connection_pool=get_session_redis_pool())


def get_cache_client() -> aioredis.Redis:
    return aioredis.Redis(connection_pool=get_cache_redis_pool())


get_cache_redis = get_cache_client
get_redis_cache = get_cache_client
get_session_redis = get_session_client
get_queue_redis = get_queue_client
get_redis = get_session_client


async def check_redis_health() -> Dict[str, Any]:
    """Probe all logical Redis instances (Queue, Session, Cache) for health."""
    results = {}
    is_all_healthy = True

    checks = [
        ("queue", settings.REDIS_QUEUE_URL, get_queue_client()),
        ("session", settings.REDIS_SESSION_URL, get_session_client()),
        ("cache_ratelimit", settings.REDIS_CACHE_URL, get_cache_client()),
    ]

    for name, url, client in checks:
        start_time = time.perf_counter()
        try:
            pong = await client.ping()
            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
            results[name] = {
                "status": "healthy" if pong else "unhealthy",
                "latency_ms": latency_ms,
            }
            if not pong:
                is_all_healthy = False
        except Exception as exc:
            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
            results[name] = {
                "status": "unhealthy",
                "error": "Redis probe is temporarily unavailable.",
                "code": "REDIS_UNAVAILABLE",
                "latency_ms": latency_ms,
            }
            is_all_healthy = False

    return {
        "status": "healthy" if is_all_healthy else "unhealthy",
        "instances": results,
    }


async def close_redis_connections() -> None:
    """Graceful shutdown: disconnect all Redis connection pools."""
    global _queue_pool, _session_pool, _cache_pool
    logger.info("Closing Redis connection pools...")
    for name, pool in [("queue", _queue_pool), ("session", _session_pool), ("cache", _cache_pool)]:
        if pool is not None:
            try:
                await pool.disconnect()
            except Exception as e:
                logger.warning(f"Error closing {name} Redis pool: {e}")
    _queue_pool = None
    _session_pool = None
    _cache_pool = None
    logger.info("Redis pools closed.")
