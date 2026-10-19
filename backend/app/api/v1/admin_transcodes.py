"""Admin Transcoding API Endpoints with DB Active Job Guarantee & Telemetry (Phase 3)."""
from pathlib import Path
import json
import time
from typing import Optional, List, Tuple

import uuid
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select, desc, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
import redis
import redis.asyncio as aioredis
from rq import Queue

from app.core.config import settings
from app.core.database import get_db
from app.core.redis import get_cache_redis
from app.api.v1.studio_processing import PROCESSING_INDEX_KEY
from app.core.logging import logger
from app.models.user import User
from app.models.video import Video, VideoStatus, TranscodeJob, TranscodeJobStatus
from app.security.auth import require_owner_or_admin, record_audit_log
from app.security.ownership import owner_scope, require_video_owner
from app.security.csrf import check_csrf_protection
from app.services.quality_settings import get_enabled_qualities_async
from app.services.rendition_checkpoint import scan_generation_renditions
from app.services.transcoder import select_quality_ladder
from app.services.transcode_plan import build_transcode_configuration_snapshot, validate_transcode_configuration_snapshot
from app.services.encoder_settings import EncoderSelection
from app.workers.transcode_worker import get_worker_status


async def _recovery_summary(db: AsyncSession, job: TranscodeJob, video: Optional[Video]) -> Tuple[Optional[List[str]], Optional[List[str]]]:
    """
    For terminal failed/cancelled jobs, scan the job's temp generation and
    report which renditions are verified-complete (recoverable on Retry) and
    which would be regenerated. Best-effort UI summary; the worker re-verifies
    fully before any reuse. Returns (recoverable_labels, regenerate_labels).
    """
    if job.status not in (TranscodeJobStatus.FAILED, TranscodeJobStatus.CANCELLED):
        return None, None
    if video is None or not video.source_duration_seconds:
        return None, None
    try:
        probe_info = {
            "width": video.source_resolution_width or 0,
            "height": video.source_resolution_height or 0,
            "duration": float(video.source_duration_seconds),
            "bitrate": video.source_bitrate,
            "video_codec": video.source_codec or "unknown",
            "has_audio": True,
        }
        snapshot = job.configuration_snapshot
        if snapshot:
            snapshot = validate_transcode_configuration_snapshot(snapshot)
            enabled = snapshot["enabled_qualities"]
            presets = snapshot["quality_presets"]
            allow_4k = snapshot["policy"]["allow_4k_transcoding"]
        else:
            enabled = await get_enabled_qualities_async(db)
            presets = None
            allow_4k = None
        ladder = select_quality_ladder(probe_info["width"], probe_info["height"], probe_info["bitrate"], enabled, presets, allow_4k, fallback_presets=snapshot.get("source_fallback_presets") if snapshot else None)
        generation_dir = Path(settings.STORAGE_TEMP_PATH) / "transcodes" / str(video.id) / str(job.id)
        if not generation_dir.exists():
            return [], [r["quality_label"] for r in ladder]
        results = scan_generation_renditions(generation_dir, ladder, probe_info, probe_media=False)
        recoverable = sorted([label for label, res in results.items() if res["ok"]])
        regenerate = sorted([label for label, res in results.items() if not res["ok"]])
        return recoverable, regenerate
    except Exception as e:
        logger.warning(f"[TRANSCODES] Could not compute recovery summary for job {job.id}: {e}")
        return None, None


router = APIRouter(
    tags=["Admin Transcodes"],
    dependencies=[Depends(require_owner_or_admin), Depends(check_csrf_protection)],
)


