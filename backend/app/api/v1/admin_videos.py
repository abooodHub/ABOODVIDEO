"""Admin Video Management API Endpoints."""
import os
import uuid
import secrets
from pathlib import Path
from typing import Optional, List, Literal
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc, delete, asc, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.core.database import get_db
from app.core.config import settings
from app.core.logging import logger
from app.models.user import User
from app.models.video import Video, VideoStatus, VideoVisibility, VideoRendition, TranscodeJob, TranscodeJobStatus
from app.models.upload import UploadSession, UploadSessionStatus
from app.security.auth import require_owner_or_admin, record_audit_log
from app.security.ownership import owner_scope, require_video_owner
from app.security.csrf import check_csrf_protection
from app.security.hashing import hash_password
from app.storage.local import LocalStorageService
from app.services.upload_service import UploadService

router = APIRouter(
    prefix="/admin/videos",
    tags=["Admin Video Management"],
    dependencies=[Depends(require_owner_or_admin), Depends(check_csrf_protection)],
)


class VideoListItem(BaseModel):
    id: uuid.UUID
    title: str
    description: Optional[str]
    category: Optional[str] = None
    series_name: Optional[str] = None
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    status: str
    visibility: str
    share_id: Optional[str]
    share_revoked: bool
    has_viewer_password: bool
    source_filename: str
    source_size_bytes: int
    source_duration_seconds: Optional[float]
    source_resolution_width: Optional[int]
    source_resolution_height: Optional[int]
    source_codec: Optional[str]
    source_bitrate: Optional[int] = None
    rendition_count: int = 0
    subtitle_count: int = 0
    available_renditions: List[dict] = Field(default_factory=list)
    view_count: int
    bandwidth_bytes: int
    active_generation_id: Optional[uuid.UUID] = None
    created_at: str
    allow_download_policy: str = "INHERIT"
    effective_download_allowed: bool = True


class VideoListResponse(BaseModel):
    items: List[VideoListItem]
    total: int
    page: int
    limit: int


class UpdateVideoRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=2000)
    category: Optional[str] = Field(None, max_length=100)
    series_name: Optional[str] = Field(None, max_length=150)
    season_number: Optional[int] = Field(None, ge=1, le=9999)
    episode_number: Optional[int] = Field(None, ge=1, le=99999)
    visibility: Optional[VideoVisibility] = None
    allow_download_policy: Optional[str] = Field(None, pattern="^(INHERIT|ALLOW|DENY)$")
    viewer_password: Optional[str] = Field(None, min_length=4, max_length=100)
    remove_password: Optional[bool] = False
    rotate_share_link: Optional[bool] = False
    revoke_share_link: Optional[bool] = False


class BulkVideoRequest(BaseModel):
    ids: List[uuid.UUID] = Field(min_length=1, max_length=50)
    action: Literal["visibility", "category", "series", "delete", "reprocess"]
    visibility: Optional[VideoVisibility] = None
    category: Optional[str] = Field(None, max_length=100)
    series_name: Optional[str] = Field(None, max_length=150)
    season_number: Optional[int] = Field(None, ge=1, le=9999)


ACTIVE_TRANSCODE_STATUSES = (
    TranscodeJobStatus.QUEUED,
    TranscodeJobStatus.PROBING,
    TranscodeJobStatus.PROCESSING,
    TranscodeJobStatus.VERIFYING,
)


async def ensure_video_not_transcoding(db: AsyncSession, video: Video) -> None:
    active_job = (await db.execute(
        select(TranscodeJob.id).where(
            TranscodeJob.video_id == video.id,
            TranscodeJob.status.in_(ACTIVE_TRANSCODE_STATUSES),
        ).limit(1)
    )).scalar_one_or_none()
    active_video_status = video.status in (
        VideoStatus.QUEUED,
        VideoStatus.PROBING,
        VideoStatus.PROCESSING,
        VideoStatus.VERIFYING,
    )
    if active_job or active_video_status:
        raise HTTPException(status_code=409, detail="Cannot delete a video while transcoding is active")


async def delete_video_storage_or_raise(storage: LocalStorageService, video: Video) -> None:
    try:
        if video.source_path:
            await storage.delete(video.source_path)
        await storage.delete_video_dir(video.id)
    except Exception as exc:
        logger.exception("Failed to remove storage while deleting video %s", video.id)
        raise HTTPException(
            status_code=500,
            detail="Video files could not be removed; the video record was kept. Retry after storage is available.",
        ) from exc


