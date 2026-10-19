"""Secure short-lived Playback Sessions authorized through an HttpOnly cookie.

Architecture (Phase 1 - VIP ready):

- A *principal* represents the watching identity for one browser (owner, admin,
  VIP subscriber, or anonymous viewer). It is created when an authorized watch
  page is rendered and is carried in the ``abood_playback`` HttpOnly cookie
  (opaque random secret, never exposed to JavaScript).
- A *session* binds a principal to one video with an entitlement snapshot
  (max quality, download flag) and lives in Redis with a 15 minute sliding TTL.
- Every playback resource (master playlist, variants, segments, subtitles,
  fonts, poster, encryption key) is authorized per request:
  the session id in the URL plus the cookie secret are BOTH required, so a
  copied URL without the authorized browser session fails with 401/403.

Future ABOODVIDEO VIP fields (external_user_id, plan, max_quality,
download_allowed, concurrent_stream_limit) are already part of the principal
record; only the WordPress-side issuer remains to be integrated.
"""
import hashlib
import json
import re
import secrets
import time
import uuid
from typing import Any, Dict, Optional, Tuple

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.video import Video, VideoStatus, VideoVisibility
from app.models.user import User, UserRole

logger = get_logger("playback_session")

QUALITY_RANK = {
    "360p": 360,
    "480p": 480,
    "720p": 720,
    "1080p": 1080,
    "1440p": 1440,
    "2160p": 2160,
    "4K": 2160,
}

_QUALITY_LABEL_RE = re.compile(r"^(\d{3,4})p?$", re.IGNORECASE)


def quality_rank(label: Optional[str]) -> Optional[int]:
    """Map a quality label ('2160p', '4K') to its numeric rank (height)."""
    if not label:
        return None
    label = label.strip()
    if label in QUALITY_RANK:
        return QUALITY_RANK[label]
    m = _QUALITY_LABEL_RE.match(label)
    if m:
        return int(m.group(1))
    return None


def highest_rendition_rank(renditions) -> int:
    """Best available quality rank from a video's rendition list."""
    best = 0
    for r in renditions or []:
        rank = quality_rank(getattr(r, "quality_label", None))
        if rank is None:
            h = getattr(r, "resolution_height", None)
            rank = int(h) if h else None
        if rank and rank > best:
            best = rank
    return best


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _pkey(principal_hash: str) -> str:
    return f"pb:principal:{principal_hash}"


def _skey(session_id: str) -> str:
    return f"pb:sess:{session_id}"


def _plist_key(principal_hash: str) -> str:
    return f"pb:plist:{principal_hash}"


def mask_session_for_logs(session_id: Optional[str]) -> str:
    """Short non-reversible identifier for logs."""
    if not session_id or len(session_id) < 8:
        return "[none]"
    return f"{session_id[:6]}...{session_id[-4:]}"


async def create_playback_principal(
    redis_conn: aioredis.Redis,
    *,
    user_id: Optional[str],
    external_user_id: Optional[str],
    plan: str,  # "owner" | "admin" | "vip" | "free"
    max_quality: str,
    download_allowed: bool,
    concurrent_stream_limit: int,
    user_agent: str = "",
    ip: str = "",
    ttl_seconds: Optional[int] = None,
) -> str:
    """Create a playback principal and return its opaque secret (cookie value)."""
    ttl = ttl_seconds or settings.PLAYBACK_SESSION_TTL_SECONDS
    secret = secrets.token_urlsafe(32)
    ph = _hash_secret(secret)
    now = int(time.time())

    ua_hash = hashlib.sha256(f"{user_agent[:128]}:{settings.SECRET_KEY}".encode()).hexdigest()[:32]
    ip_hash = hashlib.sha256(f"{ip}:{settings.SECRET_KEY}".encode()).hexdigest()[:32]

    record = {
        "user_id": user_id or "",
        "external_user_id": external_user_id or "",
        "plan": plan,
        "max_quality": max_quality,
        "download_allowed": "1" if download_allowed else "0",
        "concurrent_stream_limit": str(concurrent_stream_limit),
        "created_at": str(now),
        "expires_at": str(now + ttl),
        "last_seen_at": str(now),
        "revoked": "0",
        "ua_hash": ua_hash,
        "ip_hash": ip_hash,
    }
    await redis_conn.hset(_pkey(ph), mapping=record)
    await redis_conn.expire(_pkey(ph), ttl)
    return secret


