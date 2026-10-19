"""Read-only, permission-protected managed-storage accounting."""

import asyncio
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.models.package import PackageSessionStatus, PackageUploadSession
from app.models.upload import UploadSession, UploadSessionStatus
from app.models.user import User
from app.models.video import (
    TranscodeJob, TranscodeJobStatus, Video, VideoFontAsset, VideoRendition,
    VideoStatus, VideoSubtitle,
)
from app.security.auth import Permission, require_permission
from app.services.storage_accounting import (
    find_missing_managed_objects, find_orphan_temporary_resources, scan_managed_storage,
)


router = APIRouter(prefix="/admin/storage", tags=["Admin Storage"])


@router.get("/summary")
async def storage_summary(
    _operator: User = Depends(require_permission(Permission.SYSTEM_READ)),
    db: AsyncSession = Depends(get_db),
):
    video_rows = (await db.execute(select(
        Video.id, Video.trashed_at, Video.status, Video.active_generation_id,
        Video.source_path, Video.master_playlist_path, Video.poster_path,
    ))).all()
    video_states = {}
    video_generations = {}
    managed_references = []

    def add_video_reference(kind, identity, video_id, generation_id, raw_path):
        if not raw_path:
            return
        path = str(raw_path).replace("\\", "/")
        if not Path(path).is_absolute():
            if path.startswith("videos/"):
                pass
            elif path.startswith("generations/"):
                path = f"videos/{video_id}/{path}"
            else:
                suffix = f"generations/{generation_id}/{path}" if generation_id else path
                path = f"videos/{video_id}/{suffix}"
        managed_references.append({"kind": kind, "identity": str(identity), "path": path})

    for video_id, trashed_at, status, generation_id, source_path, master_path, poster_path in video_rows:
        if trashed_at is not None:
            state = "trash"
        elif status == VideoStatus.READY and generation_id is not None:
            state = "published"
        else:
            state = "unpublished"
        video_states[str(video_id)] = state
        video_generations[video_id] = generation_id
        if source_path:
            add_video_reference("video_source", video_id, video_id, generation_id, source_path)
        if status == VideoStatus.READY and generation_id is not None:
            add_video_reference("video_master_playlist", video_id, video_id, generation_id, master_path)
            add_video_reference("video_poster", video_id, video_id, generation_id, poster_path)

    rendition_rows = (await db.execute(select(
        VideoRendition.id, VideoRendition.video_id, VideoRendition.playlist_path,
    ).where(VideoRendition.is_ready.is_(True)))).all()
    for rendition_id, video_id, playlist_path in rendition_rows:
        generation_id = video_generations.get(video_id)
        add_video_reference("video_rendition_playlist", rendition_id, video_id, generation_id, playlist_path)

    subtitle_rows = (await db.execute(select(
        VideoSubtitle.id, VideoSubtitle.video_id, VideoSubtitle.generation_id,
        VideoSubtitle.source_path, VideoSubtitle.webvtt_path,
    ).where(VideoSubtitle.is_playable.is_(True)))).all()
    for subtitle_id, video_id, generation_id, source_path, webvtt_path in subtitle_rows:
        add_video_reference("video_subtitle_source", subtitle_id, video_id, generation_id, source_path)
        add_video_reference("video_subtitle_webvtt", subtitle_id, video_id, generation_id, webvtt_path)

    font_rows = (await db.execute(select(
        VideoFontAsset.id, VideoFontAsset.video_id, VideoFontAsset.generation_id,
        VideoFontAsset.relative_path,
    ))).all()
    for font_id, video_id, generation_id, relative_path in font_rows:
        add_video_reference("video_font_asset", font_id, video_id, generation_id, relative_path)

    active_job_rows = (await db.execute(
        select(TranscodeJob.video_id, TranscodeJob.id).where(TranscodeJob.status.in_(
            [TranscodeJobStatus.QUEUED, TranscodeJobStatus.PROBING,
             TranscodeJobStatus.PROCESSING, TranscodeJobStatus.VERIFYING]
        ))
    )).all()
    active_upload_rows = (await db.execute(
        select(UploadSession.id).where(
            UploadSession.status.in_([UploadSessionStatus.INITIATED, UploadSessionStatus.UPLOADING, UploadSessionStatus.PAUSED]),
            UploadSession.expires_at > datetime.now(timezone.utc),
        )
    )).scalars().all()
    active_package_rows = (await db.execute(
        select(PackageUploadSession.video_id, PackageUploadSession.id).where(
            PackageUploadSession.status.in_([
                PackageSessionStatus.INITIALIZED.value, PackageSessionStatus.UPLOADING.value,
                PackageSessionStatus.UPLOADED.value, PackageSessionStatus.QUEUED_FOR_VERIFICATION.value,
                PackageSessionStatus.VERIFYING.value, PackageSessionStatus.PROMOTING.value,
            ]),
            PackageUploadSession.expires_at > datetime.now(timezone.utc),
        )
    )).all()

    active_transcodes = {(str(video_id), str(job_id)) for video_id, job_id in active_job_rows}
    active_packages = {(str(video_id), str(session_id)) for video_id, session_id in active_package_rows}
    managed, temporary_orphans = await asyncio.gather(
        asyncio.to_thread(scan_managed_storage, video_states),
        asyncio.to_thread(
            find_orphan_temporary_resources,
            active_transcodes=active_transcodes,
            active_upload_ids={str(upload_id) for upload_id in active_upload_rows},
            active_package_sessions=active_packages,
        ),
    )
    missing_managed_objects = await asyncio.to_thread(find_missing_managed_objects, managed_references)
    disk_root = Path(settings.STORAGE_BASE_PATH)
    try:
        total, used, free = shutil.disk_usage(str(disk_root))
        used_percent = round(used * 100 / total, 2) if total else 0.0
        filesystem = {"total_bytes": total, "used_bytes": used, "free_bytes": free, "used_percent": used_percent}
        filesystem_status = "critical" if (
            used_percent >= settings.STORAGE_HARD_THRESHOLD_PERCENT or free < settings.STORAGE_MIN_FREE_BYTES
        ) else "warning" if used_percent >= settings.STORAGE_SOFT_THRESHOLD_PERCENT else "healthy"
    except OSError:
        filesystem = {"total_bytes": None, "used_bytes": None, "free_bytes": None, "used_percent": None}
        filesystem_status = "unknown"
    return {
        **managed,
        "orphan_temporary_resources": temporary_orphans,
        "missing_managed_objects": missing_managed_objects,
        "filesystem": filesystem,
        "filesystem_status": filesystem_status,
        "working_space_safety_margin_bytes": settings.STORAGE_SAFETY_MARGIN_BYTES,
        "storage_cleanup_automatic": False,
    }