def serialize_video(v: Video, effective_download_allowed: Optional[bool] = None) -> VideoListItem:
    ready = [r for r in v.renditions if r.is_ready]
    eff_allowed = effective_download_allowed if effective_download_allowed is not None else (v.allow_download_policy != "DENY")
    return VideoListItem(
        id=v.id, title=v.title, description=v.description,
        category=v.category, series_name=v.series_name,
        season_number=v.season_number, episode_number=v.episode_number,
        status=v.status.value, visibility=v.visibility.value,
        share_id=v.share_id, share_revoked=v.share_revoked,
        has_viewer_password=bool(v.viewer_password_hash),
        source_filename=v.source_filename, source_size_bytes=v.source_size_bytes,
        source_duration_seconds=v.source_duration_seconds,
        source_resolution_width=v.source_resolution_width,
        source_resolution_height=v.source_resolution_height,
        source_codec=v.source_codec, source_bitrate=v.source_bitrate,
        rendition_count=len(ready), subtitle_count=len(v.subtitles),
        available_renditions=[{
            "quality": r.quality_label, "width": r.resolution_width,
            "height": r.resolution_height, "codec": r.video_codec,
            "bitrate": r.video_bitrate, "size_bytes": r.size_bytes,
        } for r in sorted(ready, key=lambda r: r.resolution_height)],
        view_count=v.view_count, bandwidth_bytes=v.bandwidth_bytes,
        active_generation_id=v.active_generation_id,
        created_at=v.created_at.isoformat(),
        allow_download_policy=v.allow_download_policy or "INHERIT",
        effective_download_allowed=eff_allowed,
    )


@router.get("", response_model=VideoListResponse)
async def list_videos(
    response: Response,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    q: Optional[str] = Query(None, max_length=100),
    status: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None),
    include_test: bool = Query(False),
    category: Optional[str] = Query(None, max_length=100),
    series_name: Optional[str] = Query(None, max_length=150),
    season_number: Optional[int] = Query(None, ge=1),
    visibility: Optional[str] = Query(None),
    quality: Optional[str] = Query(None, max_length=10),
    subtitled: Optional[str] = Query(None, pattern="^(with|without)$"),
    uncategorized: bool = Query(False),
    sort: str = Query("date", pattern="^(date|title|status|size|views)$"),
    direction: str = Query("desc", pattern="^(asc|desc)$"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_owner_or_admin),
):
    """List videos in library with pagination, search, test isolation, and visibility status (Admin only)."""
    response.headers["Cache-Control"] = "no-store"
    offset = (page - 1) * limit

    base_query = select(Video).options(selectinload(Video.renditions), selectinload(Video.subtitles))
    count_query = select(func.count(Video.id))

    scope = owner_scope(current_user)
    if scope is not None:
        base_query = base_query.where(Video.user_id == scope)
        count_query = count_query.where(Video.user_id == scope)
    if not include_test:
        base_query = base_query.where(Video.is_test_content.is_(False))
        count_query = count_query.where(Video.is_test_content.is_(False))

    if q:
        search_filter = Video.title.ilike(f"%{q.strip()}%")
        base_query = base_query.where(search_filter)
        count_query = count_query.where(search_filter)

    effective_status = status or status_filter
    if effective_status:
        try:
            st = VideoStatus(effective_status.upper())
            base_query = base_query.where(Video.status == st)
            count_query = count_query.where(Video.status == st)
        except ValueError:
            pass

    for condition in (
        Video.category == category if category is not None else None,
        Video.series_name == series_name if series_name is not None else None,
        Video.season_number == season_number if season_number is not None else None,
    ):
        if condition is not None:
            base_query = base_query.where(condition)
            count_query = count_query.where(condition)

    if visibility:
        try:
            vis = VideoVisibility(visibility.upper())
            base_query = base_query.where(Video.visibility == vis)
            count_query = count_query.where(Video.visibility == vis)
        except ValueError:
            pass

    if uncategorized:
        base_query = base_query.where(Video.category.is_(None))
        count_query = count_query.where(Video.category.is_(None))

    if subtitled == "with":
        base_query = base_query.where(Video.subtitles.any())
        count_query = count_query.where(Video.subtitles.any())
    elif subtitled == "without":
        base_query = base_query.where(~Video.subtitles.any())
        count_query = count_query.where(~Video.subtitles.any())

    if quality:
        base_query = base_query.where(
            Video.renditions.any(and_(
                VideoRendition.quality_label == quality,
                VideoRendition.is_ready.is_(True),
            ))
        )
        count_query = count_query.where(
            Video.renditions.any(and_(
                VideoRendition.quality_label == quality,
                VideoRendition.is_ready.is_(True),
            ))
        )

    total = (await db.execute(count_query)).scalar() or 0
    sort_column = {"date": Video.created_at, "title": Video.title, "status": Video.status,
                   "size": Video.source_size_bytes, "views": Video.view_count}[sort]
    stmt = base_query.order_by((asc if direction == "asc" else desc)(sort_column), desc(Video.id)).offset(offset).limit(limit)
    videos = (await db.execute(stmt)).scalars().all()

    items = [serialize_video(v) for v in videos]

    return VideoListResponse(items=items, total=total, page=page, limit=limit)


