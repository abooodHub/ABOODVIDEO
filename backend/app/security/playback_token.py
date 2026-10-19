"""Secure Opaque Playback Token and Viewer Session Management."""
import json
import secrets
import hashlib
from datetime import datetime, timezone
from typing import Optional, Dict, Any
import uuid

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.config import settings
from app.core.redis import get_redis
from app.core.logging import get_logger
from app.models.video import Video, VideoStatus, VideoVisibility

logger = get_logger("playback_token")

PLAYBACK_TOKEN_TTL_SECONDS = 1800  # 30 minutes
VIEWER_SESSION_TTL_SECONDS = 86400 * 7  # 7 days
VIEWER_COOKIE_NAME = "aboodvideo_viewer_session"


def generate_opaque_token() -> str:
    """Generate high-entropy 32-byte URL-safe cryptographically random token."""
    return secrets.token_urlsafe(32)


def hash_opaque_token(raw_token: str) -> str:
    """Hash raw playback token with SHA-256 for secure keying in Redis."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def mask_token_for_logs(token: Optional[str]) -> str:
    """Mask token so raw credentials are never leaked in logs."""
    if not token or len(token) < 8:
        return "[REDACTED]"
    return f"{token[:4]}...{token[-4:]}"


async def create_playback_token(
    redis_conn: aioredis.Redis,
    video_id: uuid.UUID,
    active_generation_id: uuid.UUID,
    authorization_type: str,  # "admin", "share", "public"
    share_id: Optional[str] = None,
    viewer_session_id: Optional[str] = None,
    ttl_seconds: int = PLAYBACK_TOKEN_TTL_SECONDS,
) -> str:
    """Create an opaque playback token and persist only its SHA-256 hash in Redis."""
    raw_token = generate_opaque_token()
    token_hash = hash_opaque_token(raw_token)

    now_ts = int(datetime.now(timezone.utc).timestamp())
    expires_at = now_ts + ttl_seconds

    metadata = {
        "video_id": str(video_id),
        "active_generation_id": str(active_generation_id),
        "authorization_type": authorization_type,
        "share_id": share_id,
        "viewer_session_id": viewer_session_id,
        "created_at": now_ts,
        "expires_at": expires_at,
    }

    key = f"playback:token:{token_hash}"
    await redis_conn.setex(key, ttl_seconds, json.dumps(metadata))
    return raw_token


async def verify_playback_token(
    db: AsyncSession,
    redis_conn: aioredis.Redis,
    raw_token: str,
) -> Optional[Dict[str, Any]]:
    """Validate opaque playback token against Redis and verify live video state in DB.

    Enforces:
    - Token existence and expiry in Redis.
    - Video existence, READY status, and generation match.
    - Revocation on share link rotation, share link revocation, or visibility change to PRIVATE.
    """
    if not raw_token:
        return None

    token_hash = hash_opaque_token(raw_token)
    key = f"playback:token:{token_hash}"
    raw_val = await redis_conn.get(key)
    if not raw_val:
        return None

    try:
        data = json.loads(raw_val)
    except Exception:
        return None

    video_id = uuid.UUID(data["video_id"])
    expected_gen_id = uuid.UUID(data["active_generation_id"])
    auth_type = data.get("authorization_type")
    token_share_id = data.get("share_id")

    # Live Database State Verification
    stmt = select(Video).where(Video.id == video_id)
    result = await db.execute(stmt)
    video = result.scalar_one_or_none()

    if not video or video.status != VideoStatus.READY or video.trashed_at is not None:
        return None

    # Enforce active generation matching (invalidates immediately if generation changed)
    if not video.active_generation_id or video.active_generation_id != expected_gen_id:
        logger.warning(f"Playback token rejected: generation mismatch for video {video_id}")
        return None

    # Authorization rules for non-admin viewers
    if auth_type != "admin":
        # PRIVATE videos can never be played via viewer tokens
        if video.visibility == VideoVisibility.PRIVATE:
            return None

        # UNLISTED / PASSWORD_PROTECTED videos require active unrevoked matching share_id
        if video.visibility in (VideoVisibility.UNLISTED, VideoVisibility.PASSWORD_PROTECTED):
            if not video.share_id or video.share_id != token_share_id or video.share_revoked:
                logger.warning(f"Playback token rejected: share link revoked/rotated for video {video_id}")
                return None

    data["video"] = video
    return data


class ViewerSessionManager:
    @staticmethod
    async def grant_password_access(
        redis_conn: aioredis.Redis,
        session_id: str,
        video_id: uuid.UUID,
    ) -> None:
        """Grant a viewer session authorization to watch a password-protected video."""
        key = f"viewer:session:{session_id}"
        await redis_conn.sadd(key, str(video_id))
        await redis_conn.expire(key, VIEWER_SESSION_TTL_SECONDS)

    @staticmethod
    async def has_password_access(
        redis_conn: aioredis.Redis,
        session_id: Optional[str],
        video_id: uuid.UUID,
    ) -> bool:
        """Check if viewer session has verified password access for video."""
        if not session_id:
            return False
        key = f"viewer:session:{session_id}"
        is_member = await redis_conn.sismember(key, str(video_id))
        return bool(is_member)
