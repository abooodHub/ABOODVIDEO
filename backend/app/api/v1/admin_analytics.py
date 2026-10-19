"""Admin Analytics & Telemetry API — Visitors, Views, Top Qualities & Bandwidth Metering."""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func, distinct, desc, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.config import settings
from app.security.auth import require_owner_or_admin
from app.models.video import Video, PlaybackSession, VideoStatus, VideoRendition
from app.security.ownership import owner_scope, require_video_owner
from app.models.setting import SystemSetting
from app.core.redis import get_redis
from app.security.csrf import check_csrf_protection
from app.services.geo_access import GEO_POLICY_CACHE_KEY, GEO_POLICY_KEY, get_geo_policy, trusted_country_code
import redis.asyncio as aioredis
import json
import re

router = APIRouter(prefix="/admin/analytics", tags=["Admin Analytics"])

_COUNTRY_CODE = re.compile(r"^[A-Z]{2}$")


class GeoPolicyPayload(BaseModel):
    enabled: bool
    blocked_countries: list[str] = Field(default_factory=list, max_length=250)


def _format_bytes(num_bytes: Any) -> str:
    """Format bytes into readable human format (KB, MB, GB, TB)."""
    if not num_bytes:
        return "0 B"
    try:
        val = float(num_bytes)
    except (TypeError, ValueError):
        return "0 B"
    if val <= 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if abs(val) < 1024.0:
            return f"{val:.1f} {unit}"
        val /= 1024.0
    return f"{val:.1f} EB"