class TranscodeJobItem(BaseModel):
    id: uuid.UUID
    video_id: uuid.UUID
    video_title: str
    status: str
    priority: int
    attempt: int
    max_attempts: int
    worker_id: Optional[str]
    current_rendition: Optional[str]
    progress_percent: float
    ffmpeg_fps: Optional[float]
    ffmpeg_speed: Optional[float]
    eta_seconds: Optional[int]
    started_at: Optional[str]
    completed_at: Optional[str]
    cancel_requested: bool
    error_code: Optional[str]
    error_message: Optional[str]
    configuration_snapshot: Optional[dict] = None
    created_at: str
    recoverable_renditions: Optional[List[str]] = None
    regenerate_renditions: Optional[List[str]] = None
    has_poster: bool = False


class TranscodeListResponse(BaseModel):
    items: List[TranscodeJobItem]
    total: int
    page: int
    limit: int


@router.post("/admin/videos/{video_id}/transcode", status_code=status.HTTP_202_ACCEPTED)
async def trigger_video_transcode(
    video_id: uuid.UUID,
    priority: int = Query(0, ge=0, le=10),
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
    options: Optional[EncoderSelection] = None,
):
    """
    Trigger or reprocess transcoding for a video.
    Guaranteed by PostgreSQL partial unique index to never allow concurrent active jobs.
    """
    if not settings.ENABLE_TRANSCODING:
        raise HTTPException(status_code=503, detail="Transcoding is disabled on this deployment")
    video = await db.get(Video, video_id)
    require_video_owner(video, current_user)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    if video.status in (VideoStatus.QUEUED, VideoStatus.PROBING, VideoStatus.PROCESSING, VideoStatus.VERIFYING):
        raise HTTPException(
            status_code=409,
            detail="An active transcoding job already exists for this video",
        )

    if video.status not in (VideoStatus.UPLOADED, VideoStatus.FAILED, VideoStatus.READY, VideoStatus.CANCELLED):
        raise HTTPException(
            status_code=400,
            detail=f"Video is currently in state '{video.status.value}', which cannot be transcoded.",
        )

    # 1. Application-level check
    active_job_res = await db.execute(
        select(TranscodeJob).where(
            TranscodeJob.video_id == video.id,
            TranscodeJob.status.in_([
                TranscodeJobStatus.QUEUED,
                TranscodeJobStatus.PROBING,
                TranscodeJobStatus.PROCESSING,
                TranscodeJobStatus.VERIFYING,
            ]),
        )
    )
    if active_job_res.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="An active transcoding job already exists for this video")

    # 2. Insert TranscodeJob with DB-level partial unique index protection
    job = TranscodeJob(
        video_id=video.id,
        status=TranscodeJobStatus.QUEUED,
        priority=priority,
        attempt=1,
    )
    db.add(job)
    video.status = VideoStatus.QUEUED

    try:
        job.configuration_snapshot = await build_transcode_configuration_snapshot(db, selection=options)
        await db.commit()
        await db.refresh(job)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Concurrent conflict: an active transcoding job already exists")

    # 3. Enqueue to Redis queue 'transcode'
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            redis_conn = redis.from_url(settings.REDIS_QUEUE_URL)
            q = Queue("transcode", connection=redis_conn)
            q.enqueue(
                "app.workers.transcode_worker.process_transcode_job",
                str(job.id),
                job_timeout=settings.TRANSCODE_JOB_TIMEOUT_SECONDS,
            )
    except Exception as e:
        # Revert job status if queue fails
        job.status = TranscodeJobStatus.FAILED
        job.error_code = "QUEUE_ENQUEUE_FAILED"
        job.error_message = str(e)
        video.status = VideoStatus.FAILED
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Failed to enqueue transcode job: {e}")

    await record_audit_log(
        db=db,
        action="video.transcode_triggered",
        user_id=current_user.id,
        target_entity="video",
        target_id=str(video.id),
        metadata_json={"job_id": str(job.id), "priority": priority},
    commit=True,
    )

    return {
        "message": "Transcode job queued successfully",
        "job_id": str(job.id),
        "video_id": str(video.id),
        "status": job.status.value,
    }


