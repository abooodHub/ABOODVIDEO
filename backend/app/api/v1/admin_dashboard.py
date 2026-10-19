"""Admin Dashboard Aggregated Read-Only Summary Endpoint.

Single endpoint powering the /admin dashboard body. Combines real library,
processing, sharing, health, and activity data so the dashboard renders from
ONE request instead of 5+ parallel calls. Read-only: no state is mutated and
no dashboard data model is persisted.
"""
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import redis as redis_sync
from fastapi import APIRouter, Depends
from rq import Queue
from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.models.audit import AuditLog
from app.models.share import Share
from app.models.video import (
    PlaybackSession,
    TranscodeJob,
    TranscodeJobStatus,
    Video,
    VideoRendition,
    VideoStatus,
)
from app.security.auth import require_owner_or_admin
from app.services.health_service import HealthService
from app.workers.transcode_worker import get_worker_status

router = APIRouter(tags=["Admin Dashboard"])

TEST_TITLE_PATTERN = r"(test|sample|temp|seq_test|pause|missing|dup|checksum|auth|cancel|stale|expired|exp\b|fake|dummy|mock|demo|fixture|verification|تجربة|awesome|csrf)"
TEST_RE = re.compile(TEST_TITLE_PATTERN, re.IGNORECASE)
ACTIVE_JOB_STATUSES = [
    TranscodeJobStatus.QUEUED,
    TranscodeJobStatus.PROBING,
    TranscodeJobStatus.PROCESSING,
    TranscodeJobStatus.VERIFYING,
]


AUDIT_ACTION_LABELS = {
    "VIDEO_DELETED": ("delete", "تم حذف فيديو"),
    "VIDEO_METADATA_UPDATED": ("edit", "تم تعديل بيانات فيديو"),
    "VIDEOS_BULK_METADATA_UPDATED": ("edit", "تم تحديث بيانات مجموعة فيديوهات"),
    "UPLOAD_CANCELLED_AND_VIDEO_DELETED": ("delete", "تم إلغاء رفع وحذف فيديو"),
    "api_key.create": ("key", "تم إنشاء مفتاح API"),
    "api_key.revoke": ("key", "تم إلغاء مفتاح API"),
    "video.subtitle_uploaded": ("subtitle", "تم رفع ملف ترجمة"),
    "video.subtitle_deleted": ("subtitle", "تم حذف ملف ترجمة"),
    "transcode.cancelled": ("processing", "تم إلغاء مهمة معالجة"),
    "transcode.retried": ("processing", "إعادة محاولة لمهمة معالجة"),
}


def _is_test(text: Optional[str]) -> bool:
    """Check if a string matches automated test or fixture patterns."""
    if not text:
        return False
    return bool(TEST_RE.search(text))


def _non_test(video_cls=Video) -> Any:
    """Mirror the test-data isolation used by the admin list endpoints."""
    cond = (
        video_cls.title.op("~*")(TEST_TITLE_PATTERN)
        | video_cls.source_filename.op("~*")(TEST_TITLE_PATTERN)
    )
    return ~cond


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _read_network_counters() -> Dict[str, Any]:
    """Read container network byte counters without installing a system package."""
    try:
        received = sent = 0
        found_interface = False
        with open("/proc/net/dev", "r", encoding="ascii") as network_stats:
            for line in network_stats.readlines()[2:]:
                if ":" not in line:
                    continue
                interface, raw_counters = line.split(":", 1)
                if interface.strip() == "lo":
                    continue
                counters = [int(value) for value in raw_counters.split()]
                if len(counters) < 16:
                    continue
                received += counters[0]
                sent += counters[8]
                found_interface = True
        return {
            "available": found_interface,
            "received_bytes": received if found_interface else None,
            "sent_bytes": sent if found_interface else None,
        }
    except (OSError, ValueError):
        return {"available": False, "received_bytes": None, "sent_bytes": None}


@router.get("/admin/dashboard/live")
async def get_admin_dashboard_live_metrics(
    current_user=Depends(require_owner_or_admin),
) -> Dict[str, Any]:
    """Lightweight live network counters for the authenticated dashboard."""
    return _read_network_counters()