@router.post("/bulk")
async def bulk_videos(
    payload: BulkVideoRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_owner_or_admin),
):
    """Apply an explicit admin action to a bounded selection; no implicit retries."""
    response.headers["Cache-Control"] = "no-store"
    if len(set(payload.ids)) != len(payload.ids):
        raise HTTPException(status_code=422, detail="Duplicate video IDs")
    videos = (await db.execute(select(Video).where(Video.id.in_(payload.ids)))).scalars().all()
    for video in videos:
        require_video_owner(video, current_user)
    if len(videos) != len(payload.ids):
        raise HTTPException(status_code=404, detail="One or more videos were not found")
    if payload.action == "visibility" and payload.visibility is None:
        raise HTTPException(status_code=422, detail="Visibility is required")
    if payload.action == "delete":
        for video in videos:
            if video.status in (VideoStatus.QUEUED, VideoStatus.PROBING, VideoStatus.PROCESSING, VideoStatus.VERIFYING):
                raise HTTPException(status_code=409, detail="Selection includes an actively processing video")
    if payload.action in ("visibility", "category", "series"):
        for video in videos:
            if payload.action == "visibility":
                video.visibility = payload.visibility
                if payload.visibility in (VideoVisibility.UNLISTED, VideoVisibility.PUBLIC, VideoVisibility.PASSWORD_PROTECTED) and not video.share_id:
                    video.share_id = secrets.token_urlsafe(16)
            elif payload.action == "category":
                video.category = payload.category.strip() or None if payload.category else None
            else:
                video.series_name = payload.series_name.strip() or None if payload.series_name else None
                video.season_number = payload.season_number
        await db.commit()
        await record_audit_log(
            db=db, action="VIDEOS_BULK_METADATA_UPDATED", user_id=current_user.id,
            target_entity="videos", target_id=",".join(str(item.id) for item in videos),
            metadata_json={"action": payload.action, "count": len(videos)},
        commit=True,
    )
        return {"updated": len(videos), "errors": []}

    results = []
    for video in videos:
        try:
            if payload.action == "delete":
                if video.status == VideoStatus.UPLOADING:
                    await cancel_upload_and_delete_video(video.id, response, db, current_user)
                else:
                    await delete_video(video.id, response, db, current_user)
            else:
                from app.api.v1.admin_transcodes import trigger_video_transcode
                await trigger_video_transcode(video.id, priority=0, current_user=current_user, db=db)
            results.append({"id": str(video.id), "ok": True})
        except HTTPException as exc:
            results.append({"id": str(video.id), "ok": False, "error": str(exc.detail)})
    return {"updated": sum(item["ok"] for item in results), "results": results}


class SeriesIndexItem(BaseModel):
    name: str
    seasons: int
    episodes: int
    ready_count: int
    processing_count: int
    total_size_bytes: int
    updated_at: Optional[str] = None