@router.get("/admin/transcodes/worker-status")
async def get_transcode_worker_status(
    current_user: User = Depends(require_owner_or_admin),
):
    """Retrieve current worker health state (ONLINE / STALE / OFFLINE) and telemetry."""
    redis_conn = redis.from_url(settings.REDIS_QUEUE_URL)
    status_data = get_worker_status(redis_conn)
    return status_data


@router.get("/admin/transcodes/local-studio")
async def list_local_studio_processing(
    current_user: User = Depends(require_owner_or_admin),
    cache: aioredis.Redis = Depends(get_cache_redis),
):
    """Return sanitized, short-lived Studio heartbeats; local paths are never stored."""
    now = int(time.time())
    await cache.zremrangebyscore(PROCESSING_INDEX_KEY, "-inf", now - 86400)
    keys = await cache.zrevrange(PROCESSING_INDEX_KEY, 0, 199)
    values = await cache.mget(keys) if keys else []
    items = []
    stale_keys = []
    for key, raw in zip(keys, values):
        if not raw:
            stale_keys.append(key)
            continue
        try:
            item = json.loads(raw)
        except (TypeError, ValueError):
            continue
        # Ignore malformed/expired data and cap the response against accidental key buildup.
        if isinstance(item, dict) and item.get("status"):
            status_value = str(item.get("status") or "").upper()
            updated_at = int(item.get("updated_at") or 0)
            is_active = status_value in {"PROBING", "READY_TO_ENCODE", "ENCODING", "PACKAGING", "VERIFYING_LOCAL", "UPLOADING", "SERVER_VERIFYING"}
            item["connection_state"] = "connected" if is_active and now - updated_at <= 15 else ("stale" if is_active else "terminal")
            items.append(item)
    if stale_keys:
        await cache.zrem(PROCESSING_INDEX_KEY, *stale_keys)
    connected = any(item.get("connection_state") == "connected" for item in items)
    return {"items": items, "total": len(items), "studio_connected": connected}