@router.get("/admin/dashboard")
async def get_admin_dashboard_summary(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_owner_or_admin),
) -> Dict[str, Any]:
    """Aggregated, read-only dashboard summary built from existing records."""
    now = datetime.now(timezone.utc)
    day_30_ago = now - timedelta(days=30)

    # ── Video KPI counts (mirror admin list: exclude test noise, exclude trash)
    status_counts = {
        row[0]: row[1]
        for row in (
            await db.execute(
                select(Video.status, func.count(Video.id))
                .where(_non_test(), Video.trashed_at.is_(None))
                .group_by(Video.status)
            )
        ).all()
    }
    totals = (
        await db.execute(
            select(
                func.count(Video.id),
                func.coalesce(func.sum(Video.view_count), 0),
                func.coalesce(func.sum(Video.source_size_bytes), 0),
            ).where(_non_test(), Video.trashed_at.is_(None))
        )
    ).one()

    processing_video_count = sum(
        status_counts.get(s, 0)
        for s in (
            VideoStatus.UPLOADING,
            VideoStatus.UPLOADED,
            VideoStatus.QUEUED,
            VideoStatus.PROBING,
            VideoStatus.PROCESSING,
            VideoStatus.VERIFYING,
        )
    )

    # ── Latest videos (authoritative logical Video entity deduplicated)
    video_rows = (
        await db.execute(
            select(Video)
            .where(_non_test(Video), Video.trashed_at.is_(None))
            .order_by(Video.created_at.desc(), Video.id.desc())
            .limit(6)
        )
    ).scalars().all()

    video_ids = [v.id for v in video_rows]
    rendition_map: Dict[Any, List[VideoRendition]] = {v.id: [] for v in video_rows}
    if video_ids:
        renditions = (
            await db.execute(
                select(VideoRendition)
                .where(
                    VideoRendition.video_id.in_(video_ids),
                    VideoRendition.is_ready.is_(True),
                )
                .order_by(VideoRendition.resolution_height.desc().nullslast())
            )
        ).scalars().all()
        for r in renditions:
            rendition_map[r.video_id].append(r)

    def _normalize_quality(label: str, height: Optional[int] = None) -> str:
        clean = (label or "").strip()
        if (height and height >= 2160) or "2160" in clean:
            return "4K"
        return clean

    latest_videos: List[Dict[str, Any]] = []
    for video in video_rows:
        ready_renditions = rendition_map.get(video.id, [])
        qualities_list = [_normalize_quality(r.quality_label, r.resolution_height) for r in ready_renditions]
        seen_q = set()
        unique_qualities = [q for q in qualities_list if not (q in seen_q or seen_q.add(q))]

        best_quality = unique_qualities[0] if unique_qualities else None
        q_count = len(unique_qualities)
        if best_quality and q_count > 1:
            quality_summary = f"{best_quality} · {q_count} جودات"
        elif best_quality:
            quality_summary = best_quality
        else:
            quality_summary = None

        latest_videos.append({
            "id": str(video.id),
            "title": video.title,
            "status": video.status.value,
            "quality": best_quality,
            "quality_count": q_count,
            "quality_summary": quality_summary,
            "qualities": unique_qualities,
            "duration_seconds": video.source_duration_seconds,
            "size_bytes": video.source_size_bytes,
            "created_at": _iso(video.created_at),
            "view_count": video.view_count,
            "share_id": video.share_id,
        })

    # ── Processing queue (active jobs first, newest first, cap 5, filter test videos)
    job_rows = (
        await db.execute(
            select(TranscodeJob, Video.title)
            .join(Video, TranscodeJob.video_id == Video.id)
            .where(
                _non_test(Video),
                TranscodeJob.status.in_(ACTIVE_JOB_STATUSES),
            )
            .order_by(TranscodeJob.created_at.desc())
            .limit(5)
        )
    ).all()
    jobs_items = [
        {
            "id": str(job.id),
            "video_id": str(job.video_id),
            "video_title": title,
            "status": job.status.value,
            "current_rendition": job.current_rendition,
            "progress_percent": job.progress_percent,
            "ffmpeg_fps": job.ffmpeg_fps,
            "ffmpeg_speed": job.ffmpeg_speed,
            "eta_seconds": job.eta_seconds,
            "created_at": _iso(job.created_at),
        }
        for job, title in job_rows
    ]
    active_job_count = (
        await db.execute(
            select(func.count(TranscodeJob.id))
            .join(Video, TranscodeJob.video_id == Video.id)
            .where(
                _non_test(Video),
                TranscodeJob.status.in_(ACTIVE_JOB_STATUSES),
            )
        )
    ).scalar_one()
    queued_job_count = (
        await db.execute(
            select(func.count(TranscodeJob.id))
            .join(Video, TranscodeJob.video_id == Video.id)
            .where(
                _non_test(Video),
                TranscodeJob.status.in_(
                    [TranscodeJobStatus.QUEUED, TranscodeJobStatus.PROBING]
                ),
            )
        )
    ).scalar_one()

    # ── Shares (active = not revoked and not expired)
    share_rows = (
        await db.execute(select(Share).limit(500))
    ).scalars().all()
    now_naive = now.replace(tzinfo=None)
    active_shares = 0
    for sh in share_rows:
        if sh.revoked_at is not None:
            continue
        if sh.expires_at is not None:
            exp = sh.expires_at
            if exp.tzinfo is not None:
                exp = exp.astimezone(timezone.utc).replace(tzinfo=None)
            if exp < now_naive:
                continue
        active_shares += 1

    # ── Health (reuse existing services, human-readable only)
    overall = await HealthService.get_overall_health()
    storage_health = HealthService.get_storage_health()
    storage_details = storage_health.get("details") or storage_health
    services = overall.get("services", {}) or {}

    worker: Dict[str, Any] = {"status": "OFFLINE", "last_seen_seconds_ago": None}
    try:
        conn = redis_sync.from_url(settings.REDIS_QUEUE_URL)
        worker = get_worker_status(conn)
        worker["queue_depth"] = len(Queue("transcode", connection=conn))
    except Exception:
        worker = {"status": "OFFLINE", "last_seen_seconds_ago": None, "queue_depth": None}

    # ── Daily activity (real time-series: uploads + playback sessions)
    video_day = cast(Video.created_at, Date)
    upload_days = {
        (row[0].strftime("%Y-%m-%d") if hasattr(row[0], "strftime") else str(row[0])): row[1]
        for row in (
            await db.execute(
                select(video_day, func.count(Video.id))
                .where(
                    _non_test(),
                    Video.trashed_at.is_(None),
                    Video.created_at >= day_30_ago,
                )
                .group_by(video_day)
            )
        ).all()
        if row[0] is not None
    }
    play_day = cast(PlaybackSession.started_at, Date)
    play_days = {
        (row[0].strftime("%Y-%m-%d") if hasattr(row[0], "strftime") else str(row[0])): row[1]
        for row in (
            await db.execute(
                select(play_day, func.count(PlaybackSession.id))
                .where(PlaybackSession.started_at >= day_30_ago)
                .group_by(play_day)
            )
        ).all()
        if row[0] is not None
    }
    daily: List[Dict[str, Any]] = []
    for i in range(29, -1, -1):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        daily.append({
            "date": day,
            "uploads": upload_days.get(day, 0),
            "plays": play_days.get(day, 0),
        })

    # ── Recent activity: derive ONLY from reliable existing records
    activity: List[Dict[str, Any]] = []

    recent_uploads = (
        await db.execute(
            select(Video.title, Video.created_at)
            .where(_non_test(), Video.trashed_at.is_(None))
            .order_by(Video.created_at.desc())
            .limit(6)
        )
    ).all()
    for title, created_at in recent_uploads:
        activity.append({
            "type": "upload",
            "text": f"تم رفع فيديو جديد: {title}",
            "at": _iso(created_at),
        })

    completed_jobs = (
        await db.execute(
            select(Video.title, TranscodeJob.completed_at)
            .join(Video, TranscodeJob.video_id == Video.id)
            .where(
                _non_test(Video),
                TranscodeJob.status == TranscodeJobStatus.COMPLETED,
            )
            .order_by(TranscodeJob.completed_at.desc())
            .limit(4)
        )
    ).all()
    for title, completed_at in completed_jobs:
        if completed_at is None:
            continue
        activity.append({
            "type": "processing",
            "text": f"اكتملت معالجة: {title}",
            "at": _iso(completed_at),
        })

    failed_jobs = (
        await db.execute(
            select(Video.title, TranscodeJob.completed_at, TranscodeJob.created_at)
            .join(Video, TranscodeJob.video_id == Video.id)
            .where(
                _non_test(Video),
                TranscodeJob.status == TranscodeJobStatus.FAILED,
            )
            .order_by(TranscodeJob.created_at.desc())
            .limit(3)
        )
    ).all()
    for title, completed_at, created_at in failed_jobs:
        activity.append({
            "type": "failed",
            "text": f"فشلت معالجة: {title}",
            "at": _iso(completed_at or created_at),
        })

    recent_shares_rows = (
        await db.execute(
            select(Share, Video.title)
            .outerjoin(Video, (Share.target_type == "video") & (Share.target_id == Video.id))
            .order_by(Share.created_at.desc())
            .limit(10)
        )
    ).all()
    target_labels = {
        "video": "فيديو",
        "folder": "مجلد",
        "series": "مسلسل",
        "season": "موسم",
    }
    share_count_added = 0
    for sh, v_title in recent_shares_rows:
        if share_count_added >= 4:
            break
        if sh.target_type == "video" and v_title and _is_test(v_title):
            continue
        label = target_labels.get(sh.target_type, "عنصر")
        activity.append({
            "type": "share",
            "text": f"تم إنشاء مشاركة جديدة ({label})",
            "at": _iso(sh.created_at),
        })
        share_count_added += 1

    audit_rows = (
        await db.execute(
            select(AuditLog.action, AuditLog.metadata_json, AuditLog.created_at)
            .where(AuditLog.action.in_(list(AUDIT_ACTION_LABELS.keys())))
            .order_by(AuditLog.created_at.desc())
            .limit(12)
        )
    ).all()
    audit_count_added = 0
    for action, metadata, created_at in audit_rows:
        if audit_count_added >= 8:
            break
        meta = metadata or {}
        test_markers = [meta.get("title"), meta.get("name"), meta.get("filename"), meta.get("key_name")]
        if any(_is_test(m) for m in test_markers if m):
            continue

        icon_type, label = AUDIT_ACTION_LABELS[action]
        if action == "VIDEOS_BULK_METADATA_UPDATED":
            count = meta.get("count")
            if count:
                label = f"تم تحديث بيانات {count} فيديو"
        activity.append({
            "type": icon_type,
            "text": label,
            "at": _iso(created_at),
        })
        audit_count_added += 1

    activity = [a for a in activity if a.get("at")]
    activity.sort(key=lambda a: a["at"], reverse=True)
    activity = activity[:8]

    return {
        "generated_at": _iso(now),
        "videos": {
            "total": totals[0] or 0,
            "ready": status_counts.get(VideoStatus.READY, 0),
            "processing": processing_video_count,
            "failed": status_counts.get(VideoStatus.FAILED, 0),
            "total_views": int(totals[1] or 0),
            "total_size_bytes": int(totals[2] or 0),
        },
        "storage": {
            "used_bytes": storage_details.get("used_bytes"),
            "total_bytes": storage_details.get("total_bytes"),
            "free_bytes": storage_details.get("free_bytes"),
            "used_percentage": storage_details.get("used_percentage"),
            "status": storage_health.get("status", "unknown"),
            "paths": {
                "uploads": settings.STORAGE_UPLOADS_PATH,
                "videos": settings.STORAGE_VIDEOS_PATH,
                "temp": settings.STORAGE_TEMP_PATH,
                "packages": settings.STORAGE_PACKAGES_STAGING_PATH,
            },
        },
        "jobs": {
            "active_count": active_job_count,
            "queued_count": queued_job_count,
            "items": jobs_items,
        },
        "shares": {
            "active": active_shares,
            "total": len(share_rows),
        },
        "services": {
            "api": "healthy",
            "database": services.get("database", "unknown"),
            "redis": services.get("redis", "unknown"),
            "storage": services.get("storage", "unknown"),
            "worker": worker,
        },
        "latest_videos": latest_videos,
        "activity": activity,
        "daily": daily,
    }