@router.get("/series", response_model=List[SeriesIndexItem])
async def list_series_index(
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_owner_or_admin),
):
    """Distinct series index with season/episode counts for admin library organization."""
    response.headers["Cache-Control"] = "no-store"
    stmt = (
        select(Video)
        .where(Video.series_name.isnot(None), Video.series_name != "")
        .options(selectinload(Video.renditions))
        .order_by(Video.series_name.asc(), Video.season_number.asc(), Video.episode_number.asc())
    )
    if owner_scope(current_user) is not None:
        stmt = stmt.where(Video.user_id == current_user.id)
    videos = (await db.execute(stmt)).scalars().all()

    series: dict = {}
    for v in videos:
        bucket = series.setdefault(v.series_name, {
            "name": v.series_name, "seasons": set(), "episodes": 0, "ready_count": 0,
            "processing_count": 0, "total_size_bytes": 0, "updated_at": None,
        })
        if v.season_number:
            bucket["seasons"].add(v.season_number)
        bucket["episodes"] += 1
        if v.status == VideoStatus.READY:
            bucket["ready_count"] += 1
        if v.status in (VideoStatus.PROCESSING, VideoStatus.QUEUED, VideoStatus.PROBING, VideoStatus.VERIFYING):
            bucket["processing_count"] += 1
        bucket["total_size_bytes"] += v.source_size_bytes or 0
        created = v.created_at.isoformat() if v.created_at else None
        if created and (bucket["updated_at"] is None or created > bucket["updated_at"]):
            bucket["updated_at"] = created

    items = [
        SeriesIndexItem(
            name=b["name"], seasons=len(b["seasons"]), episodes=b["episodes"],
            ready_count=b["ready_count"], processing_count=b["processing_count"],
            total_size_bytes=b["total_size_bytes"], updated_at=b["updated_at"],
        )
        for b in sorted(series.values(), key=lambda x: x["name"])
    ]
    return items


_FALLBACK_POSTER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180" fill="#0f172a">'
    '<rect width="320" height="180" fill="#1e293b"/>'
    '<path d="m140 70 50 30-50 30V70z" fill="#475569"/>'
    '</svg>'
)


@router.get("/{video_id}/poster")
async def get_admin_video_poster(
    video_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_owner_or_admin),
):
    """Serve the active generation's poster to authenticated admins only. Returns fallback SVG if poster file is absent."""
    video = await db.get(Video, video_id)
    if video and video.status == VideoStatus.READY and video.active_generation_id:
        poster = Path(settings.STORAGE_VIDEOS_PATH) / str(video.id) / "generations" / str(video.active_generation_id) / "poster.webp"
        if poster.is_file():
            return FileResponse(poster, media_type="image/webp", headers={"Cache-Control": "private, max-age=300"})
    return Response(
        content=_FALLBACK_POSTER_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.get("/{video_id}", response_model=VideoListItem)
async def get_video(
    video_id: uuid.UUID,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_owner_or_admin),
):
    """Retrieve full video details (Admin only)."""
    response.headers["Cache-Control"] = "no-store"
    stmt = select(Video).options(selectinload(Video.renditions), selectinload(Video.subtitles)).where(Video.id == video_id)
    video = (await db.execute(stmt)).scalar_one_or_none()
    require_video_owner(video, current_user)
    if not video:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    from app.services.drive_service import DriveService
    eff_allowed = await DriveService.get_effective_download_policy(db, video)
    return serialize_video(video, effective_download_allowed=eff_allowed)


@router.patch("/{video_id}", response_model=VideoListItem)
async def update_video(
    video_id: uuid.UUID,
    payload: UpdateVideoRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_owner_or_admin),
):
    """Update video metadata, visibility policy, download policy, viewer password, and share link."""
    response.headers["Cache-Control"] = "no-store"
    stmt = select(Video).where(Video.id == video_id)
    video = (await db.execute(stmt)).scalar_one_or_none()
    require_video_owner(video, current_user)
    if not video:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    if payload.title is not None:
        video.title = payload.title.strip()
    if payload.description is not None:
        video.description = payload.description.strip()
    for field in ("category", "series_name", "season_number", "episode_number"):
        if field in payload.model_fields_set:
            value = getattr(payload, field)
            setattr(video, field, value.strip() or None if isinstance(value, str) else value)

    # Download policy handling
    if payload.allow_download_policy is not None:
        video.allow_download_policy = payload.allow_download_policy

    # Visibility policy handling
    if payload.visibility is not None:
        video.visibility = payload.visibility
        # Generate initial share_id if UNLISTED or PASSWORD_PROTECTED and none exists
        if payload.visibility in (VideoVisibility.UNLISTED, VideoVisibility.PUBLIC, VideoVisibility.PASSWORD_PROTECTED) and not video.share_id:
            video.share_id = secrets.token_urlsafe(16)
            video.share_revoked = False

    # Share link rotation or revocation
    if payload.rotate_share_link:
        video.share_id = secrets.token_urlsafe(16)
        video.share_revoked = False

    if payload.revoke_share_link:
        video.share_revoked = True

    # Viewer password (Argon2id hashed, never plaintext)
    if payload.remove_password:
        video.viewer_password_hash = None
    elif payload.viewer_password:
        video.viewer_password_hash = hash_password(payload.viewer_password)

    await db.commit()
    await db.refresh(video)

    await record_audit_log(
        db=db,
        action="VIDEO_METADATA_UPDATED",
        user_id=current_user.id,
        target_entity="videos",
        target_id=str(video.id),
        metadata_json={
            "visibility": video.visibility.value,
            "has_password": bool(video.viewer_password_hash),
            "share_revoked": video.share_revoked,
            "allow_download_policy": video.allow_download_policy,
        },
    commit=True,
    )

    refreshed = (await db.execute(
        select(Video).options(selectinload(Video.renditions), selectinload(Video.subtitles)).where(Video.id == video_id)
    )).scalar_one()
    from app.services.drive_service import DriveService
    eff_allowed = await DriveService.get_effective_download_policy(db, refreshed)
    return serialize_video(refreshed, effective_download_allowed=eff_allowed)


