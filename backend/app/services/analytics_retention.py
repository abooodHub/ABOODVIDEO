"""Bounded pruning for playback analytics only (never audit history)."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.video import PlaybackSession


async def prune_playback_analytics(
    db: AsyncSession,
    *,
    retention_days: int | None = None,
    batch_size: int | None = None,
) -> int:
    days = settings.ANALYTICS_RETENTION_DAYS if retention_days is None else retention_days
    batch = settings.ANALYTICS_RETENTION_BATCH_SIZE if batch_size is None else batch_size
    if days <= 0:
        return 0
    if not 1 <= batch <= 10000:
        raise ValueError("Analytics retention batch must be between 1 and 10000")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    expired_ids = (
        select(PlaybackSession.id)
        .where(PlaybackSession.started_at < cutoff)
        .order_by(PlaybackSession.started_at, PlaybackSession.id)
        .limit(batch)
        .with_for_update(skip_locked=True)
    )
    result = await db.execute(
        delete(PlaybackSession)
        .where(PlaybackSession.id.in_(expired_ids))
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)