async def create_playback_session(
    redis_conn: aioredis.Redis,
    *,
    principal_secret: str,
    video_id,
    generation_id,
    authorization_type: str,  # "admin" | "share" | "public" | "drive_share"
    share_id: Optional[str] = None,
    viewer_session_id: Optional[str] = None,
    max_quality: str,
    download_allowed: bool,
    ttl_seconds: Optional[int] = None,
    drive_share_token: Optional[str] = None,
    drive_share_password_version: Optional[str] = None,
) -> str:
    """Create a playback session bound to the principal's video. Returns session id."""
    ttl = ttl_seconds or settings.PLAYBACK_SESSION_TTL_SECONDS
    ph = _hash_secret(principal_secret)
    sid = secrets.token_urlsafe(18)
    now = int(time.time())

    record = {
        "principal_hash": ph,
        "video_id": str(video_id),
        "generation_id": str(generation_id),
        "authorization_type": authorization_type,
        "drive_share_token": drive_share_token or "",
        "drive_share_password_version": drive_share_password_version or "",
        "share_id": share_id or "",
        "viewer_session_id": viewer_session_id or "",
        "max_quality": max_quality,
        "max_quality_rank": str(quality_rank(max_quality) or 0),
        "download_allowed": "1" if download_allowed else "0",
        "created_at": str(now),
        "expires_at": str(now + ttl),
        "last_seen_at": str(now),
        "revoked": "0",
    }
    # Register and enforce the cap atomically across API processes.
    plist = _plist_key(ph)
    limit = settings.PLAYBACK_MAX_CONCURRENT_SESSIONS
    await redis_conn.eval("""
        redis.call('HSET', KEYS[1], unpack(ARGV, 5))
        redis.call('EXPIRE', KEYS[1], ARGV[2])
        for _, old in ipairs(redis.call('ZRANGE', KEYS[2], 0, -1)) do
            if redis.call('EXISTS', 'pb:sess:' .. old) == 0 then
                redis.call('ZREM', KEYS[2], old)
            end
        end
        redis.call('ZADD', KEYS[2], ARGV[4], ARGV[1])
        local personal_limit = tonumber(redis.call('HGET', KEYS[3], 'concurrent_stream_limit')) or tonumber(ARGV[3])
        local cap = math.max(1, math.min(tonumber(ARGV[3]), personal_limit))
        local excess = redis.call('ZCARD', KEYS[2]) - cap
        if excess > 0 then
            for _, old in ipairs(redis.call('ZRANGE', KEYS[2], 0, excess - 1)) do
                redis.call('DEL', 'pb:sess:' .. old)
                redis.call('ZREM', KEYS[2], old)
            end
        end
        redis.call('EXPIRE', KEYS[2], ARGV[2])
        return 1
    """, 3, _skey(sid), plist, _pkey(ph), sid, ttl, limit, time.time(),
        *[item for pair in record.items() for item in pair])
    return sid


async def revoke_playback_session(redis_conn: aioredis.Redis, session_id: str) -> None:
    """Revoke a single playback session immediately."""
    await redis_conn.delete(_skey(session_id))


async def revoke_playback_principal(redis_conn: aioredis.Redis, principal_secret: str) -> None:
    """Revoke a principal and every playback session derived from it."""
    ph = _hash_secret(principal_secret)
    plist = _plist_key(ph)
    sids = await redis_conn.zrange(plist, 0, -1)
    if sids:
        for sid in sids:
            await redis_conn.delete(_skey(sid))
        await redis_conn.delete(plist)
    await redis_conn.delete(_pkey(ph))


async def _sliding_refresh(redis_conn: aioredis.Redis, sid: str, ph: str, ttl: int) -> None:
    """Extend session and principal lifetime (15 minute sliding expiry)."""
    now = int(time.time())
    pipe = redis_conn.pipeline(transaction=False)
    pipe.hset(_skey(sid), mapping={"last_seen_at": str(now), "expires_at": str(now + ttl)})
    pipe.expire(_skey(sid), ttl)
    pipe.hset(_pkey(ph), mapping={"last_seen_at": str(now), "expires_at": str(now + ttl)})
    pipe.expire(_pkey(ph), ttl)
    pipe.expire(_plist_key(ph), ttl)
    await pipe.execute()