def _format_duration(seconds: Any) -> str:
    """Format duration in seconds into readable MM:SS or HH:MM:SS."""
    if not seconds:
        return "00:00"
    try:
        sec_float = float(seconds)
    except (TypeError, ValueError):
        return "00:00"
    if sec_float <= 0:
        return "00:00"
    m, s = divmod(int(sec_float), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


@router.get("/overview")
async def get_analytics_overview(
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_owner_or_admin),
) -> Dict[str, Any]:
    """Retrieve platform-wide analytics: views, unique visitors, quality usage, and bandwidth."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    accessible = select(Video.id).where(Video.trashed_at.is_(None))
    if owner_scope(current_user) is not None:
        accessible = accessible.where(Video.user_id == current_user.id)

    # 1. High-level KPIs
    # Unique visitors
    visitors_res = await db.execute(
        select(func.count(distinct(PlaybackSession.ip_hash)))
        .where(PlaybackSession.started_at >= cutoff, PlaybackSession.started_at <= now, PlaybackSession.video_id.in_(accessible))
    )
    unique_visitors = visitors_res.scalar() or 0

    # Total engaged views (sessions >= 10s or is_completed)
    views_res = await db.execute(
        select(func.count(PlaybackSession.id))
        .where(
            PlaybackSession.started_at >= cutoff, PlaybackSession.started_at <= now, PlaybackSession.video_id.in_(accessible),
            PlaybackSession.is_completed == True,
        )
    )
    engaged_views = views_res.scalar() or 0

    # Lifetime counters are separate from the requested period.
    total_video_views_res = await db.execute(
        select(func.coalesce(func.sum(Video.view_count), 0))
        .where(Video.id.in_(accessible))
    )
    lifetime_views = total_video_views_res.scalar() or 0
    display_views = engaged_views

    # Bandwidth served
    bw_res = await db.execute(
        select(func.coalesce(func.sum(PlaybackSession.bytes_served), 0))
        .where(PlaybackSession.started_at >= cutoff, PlaybackSession.started_at <= now, PlaybackSession.video_id.in_(accessible))
    )
    bandwidth_bytes = int(bw_res.scalar() or 0)
    video_bw_res = await db.execute(
        select(func.coalesce(func.sum(Video.bandwidth_bytes), 0))
        .where(Video.id.in_(accessible))
    )
    lifetime_bw = int(video_bw_res.scalar() or 0)
    display_bandwidth = bandwidth_bytes

    # Average watch duration
    dur_res = await db.execute(
        select(func.coalesce(func.avg(PlaybackSession.duration_seconds), 0.0))
        .where(PlaybackSession.started_at >= cutoff, PlaybackSession.started_at <= now, PlaybackSession.video_id.in_(accessible), PlaybackSession.duration_seconds > 0)
    )
    avg_duration = float(dur_res.scalar() or 0.0)

    # 2. Quality Distribution & Top Quality
    quality_stmt = (
        select(
            PlaybackSession.quality,
            func.count(PlaybackSession.id).label("count")
        )
        .where(
            PlaybackSession.started_at >= cutoff, PlaybackSession.started_at <= now, PlaybackSession.video_id.in_(accessible),
            PlaybackSession.quality.isnot(None)
        )
        .group_by(PlaybackSession.quality)
        .order_by(desc("count"))
    )
    quality_rows = (await db.execute(quality_stmt)).all()
    total_quality_samples = sum(r[1] for r in quality_rows) if quality_rows else 0
    quality_distribution = []
    top_quality = None
    if quality_rows:
        for q, cnt in quality_rows:
            pct = round((cnt / total_quality_samples) * 100.0, 1) if total_quality_samples > 0 else 0.0
            quality_distribution.append({
                "quality": q or "Auto",
                "count": cnt,
                "percentage": pct,
            })
    # 3. Referrer Domains (which external sites embed the player)
    ref_stmt = (
        select(
            PlaybackSession.referrer_domain,
            func.count(PlaybackSession.id).label("count")
        )
        .where(
            PlaybackSession.started_at >= cutoff, PlaybackSession.started_at <= now, PlaybackSession.video_id.in_(accessible),
            PlaybackSession.referrer_domain.isnot(None),
            PlaybackSession.referrer_domain != ""
        )
        .group_by(PlaybackSession.referrer_domain)
        .order_by(desc("count"))
        .limit(10)
    )
    ref_rows = (await db.execute(ref_stmt)).all()
    total_ref_samples = sum(r[1] for r in ref_rows) if ref_rows else 0
    referrer_domains = []
    for dom, cnt in ref_rows:
        pct = round((cnt / total_ref_samples) * 100.0, 1) if total_ref_samples > 0 else 0.0
        referrer_domains.append({
            "domain": dom,
            "views": cnt,
            "percentage": pct,
        })

    # 4. Device Type Distribution
    dev_stmt = (
        select(
            PlaybackSession.device_type,
            func.count(PlaybackSession.id).label("count")
        )
        .where(
            PlaybackSession.started_at >= cutoff, PlaybackSession.started_at <= now, PlaybackSession.video_id.in_(accessible),
            PlaybackSession.device_type.isnot(None)
        )
        .group_by(PlaybackSession.device_type)
        .order_by(desc("count"))
    )
    dev_rows = (await db.execute(dev_stmt)).all()
    total_dev = sum(r[1] for r in dev_rows) if dev_rows else 0
    device_distribution = {}
    for dev, cnt in dev_rows:
        device_distribution[dev or "desktop"] = {
            "count": cnt,
            "percentage": round((cnt / total_dev) * 100.0, 1) if total_dev > 0 else 0.0
        }

    # Period totals and lifetime totals are deliberately separate.
    period = select(
        PlaybackSession.video_id.label("vid"),
        func.sum(case((PlaybackSession.is_completed.is_(True), 1), else_=0)).label("views"),
        func.sum(PlaybackSession.bytes_served).label("bytes"),
    ).where(PlaybackSession.started_at >= cutoff, PlaybackSession.started_at <= now, PlaybackSession.video_id.in_(accessible)).group_by(PlaybackSession.video_id).subquery()
    top_vids_stmt = select(
        Video.id, Video.title, Video.series_name, Video.episode_number, Video.share_id,
        func.coalesce(period.c.views, 0), func.coalesce(period.c.bytes, 0),
        Video.view_count, Video.bandwidth_bytes,
    ).outerjoin(period, period.c.vid == Video.id).where(Video.trashed_at.is_(None))
    if owner_scope(current_user) is not None:
        top_vids_stmt = top_vids_stmt.where(Video.user_id == current_user.id)
    top_vids_stmt = top_vids_stmt.order_by(desc(func.coalesce(period.c.views, 0)), desc(Video.created_at)).limit(15)
    top_vids = (await db.execute(top_vids_stmt)).all()
    ready_quality = select(VideoRendition.quality_label).join(Video).where(VideoRendition.is_ready.is_(True), Video.trashed_at.is_(None))
    if owner_scope(current_user) is not None:
        ready_quality = ready_quality.where(Video.user_id == current_user.id)
    top_quality = (await db.execute(ready_quality.order_by(VideoRendition.resolution_height.desc()).limit(1))).scalar_one_or_none()
    videos_data = []
    for vid in top_vids:
        v_id, v_title, v_series, v_ep, v_share, v_views, v_bw, v_lifetime_views, v_lifetime_bw = vid
        v_q_stmt = select(VideoRendition.quality_label).where(VideoRendition.video_id == v_id, VideoRendition.is_ready.is_(True)).order_by(VideoRendition.resolution_height.desc()).limit(1)
        v_top_q = (await db.execute(v_q_stmt)).scalar_one_or_none()

        # Per-video unique visitors
        v_vis_stmt = (
            select(func.count(distinct(PlaybackSession.ip_hash)))
            .where(PlaybackSession.video_id == v_id, PlaybackSession.started_at >= cutoff, PlaybackSession.started_at <= now, PlaybackSession.video_id.in_(accessible))
        )
        v_vis = int((await db.execute(v_vis_stmt)).scalar() or 0)

        videos_data.append({
            "id": str(v_id),
            "title": v_title,
            "series_name": v_series or "عام",
            "episode_number": v_ep,
            "share_id": v_share or "",
            "embed_url": f"{settings.PUBLIC_BASE_URL.rstrip('/')}/embed/{v_share}" if v_share else "",
            "views": int(v_views or 0),
            "lifetime_views": int(v_lifetime_views or 0),
            "lifetime_bandwidth_bytes": int(v_lifetime_bw or 0),
            "visitors": int(v_vis),
            "bandwidth_bytes": int(v_bw or 0),
            "bandwidth_formatted": _format_bytes(v_bw or 0),
            "top_quality": v_top_q,
        })

    return {
        "period_days": days,
        "kpis": {
            "views": int(display_views),
            "lifetime_views": int(lifetime_views),
            "lifetime_bandwidth_bytes": int(lifetime_bw),
            "unique_visitors": int(unique_visitors),
            "bandwidth_bytes": int(display_bandwidth),
            "bandwidth_formatted": _format_bytes(display_bandwidth),
            "avg_duration_seconds": round(avg_duration, 1),
            "avg_duration_formatted": _format_duration(avg_duration),
            "top_quality": top_quality,
        },
        "quality_distribution": quality_distribution,
        "referrer_domains": referrer_domains,
        "device_distribution": device_distribution,
        "top_videos": videos_data,
    }


@router.get("/video/{video_id}")
async def get_video_analytics(
    video_id: str,
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_owner_or_admin),
) -> Dict[str, Any]:
    """Retrieve detailed analytics for a single specific video/episode."""
    try:
        vid_uuid = uuid.UUID(video_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="معرف الفيديو غير صالح")

    video = (await db.execute(select(Video).where(Video.id == vid_uuid))).scalar_one_or_none()
    require_video_owner(video, current_user)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    # Unique visitors
    vis_res = await db.execute(
        select(func.count(distinct(PlaybackSession.ip_hash)))
        .where(PlaybackSession.video_id == vid_uuid, PlaybackSession.started_at >= cutoff, PlaybackSession.started_at <= now)
    )
    unique_vis = vis_res.scalar() or 0

    # Engaged views
    views_res = await db.execute(
        select(func.count(PlaybackSession.id))
        .where(
            PlaybackSession.video_id == vid_uuid,
            PlaybackSession.started_at >= cutoff, PlaybackSession.started_at <= now,
            PlaybackSession.is_completed == True,
        )
    )
    engaged_views = views_res.scalar() or 0

    # Average duration
    dur_res = await db.execute(
        select(func.coalesce(func.avg(PlaybackSession.duration_seconds), 0.0))
        .where(
            PlaybackSession.video_id == vid_uuid,
            PlaybackSession.started_at >= cutoff, PlaybackSession.started_at <= now,
            PlaybackSession.duration_seconds > 0,
        )
    )
    avg_duration = float(dur_res.scalar() or 0.0)

    # Qualities
    q_rows = (await db.execute(
        select(PlaybackSession.quality, func.count(PlaybackSession.id).label("c"))
        .where(PlaybackSession.video_id == vid_uuid, PlaybackSession.started_at >= cutoff, PlaybackSession.started_at <= now, PlaybackSession.quality.isnot(None))
        .group_by(PlaybackSession.quality)
        .order_by(desc("c"))
    )).all()
    tot_q = sum(r[1] for r in q_rows) if q_rows else 0
    qualities = [{"quality": r[0] or "Auto", "count": r[1], "percentage": round((r[1] / tot_q) * 100, 1) if tot_q else 0} for r in q_rows]

    # Referrers
    ref_rows = (await db.execute(
        select(PlaybackSession.referrer_domain, func.count(PlaybackSession.id).label("c"))
        .where(
            PlaybackSession.video_id == vid_uuid,
            PlaybackSession.started_at >= cutoff, PlaybackSession.started_at <= now,
            PlaybackSession.referrer_domain.isnot(None),
            PlaybackSession.referrer_domain != "",
        )
        .group_by(PlaybackSession.referrer_domain)
        .order_by(desc("c"))
        .limit(10)
    )).all()
    tot_ref = sum(r[1] for r in ref_rows) if ref_rows else 0
    referrers = [{"domain": r[0], "views": r[1], "percentage": round((r[1] / tot_ref) * 100, 1) if tot_ref else 0} for r in ref_rows]

    period_bytes = int((await db.execute(select(func.coalesce(func.sum(PlaybackSession.bytes_served), 0)).where(PlaybackSession.video_id == vid_uuid, PlaybackSession.started_at >= cutoff, PlaybackSession.started_at <= now))).scalar() or 0)
    return {
        "video": {
            "id": str(video.id),
            "title": video.title,
            "series_name": video.series_name or "عام",
            "episode_number": video.episode_number,
            "share_id": video.share_id,
            "embed_url": f"{settings.PUBLIC_BASE_URL.rstrip('/')}/embed/{video.share_id}" if video.share_id else "",
            "views": engaged_views,
            "lifetime_views": video.view_count,
            "lifetime_bandwidth_bytes": video.bandwidth_bytes,
            "unique_visitors": unique_vis,
            "bandwidth_bytes": period_bytes,
            "bandwidth_formatted": _format_bytes(period_bytes),
            "avg_duration_formatted": _format_duration(avg_duration),
        },
        "qualities": qualities,
        "referrers": referrers,
    }


@router.get("/geography")
async def get_geography_analytics(
    request: Request,
    response: Response,
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_owner_or_admin),
) -> Dict[str, Any]:
    """Country-level counts only; no raw addresses are stored or returned."""
    response.headers["Cache-Control"] = "no-store"
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    query = (
        select(PlaybackSession.country_code, func.count(PlaybackSession.id)).join(Video, Video.id == PlaybackSession.video_id)
        .where(PlaybackSession.started_at >= cutoff, PlaybackSession.started_at <= datetime.now(timezone.utc), PlaybackSession.country_code.is_not(None), Video.trashed_at.is_(None))
        .group_by(PlaybackSession.country_code)
        .order_by(desc(func.count(PlaybackSession.id)))
    )
    scope = owner_scope(current_user)
    if scope is not None:
        query = query.where(Video.user_id == scope)
    rows = (await db.execute(query)).all()
    return {"countries": {code: int(count) for code, count in rows}, "known_sessions": sum(int(row[1]) for row in rows), "source_ready": trusted_country_code(request) is not None}


@router.get("/geo-policy")
async def read_geo_policy(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis_conn: aioredis.Redis = Depends(get_redis),
    current_user=Depends(require_owner_or_admin),
):
    response.headers["Cache-Control"] = "no-store"
    return {**(await get_geo_policy(db, redis_conn)), "source_ready": trusted_country_code(request) is not None}


@router.put("/geo-policy", dependencies=[Depends(check_csrf_protection)])
async def update_geo_policy(
    payload: GeoPolicyPayload,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis_conn: aioredis.Redis = Depends(get_redis),
    current_user=Depends(require_owner_or_admin),
):
    response.headers["Cache-Control"] = "no-store"
    if payload.enabled and trusted_country_code(request) is None:
        raise HTTPException(status_code=409, detail="Cloudflare country header is unavailable through the trusted proxy")
    codes = [code.strip().upper() for code in payload.blocked_countries]
    if any(not _COUNTRY_CODE.fullmatch(code) or code in {"XX", "T1"} for code in codes):
        raise HTTPException(status_code=422, detail="Use valid two-letter country codes")
    policy = {"enabled": payload.enabled, "blocked_countries": sorted(set(codes))}
    row = await db.get(SystemSetting, GEO_POLICY_KEY)
    encoded = json.dumps(policy, separators=(",", ":"))
    if row is None:
        db.add(SystemSetting(key=GEO_POLICY_KEY, value=encoded, description="Country playback access policy"))
    else:
        row.value = encoded
    await db.commit()
    await redis_conn.delete(GEO_POLICY_CACHE_KEY)
    return policy
