"""Bounded retention for expired authentication sessions."""

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session import UserSession


async def cleanup_expired_user_sessions(db: AsyncSession, batch_size: int = 250) -> int:
    """Delete only expired session rows in a bounded batch; caller owns commit."""
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    expired_ids = (
        select(UserSession.id)
        .where(UserSession.expires_at <= datetime.now(timezone.utc))
        .order_by(UserSession.expires_at, UserSession.id)
        .limit(batch_size)
    )
    result = await db.execute(delete(UserSession).where(UserSession.id.in_(expired_ids)))
    return int(result.rowcount or 0)
