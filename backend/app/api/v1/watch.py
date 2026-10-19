"""Watch Page API controller including rate-limited password verification."""
import secrets
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import redis.asyncio as aioredis

from app.core.config import settings
from app.core.database import get_db
from app.core.redis import get_redis
from app.core.logging import get_logger
from app.core.network import get_real_client_ip
from app.models.video import Video, VideoStatus, VideoVisibility
from app.security.hashing import verify_password
from app.security.playback_token import (
    ViewerSessionManager,
    VIEWER_COOKIE_NAME,
    VIEWER_SESSION_TTL_SECONDS,
)

logger = get_logger("watch_api")

router = APIRouter(prefix="/watch", tags=["Watch"])


class PasswordVerificationRequest(BaseModel):
    password: str = Field(..., min_length=1, max_length=1024)


@router.post("/{share_id}/verify-password")
async def verify_viewer_password(
    share_id: str,
    body: PasswordVerificationRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis_conn: aioredis.Redis = Depends(get_redis),
):
    """Verify viewer password for PASSWORD_PROTECTED videos with rate limiting."""
    client_ip = get_real_client_ip(request)
    rate_limit_key = f"ratelimit:pw:{client_ip}:{share_id}"

    stmt = select(Video).where(Video.share_id == share_id)
    result = await db.execute(stmt)
    video = result.scalar_one_or_none()

    if (not video or video.status != VideoStatus.READY or video.share_revoked
            or video.trashed_at is not None or not video.active_generation_id
            or video.visibility == VideoVisibility.PRIVATE):
        raise HTTPException(status_code=404, detail="Video not found")

    if video.visibility != VideoVisibility.PASSWORD_PROTECTED:
        return {"success": True, "message": "Video does not require a password"}

    attempts = await redis_conn.get(rate_limit_key)
    if attempts and int(attempts) >= settings.RATE_LIMIT_VIEWER_PASSWORD_ATTEMPTS:
        ttl = max(await redis_conn.ttl(rate_limit_key), 1)
        raise HTTPException(
            status_code=429,
            detail=f"تم تجاوز الحد الأقصى للمحاولات. يرجى الانتظار {ttl} ثانية.",
            headers={"Retry-After": str(ttl)},
        )

    if not video.viewer_password_hash or not verify_password(body.password, video.viewer_password_hash):
        new_attempts = await redis_conn.incr(rate_limit_key)
        if new_attempts == 1:
            await redis_conn.expire(rate_limit_key, settings.RATE_LIMIT_VIEWER_PASSWORD_WINDOW_SECONDS)
        logger.warning(f"Failed password attempt for video {video.id} from IP {client_ip}")
        raise HTTPException(status_code=401, detail="Incorrect password")

    # Success: Clear rate limiting
    await redis_conn.delete(rate_limit_key)

    # Establish or reuse stable viewer session
    viewer_session_id = request.cookies.get(VIEWER_COOKIE_NAME)
    if not viewer_session_id:
        viewer_session_id = secrets.token_urlsafe(32)

    await ViewerSessionManager.grant_password_access(redis_conn, viewer_session_id, video.id)

    # Set stable cookie
    response.set_cookie(
        key=VIEWER_COOKIE_NAME,
        value=viewer_session_id,
        max_age=VIEWER_SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.SESSION_COOKIE_SECURE,
        path="/",
    )

    return {"success": True, "message": "Password verified successfully"}
