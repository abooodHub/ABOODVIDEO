"""Personal API Key Authentication, Scopes, Rate Limiting, and Write Throttling."""
import hashlib
import secrets
import uuid
from typing import Optional, List, Callable
from fastapi import Header, Request, Depends, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from app.core.config import settings
from app.core.database import get_db
from app.core.redis import get_cache_redis
from app.core.exceptions import AuthenticationException, PermissionDeniedException, AboodVideoException
from app.core.logging import logger, hash_client_ip
from app.core.network import get_real_client_ip
from app.models.api_key import ApiKey
from app.models.base import utc_now


def generate_raw_api_key() -> str:
    """Generates an opaque, cryptographically random Personal API Key with prefix ak_live_."""
    return f"ak_live_{secrets.token_urlsafe(32)}"


def hash_api_key(raw_key: str) -> str:
    """Computes SHA-256 hash of raw API key for storage and lookup."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


async def check_invalid_apikey_rate_limit(request: Request, redis: aioredis.Redis) -> None:
    """Rate limits failed API key attempts per client IP HMAC."""
    client_ip = get_real_client_ip(request)
    ip_hash = hash_client_ip(client_ip)
    key = f"ratelimit:auth:apikey_fail:{ip_hash}"
    try:
        current = await redis.incr(key)
        if current == 1:
            await redis.expire(key, settings.RATE_LIMIT_APIKEY_FAIL_WINDOW_SECONDS)
        if current > settings.RATE_LIMIT_APIKEY_FAIL_ATTEMPTS:
            logger.warning(f"Rate limit exceeded for invalid API key attempts from IP hash {ip_hash[:12]}")
            raise AboodVideoException(
                message="Too many failed API key authentication attempts. Please retry later.",
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                details={"retry_after_seconds": settings.RATE_LIMIT_APIKEY_FAIL_WINDOW_SECONDS},
            )
    except AboodVideoException:
        raise
    except Exception as e:
        logger.warning(f"Redis rate limit check error (failing open for auth attempts): {e}")


async def check_api_key_rate_limit(
    api_key_id: uuid.UUID,
    action: str,
    limit: int,
    window_seconds: int,
    redis: aioredis.Redis,
) -> None:
    """
    Enforces rate limits using internal api_key.id (never human prefix or raw key).
    Example key: ratelimit:studio:init:{api_key_id}
    """
    key = f"ratelimit:studio:{action}:{api_key_id}"
    try:
        current = await redis.incr(key)
        if current == 1:
            await redis.expire(key, window_seconds)
        if current > limit:
            logger.warning(f"Rate limit exceeded for API key {api_key_id} on action {action}")
            raise AboodVideoException(
                message=f"Rate limit exceeded for action '{action}'. Please retry later.",
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                details={"action": action, "retry_after_seconds": window_seconds},
            )
    except AboodVideoException:
        raise
    except Exception as e:
        logger.warning(f"Redis rate limit check error for API key {api_key_id}: {e}")


async def touch_api_key_last_used(
    api_key_id: uuid.UUID,
    client_ip: str,
    db: AsyncSession,
    redis: aioredis.Redis,
) -> None:
    """
    Throttles last_used_at persistence in PostgreSQL to at most once per 5 minutes per key.
    Eliminates database write amplification during multi-file package uploads.
    """
    cache_key = f"apikey:last_persisted:{api_key_id}"
    try:
        # SET NX with 300s TTL: Only returns True if key did NOT already exist
        is_first_in_window = await redis.set(cache_key, "1", ex=300, nx=True)
        if is_first_in_window:
            ip_hash = hash_client_ip(client_ip)
            stmt = (
                update(ApiKey)
                .where(ApiKey.id == api_key_id)
                .values(last_used_at=utc_now(), last_used_ip_hash=ip_hash)
            )
            await db.execute(stmt)
            await db.commit()
    except Exception as e:
        logger.warning(f"Error throttling last_used_at persistence for API key {api_key_id}: {e}")


async def get_current_api_key(
    request: Request,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_cache_redis),
) -> ApiKey:
    """
    Authoritative API key dependency.
    PostgreSQL is strictly authoritative for active/revocation status.
    A stale positive cache can NEVER override a completed DB revocation.
    """
    if not authorization or not authorization.startswith("Bearer "):
        await check_invalid_apikey_rate_limit(request, redis)
        raise AuthenticationException("Missing or malformed Authorization header. Expected 'Bearer ak_live_...'")

    raw_key = authorization.split(" ", 1)[1].strip()
    if not raw_key.startswith("ak_live_"):
        await check_invalid_apikey_rate_limit(request, redis)
        raise AuthenticationException("Invalid API key format.")

    key_hash = hash_api_key(raw_key)

    # 1. Check Redis DB 2 for immediate revocation tombstone
    try:
        tombstone = await redis.get(f"apikey:revoked:{key_hash}")
        if tombstone:
            raise AuthenticationException("API key has been revoked.")
    except AuthenticationException:
        raise
    except Exception as e:
        logger.warning(f"Redis tombstone check error: {e}")

    # 2. Authoritative PostgreSQL Lookup: PostgreSQL active/revocation status is ALWAYS authoritative
    stmt = select(ApiKey).where(ApiKey.key_hash == key_hash)
    result = await db.execute(stmt)
    api_key = result.scalar_one_or_none()

    if not api_key:
        await check_invalid_apikey_rate_limit(request, redis)
        raise AuthenticationException("Invalid API key.")

    if not api_key.is_active or api_key.revoked_at is not None:
        # Cache tombstone in Redis as best-effort defense
        try:
            await redis.set(f"apikey:revoked:{key_hash}", "1", ex=300)
            await redis.delete(f"apikey:{key_hash}")
        except Exception:
            pass
        raise AuthenticationException("API key has been revoked.")

    if api_key.expires_at and api_key.expires_at < utc_now():
        raise AuthenticationException("API key has expired.")

    # 3. Touch last_used_at with 5-minute write coalescing
    client_ip = get_real_client_ip(request)
    await touch_api_key_last_used(api_key.id, client_ip, db, redis)

    return api_key


# Canonical Scopes definition
VALID_API_KEY_SCOPES = {
    "videos:read",
    "videos:create",
    "uploads:create",
    "uploads:write",
    "subtitles:write",
    "packages:create",
    "packages:write",
    "packages:publish",
    "system:monitor",
    "studio:write",  # Convenience aggregate alias
    "*",             # Full admin wildcard
}

AGGREGATE_SCOPE_EXPANSIONS = {
    "studio:write": {
        "videos:read",
        "videos:create",
        "uploads:create",
        "uploads:write",
        "subtitles:write",
        "packages:create",
        "packages:write",
        "packages:publish",
    },
    "*": {
        "videos:read",
        "videos:create",
        "uploads:create",
        "uploads:write",
        "subtitles:write",
        "packages:create",
        "packages:write",
        "packages:publish",
        "system:monitor",
    },
}


def expand_scopes(scopes: List[str]) -> set[str]:
    """Expands composite/aggregate scopes (e.g. studio:write or *) into effective permissions."""
    effective = set()
    for s in scopes:
        effective.add(s)
        if s in AGGREGATE_SCOPE_EXPANSIONS:
            effective.update(AGGREGATE_SCOPE_EXPANSIONS[s])
    return effective


def require_api_key_scope(*required_scopes: str) -> Callable:
    """Dependency factory enforcing specific granular scope membership on an authenticated API key."""

    async def scope_checker(api_key: ApiKey = Depends(get_current_api_key)) -> ApiKey:
        effective_scopes = expand_scopes(api_key.scopes)
        if "*" in effective_scopes or any(s in effective_scopes for s in required_scopes):
            return api_key
        raise PermissionDeniedException(
            f"API key lacks required scope from {list(required_scopes)}. Effective scopes: {sorted(list(effective_scopes))}"
        )

    return scope_checker


def require_all_api_key_scopes(*required_scopes: str) -> Callable:
    """Dependency factory enforcing that an authenticated API key possesses ALL required scopes."""

    async def scope_checker(api_key: ApiKey = Depends(get_current_api_key)) -> ApiKey:
        effective_scopes = expand_scopes(api_key.scopes)
        if "*" in effective_scopes:
            return api_key
        missing = [s for s in required_scopes if s not in effective_scopes]
        if missing:
            raise PermissionDeniedException(
                f"API key lacks required scopes: {missing}. Effective scopes: {sorted(list(effective_scopes))}"
            )
        return api_key

    return scope_checker