@router.get("/admin/transcodes", response_model=TranscodeListResponse)
async def list_transcode_jobs(
    status_filter: Optional[str] = Query(None, alias="status"),
    video_id: Optional[uuid.UUID] = Query(None),
    tab: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    include_test: bool = Query(False),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
):
    """List transcode jobs with video titles, telemetry, tabs, test isolation, and pagination."""
    query = select(TranscodeJob, Video).join(Video, TranscodeJob.video_id == Video.id)
    count_query = select(func.count(TranscodeJob.id)).join(Video, TranscodeJob.video_id == Video.id)
    scope = owner_scope(current_user)
    if scope is not None:
        query = query.where(Video.user_id == scope)
        count_query = count_query.where(Video.user_id == scope)

    # Test Data Isolation: Exclude test jobs unless explicitly requested
    if not include_test:
        query = query.where(Video.is_test_content.is_(False))
        count_query = count_query.where(Video.is_test_content.is_(False))

    if video_id is not None:
        query = query.where(TranscodeJob.video_id == video_id)
        count_query = count_query.where(TranscodeJob.video_id == video_id)

    if q:
        search_filter = Video.title.ilike(f"%{q.strip()}%")
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)

    if tab == "active":
        active_statuses = [TranscodeJobStatus.QUEUED, TranscodeJobStatus.PROBING, TranscodeJobStatus.PROCESSING, TranscodeJobStatus.VERIFYING]
        query = query.where(TranscodeJob.status.in_(active_statuses))
        count_query = count_query.where(TranscodeJob.status.in_(active_statuses))
    elif tab == "history":
        history_statuses = [TranscodeJobStatus.COMPLETED, TranscodeJobStatus.CANCELLED, TranscodeJobStatus.FAILED]
        query = query.where(TranscodeJob.status.in_(history_statuses))
        count_query = count_query.where(TranscodeJob.status.in_(history_statuses))
    elif tab == "failed":
        query = query.where(TranscodeJob.status == TranscodeJobStatus.FAILED)
        count_query = count_query.where(TranscodeJob.status == TranscodeJobStatus.FAILED)
    elif tab == "queued":
        q_statuses = [TranscodeJobStatus.QUEUED, TranscodeJobStatus.PROBING]
        query = query.where(TranscodeJob.status.in_(q_statuses))
        count_query = count_query.where(TranscodeJob.status.in_(q_statuses))

    if status_filter:
        try:
            status_enum = TranscodeJobStatus(status_filter.upper())
            query = query.where(TranscodeJob.status == status_enum)
            count_query = count_query.where(TranscodeJob.status == status_enum)
        except ValueError:
            pass

    query = query.order_by(desc(TranscodeJob.created_at))
    total = (await db.execute(count_query)).scalar_one()

    # Paginate
    offset = (page - 1) * limit
    paged_res = await db.execute(query.offset(offset).limit(limit))
    rows = paged_res.all()

    items = []
    for job, video in rows:
        recoverable, regenerate = await _recovery_summary(db, job, video)
        items.append(
            TranscodeJobItem(
                id=job.id,
                video_id=job.video_id,
                video_title=video.title,
                status=job.status.value,
                priority=job.priority,
                attempt=job.attempt,
                max_attempts=job.max_attempts,
                worker_id=job.worker_id,
                current_rendition=job.current_rendition,
                progress_percent=job.progress_percent,
                ffmpeg_fps=job.ffmpeg_fps,
                ffmpeg_speed=job.ffmpeg_speed,
                eta_seconds=job.eta_seconds,
                started_at=job.started_at.isoformat() if job.started_at else None,
                completed_at=job.completed_at.isoformat() if job.completed_at else None,
                cancel_requested=job.cancel_requested,
                error_code=job.error_code,
                error_message=job.error_message,
                configuration_snapshot=job.configuration_snapshot,
                created_at=job.created_at.isoformat(),
                recoverable_renditions=recoverable,
                regenerate_renditions=regenerate,
                has_poster=bool(video and video.poster_path and video.active_generation_id),
            )
        )

    return TranscodeListResponse(items=items, total=total, page=page, limit=limit)