async def verify_playback_session(
    db: AsyncSession,
    redis_conn: aioredis.Redis,
    session_id: str,
    principal_secret: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Authorize a playback request.

    Returns (context, None) on success where context contains "video",
    "session" and "principal"; returns (None, reason) on failure.
    """
    if not session_id or not principal_secret:
        return None, "missing_credentials"

    s = await redis_conn.hgetall(_skey(session_id))
    if not s:
        return None, "session_not_found"
    if s.get("revoked") == "1":
        return None, "session_revoked"

    now = int(time.time())
    if now > int(s.get("expires_at", "0")):
        return None, "session_expired"

    ph = _hash_secret(principal_secret)
    if s.get("principal_hash") != ph:
        return None, "principal_mismatch"

    p = await redis_conn.hgetall(_pkey(ph))
    if not p:
        return None, "principal_not_found"
    if p.get("revoked") == "1":
        return None, "principal_revoked"
    if now > int(p.get("expires_at", "0")):
        return None, "principal_expired"

    # Local identities remain revocable after the short-lived Redis capability
    # was issued. Re-evaluate account state and quality caps for each media
    # authorization so disabling/downgrading an account takes effect promptly.
    principal_user = None
    principal_user_id = p.get("user_id")
    if principal_user_id:
        try:
            principal_uuid = uuid.UUID(principal_user_id)
        except (ValueError, TypeError, AttributeError):
            return None, "principal_corrupt"
        principal_user = await db.scalar(select(User).where(User.id == principal_uuid))
        if not principal_user or not principal_user.is_active or principal_user.role == UserRole.BANNED:
            return None, "principal_revoked"
        if s.get("authorization_type") == "admin" and principal_user.role not in (UserRole.OWNER, UserRole.ADMIN):
            return None, "admin_authority_revoked"
        from app.services.entitlements import playback_entitlement_for_user
        current_rank = quality_rank(playback_entitlement_for_user(principal_user).max_quality) or 0
        snapshot_rank = int(s.get("max_quality_rank") or 0)
        s["max_quality_rank"] = str(min(snapshot_rank, current_rank))

    ttl = settings.PLAYBACK_SESSION_TTL_SECONDS
    renew_cookie = now - int(s.get("last_seen_at", "0")) >= settings.PLAYBACK_SLIDING_REFRESH_SECONDS
    if renew_cookie:
        await _sliding_refresh(redis_conn, session_id, ph, ttl)

    # Live database state verification (same guarantees as legacy tokens)
    try:
        video_id = uuid.UUID(s["video_id"])
        expected_gen_id = uuid.UUID(s["generation_id"])
    except (KeyError, ValueError, AttributeError):
        return None, "session_corrupt"

    stmt = select(Video).where(Video.id == video_id)
    result = await db.execute(stmt)
    video = result.scalar_one_or_none()

    if not video or video.status != VideoStatus.READY or video.trashed_at is not None:
        return None, "video_unavailable"
    if not video.active_generation_id or video.active_generation_id != expected_gen_id:
        return None, "generation_mismatch"

    auth_type = s.get("authorization_type")
    if auth_type == "drive_share":
        from app.services.drive_service import DriveService
        from app.security.share_access import share_password_version
        share = await DriveService.get_share_by_token(db, s.get("drive_share_token", ""))
        if (not share or video.share_revoked
                or s.get("drive_share_password_version") != share_password_version(share)
                or not await DriveService.get_shared_video_if_available(db, share, video.id)):
            return None, "share_revoked"
        if video.visibility == VideoVisibility.PRIVATE and (
            not principal_user or principal_user.role not in (UserRole.OWNER, UserRole.ADMIN)
            or (principal_user.role != UserRole.ADMIN and video.user_id != principal_user.id)
        ):
            return None, "private_video"
        if video.visibility == VideoVisibility.PASSWORD_PROTECTED:
            from app.security.playback_token import ViewerSessionManager
            if not await ViewerSessionManager.has_password_access(redis_conn, s.get("viewer_session_id"), video.id):
                return None, "password_required"
    elif auth_type != "admin":
        if video.visibility == VideoVisibility.PRIVATE:
            return None, "private_video"
        if video.visibility in (VideoVisibility.UNLISTED, VideoVisibility.PASSWORD_PROTECTED):
            token_share_id = s.get("share_id")
            if not video.share_id or video.share_id != token_share_id or video.share_revoked:
                return None, "share_revoked"

    return {"video": video, "session": s, "principal": p, "renew_cookie": renew_cookie}, None


def session_allows_quality(session: Dict[str, str], subpath: str) -> bool:
    """Entitlement check: requested rendition must be <= session max quality."""
    first = (subpath or "").split("/", 1)[0]
    req = quality_rank(first)
    if req is None:
        return True  # non-quality-scoped asset (subtitles, fonts, poster, root)
    allowed = int(session.get("max_quality_rank") or 0)
    return req <= allowed


def build_playback_cookie(secret: str, max_age: int) -> str:
    """Build the Set-Cookie value for the HttpOnly playback credential."""
    parts = [
        f"{settings.PLAYBACK_COOKIE_NAME}={secret}",
        "HttpOnly",
        "SameSite=Lax",
        "Path=/",
        f"Max-Age={max_age}",
    ]
    if settings.SESSION_COOKIE_SECURE:
        parts.append("Secure")
    return "; ".join(parts)
