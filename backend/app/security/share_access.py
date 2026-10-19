"""Password grants for explicit Drive shares, separate from admin sessions."""
import hashlib
import hmac
import secrets

from fastapi import HTTPException, Request, Response

from app.core.config import settings
from app.core.logging import hash_client_ip
from app.core.network import get_real_client_ip
from app.models.share import Share
from app.security.hashing import verify_password

SHARE_COOKIE_NAME = "abood_share_access"


def share_password_version(share: Share) -> str:
    return hashlib.sha256((share.password_hash or "").encode()).hexdigest()


def _grant_key(secret: str, share: Share) -> str:
    digest = hashlib.sha256(secret.encode()).hexdigest()
    return f"share:grant:{share.id}:{digest}"


async def has_share_access(request: Request, share: Share, redis_conn) -> bool:
    if not share.password_hash:
        return True
    secret = request.cookies.get(SHARE_COOKIE_NAME)
    if not secret or len(secret) > 128:
        return False
    version = await redis_conn.get(_grant_key(secret, share))
    return bool(version and hmac.compare_digest(version, share_password_version(share)))


async def require_share_access(request: Request, share: Share, redis_conn) -> None:
    if not await has_share_access(request, share, redis_conn):
        raise HTTPException(status_code=401, detail="Share password required",
                            headers={"Cache-Control": "no-store"})


async def unlock_share(request: Request, response: Response, share: Share,
                       password: str, redis_conn) -> None:
    response.headers["Cache-Control"] = "no-store"
    if not share.password_hash:
        return
    ip_hash = hash_client_ip(get_real_client_ip(request))
    key = f"share:password:{share.id}:{ip_hash}"
    limit = settings.RATE_LIMIT_VIEWER_PASSWORD_ATTEMPTS
    window = settings.RATE_LIMIT_VIEWER_PASSWORD_WINDOW_SECONDS
    # Count verification attempts atomically, including concurrent requests.
    attempts = await redis_conn.eval("""
        local count = redis.call('INCR', KEYS[1])
        if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
        return count
    """, 1, key, window)
    if attempts > limit:
        raise HTTPException(status_code=429, detail="Too many password attempts",
                            headers={"Retry-After": str(max(await redis_conn.ttl(key), 1)),
                                     "Cache-Control": "no-store"})
    if not verify_password(password, share.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect share password",
                            headers={"Cache-Control": "no-store"})
    await redis_conn.delete(key)
    # Rotate on verification so a preselected browser cookie cannot be fixed.
    secret = secrets.token_urlsafe(32)
    ttl = settings.PLAYBACK_SESSION_TTL_SECONDS
    await redis_conn.set(_grant_key(secret, share), share_password_version(share), ex=ttl)
    response.set_cookie(SHARE_COOKIE_NAME, secret, max_age=ttl, httponly=True,
                        secure=settings.SESSION_COOKIE_SECURE, samesite="lax", path="/")
    response.headers["Cache-Control"] = "no-store"