@router.get("/admin/transcodes/{job_id}")
async def get_transcode_job_details(
    job_id: uuid.UUID,
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get detailed telemetry and diagnostic information for a specific job."""
    res = await db.execute(
        select(TranscodeJob, Video).join(Video, TranscodeJob.video_id == Video.id).where(TranscodeJob.id == job_id)
    )
    row = res.first()
    if not row:
        raise HTTPException(status_code=404, detail="Transcode job not found")

    job, video = row
    require_video_owner(video, current_user)
    recoverable, regenerate = await _recovery_summary(db, job, video)
    return {
        "id": str(job.id),
        "video_id": str(video.id),
        "video_title": video.title,
        "status": job.status.value,
        "priority": job.priority,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "worker_id": job.worker_id,
        "current_rendition": job.current_rendition,
        "progress_percent": job.progress_percent,
        "ffmpeg_fps": job.ffmpeg_fps,
        "ffmpeg_speed": job.ffmpeg_speed,
        "eta_seconds": job.eta_seconds,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "cancel_requested": job.cancel_requested,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat(),
        "video_status": video.status.value,
        "video_source_size": video.source_size_bytes,
        "video_active_generation_id": str(video.active_generation_id) if video.active_generation_id else None,
        "recoverable_renditions": recoverable,
        "regenerate_renditions": regenerate,
    }


@router.post("/admin/transcodes/{job_id}/cancel")
async def cancel_transcode_job(
    job_id: uuid.UUID,
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Request cancellation of a transcode job.
    Kills the worker's FFmpeg process group cleanly without leaving zombies.
    """
    job = await db.get(TranscodeJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Transcode job not found")
    require_video_owner(await db.get(Video, job.video_id), current_user)

    if job.status in (TranscodeJobStatus.COMPLETED, TranscodeJobStatus.FAILED, TranscodeJobStatus.CANCELLED):
        return {"message": f"Job already in terminal state {job.status.value}", "status": job.status.value}

    job.cancel_requested = True
    if job.status == TranscodeJobStatus.QUEUED:
        job.status = TranscodeJobStatus.CANCELLED
        video = await db.get(Video, job.video_id)
        if video and not video.active_generation_id:
            video.status = VideoStatus.CANCELLED

    await db.commit()

    await record_audit_log(
        db=db,
        action="transcode.cancelled",
        user_id=current_user.id,
        target_entity="transcode_job",
        target_id=str(job.id),
        metadata_json={"video_id": str(job.video_id)},
    commit=True,
    )

    return {"message": "Job cancellation requested", "job_id": str(job.id), "status": job.status.value}


@router.post("/admin/transcodes/{job_id}/retry")
async def retry_transcode_job(
    job_id: uuid.UUID,
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Retry a failed or cancelled transcode job.
    Increments attempt counter and retains canonical generation isolation.
    """
    if not settings.ENABLE_TRANSCODING:
        raise HTTPException(status_code=503, detail="Transcoding is disabled on this deployment")
    prior_job = await db.get(TranscodeJob, job_id)
    if not prior_job:
        raise HTTPException(status_code=404, detail="Transcode job not found")
    require_video_owner(await db.get(Video, prior_job.video_id), current_user)

    if prior_job.status in (
        TranscodeJobStatus.QUEUED,
        TranscodeJobStatus.PROBING,
        TranscodeJobStatus.PROCESSING,
        TranscodeJobStatus.VERIFYING,
    ):
        raise HTTPException(status_code=409, detail="Job is currently active, cannot retry")

    if prior_job.attempt >= prior_job.max_attempts:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum transcode attempts ({prior_job.max_attempts}) reached for this job sequence.",
        )

    # Check that video has no other active job
    active_check = await db.execute(
        select(TranscodeJob).where(
            TranscodeJob.video_id == prior_job.video_id,
            TranscodeJob.status.in_([
                TranscodeJobStatus.QUEUED,
                TranscodeJobStatus.PROBING,
                TranscodeJobStatus.PROCESSING,
                TranscodeJobStatus.VERIFYING,
            ]),
        )
    )
    if active_check.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="An active transcoding job already exists for this video")

    video = await db.get(Video, prior_job.video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video record not found")

    # Create new job with incremented attempt
    new_job = TranscodeJob(
        video_id=video.id,
        status=TranscodeJobStatus.QUEUED,
        attempt=prior_job.attempt + 1,
        max_attempts=prior_job.max_attempts,
        priority=prior_job.priority,
        configuration_snapshot=await build_transcode_configuration_snapshot(db),
    )
    db.add(new_job)
    video.status = VideoStatus.QUEUED

    try:
        await db.commit()
        await db.refresh(new_job)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Concurrent conflict: active job already exists")

    # Enqueue new job
    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        redis_conn = redis.from_url(settings.REDIS_QUEUE_URL)
        q = Queue("transcode", connection=redis_conn)
        q.enqueue(
            "app.workers.transcode_worker.process_transcode_job",
            str(new_job.id),
            job_timeout=settings.TRANSCODE_JOB_TIMEOUT_SECONDS,
        )

    recoverable, regenerate = await _recovery_summary(db, prior_job, video)

    await record_audit_log(
        db=db,
        action="transcode.retried",
        user_id=current_user.id,
        target_entity="transcode_job",
        target_id=str(new_job.id),
        metadata_json={"prior_job_id": str(prior_job.id), "attempt": new_job.attempt},
    commit=True,
    )

    return {
        "message": "Retry job created and enqueued",
        "job_id": str(new_job.id),
        "attempt": new_job.attempt,
        "status": new_job.status.value,
        "recoverable_renditions": recoverable or [],
        "regenerate_renditions": regenerate or [],
    }