@router.delete("/{video_id}")
async def delete_video(
    video_id: uuid.UUID,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_owner_or_admin),
):
    """
    Idempotently delete video and remove storage files via StorageService only.
    Blocked while active upload or finalization is in progress.
    """
    response.headers["Cache-Control"] = "no-store"
    stmt = select(Video).where(Video.id == video_id)
    video = (await db.execute(stmt)).scalar_one_or_none()
    require_video_owner(video, current_user)
    if not video:
        # Idempotent response if already deleted
        return {"success": True, "message": "Video deleted or does not exist"}

    # Block deletion if active upload session exists
    sess_stmt = select(UploadSession).where(
        UploadSession.video_id == video_id,
        UploadSession.status.in_([UploadSessionStatus.INITIATED, UploadSessionStatus.UPLOADING]),
    )
    active_session = (await db.execute(sess_stmt)).scalar_one_or_none()
    if active_session or video.status == VideoStatus.UPLOADING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete video while upload is active. Cancel the upload session first.",
        )
    await ensure_video_not_transcoding(db, video)

    # Keep the database row available for retry if storage cleanup fails.
    storage = LocalStorageService()
    await delete_video_storage_or_raise(storage, video)

    # Use a database-side delete so ON DELETE CASCADE handles every dependent
    # record without SQLAlchemy attempting to null non-nullable child keys.
    await db.execute(delete(Video).where(Video.id == video.id))
    await db.commit()

    await record_audit_log(
        db=db,
        action="VIDEO_DELETED",
        user_id=current_user.id,
        target_entity="videos",
        target_id=str(video_id),
        metadata_json={"title": video.title},
    commit=True,
    )

    return {"success": True, "message": "Video and associated files deleted successfully"}


@router.post("/{video_id}/cancel-and-delete")
async def cancel_upload_and_delete_video(
    video_id: uuid.UUID,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_owner_or_admin),
):
    """Cancel all active upload sessions for a video, then safely delete it."""
    response.headers["Cache-Control"] = "no-store"
    video = (await db.execute(select(Video).where(Video.id == video_id))).scalar_one_or_none()
    require_video_owner(video, current_user)
    if not video:
        return {"success": True, "message": "Video deleted or does not exist"}

    await ensure_video_not_transcoding(db, video)

    sessions = (await db.execute(
        select(UploadSession).where(
            UploadSession.video_id == video_id,
            UploadSession.status.in_([UploadSessionStatus.INITIATED, UploadSessionStatus.UPLOADING, UploadSessionStatus.PAUSED]),
        )
    )).scalars().all()
    for upload_session in sessions:
        await UploadService.cancel_upload(db, upload_session)

    storage = LocalStorageService()
    await delete_video_storage_or_raise(storage, video)
    await db.execute(delete(Video).where(Video.id == video.id))
    await db.commit()
    await record_audit_log(
        db=db,
        action="UPLOAD_CANCELLED_AND_VIDEO_DELETED",
        user_id=current_user.id,
        target_entity="videos",
        target_id=str(video_id),
        metadata_json={"cancelled_sessions": len(sessions)},
    commit=True,
    )
    return {"success": True, "message": "Upload cancelled and video deleted", "cancelled_sessions": len(sessions)}
