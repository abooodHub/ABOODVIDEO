"""Admin Subtitle Management Controller."""
import uuid
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Header, Query, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, update, func

from app.core.database import get_db
from app.core.logging import get_logger
from app.models.user import User
from app.models.video import Video, VideoSubtitle
from app.security.auth import require_owner_or_admin, record_audit_log
from app.security.ownership import owner_scope, require_video_owner
from app.security.csrf import validate_csrf_header, check_csrf_protection
from app.services.subtitles import SubtitleService, SubtitleError

logger = get_logger("admin_subtitles")

router = APIRouter(prefix="/admin/videos/{video_id}/subtitles", tags=["Admin Subtitles"])


class SubtitleResponse(BaseModel):
    id: uuid.UUID
    video_id: uuid.UUID
    language_code: str
    language_name: str
    label: str
    format: str
    is_default: bool
    is_forced: bool
    is_playable: bool
    webvtt_path: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class UpdateSubtitleRequest(BaseModel):
    is_default: Optional[bool] = None
    is_forced: Optional[bool] = None
    label: Optional[str] = None


@router.get("", response_model=List[SubtitleResponse])
async def list_subtitles(
    video_id: uuid.UUID,
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all subtitle tracks uploaded for a video."""
    video = await db.get(Video, video_id)
    require_video_owner(video, current_user)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    stmt = select(VideoSubtitle).where(VideoSubtitle.video_id == video_id).order_by(VideoSubtitle.created_at.asc())
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("", response_model=SubtitleResponse, status_code=status.HTTP_201_CREATED)
async def upload_subtitle(
    video_id: uuid.UUID,
    request: Request,
    file: UploadFile = File(...),
    language_code: str = Form(...),
    language_name: str = Form(...),
    label: str = Form(...),
    is_default: bool = Form(False),
    is_forced: bool = Form(False),
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
    x_csrf_token: Optional[str] = Header(None, alias="X-CSRF-Token"),
):
    """Store the original subtitle, converting SRT to WebVTT for browser playback."""
    validate_csrf_header(request, x_csrf_token)

    video = await db.get(Video, video_id)
    require_video_owner(video, current_user)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    content_bytes = await file.read()
    subtitle_id = uuid.uuid4()

    try:
        source_path_rel, webvtt_path_rel, ext, is_playable = SubtitleService.store_subtitle(
            video_id=video_id,
            subtitle_id=subtitle_id,
            filename=file.filename or f"sub.{language_code}",
            content_bytes=content_bytes,
        )
    except SubtitleError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Enforce single default rule: if new is default, unset any existing default
    if is_default:
        await db.execute(
            update(VideoSubtitle)
            .where(VideoSubtitle.video_id == video_id)
            .values(is_default=False)
        )

    subtitle = VideoSubtitle(
        id=subtitle_id,
        video_id=video_id,
        language_code=language_code.strip().lower()[:16],
        language_name=language_name.strip()[:64],
        label=label.strip()[:100],
        format=ext,
        playback_renderer="libass" if ext in ("ass", "ssa") else "html5",
        source_path=source_path_rel,
        webvtt_path=webvtt_path_rel,
        is_default=is_default,
        is_forced=is_forced,
        is_playable=is_playable,
    )
    db.add(subtitle)
    await db.commit()
    await db.refresh(subtitle)

    await record_audit_log(
        db=db,
        action="video.subtitle_uploaded",
        user_id=current_user.id,
        target_entity="video_subtitle",
        target_id=str(subtitle.id),
        metadata_json={
            "video_id": str(video_id),
            "language_code": language_code,
            "format": ext,
            "is_playable": is_playable,
        },
    commit=True,
    )

    return subtitle


@router.delete("/{subtitle_id}")
async def delete_subtitle(
    video_id: uuid.UUID,
    subtitle_id: uuid.UUID,
    request: Request,
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
    x_csrf_token: Optional[str] = Header(None, alias="X-CSRF-Token"),
):
    """Safely delete subtitle track and corresponding disk files."""
    validate_csrf_header(request, x_csrf_token)
    require_video_owner(await db.get(Video, video_id), current_user)

    stmt = select(VideoSubtitle).where(
        and_(VideoSubtitle.id == subtitle_id, VideoSubtitle.video_id == video_id)
    )
    result = await db.execute(stmt)
    subtitle = result.scalar_one_or_none()
    if not subtitle:
        raise HTTPException(status_code=404, detail="Subtitle not found")

    # Delete disk files
    SubtitleService.delete_subtitle_files(video_id, subtitle_id)

    # Delete DB row
    await db.delete(subtitle)
    await db.commit()

    await record_audit_log(
        db=db,
        action="video.subtitle_deleted",
        user_id=current_user.id,
        target_entity="video_subtitle",
        target_id=str(subtitle_id),
        metadata_json={"video_id": str(video_id)},
    commit=True,
    )

    return {"message": "Subtitle deleted successfully"}


@router.patch("/{subtitle_id}", response_model=SubtitleResponse)
async def update_subtitle(
    video_id: uuid.UUID,
    subtitle_id: uuid.UUID,
    body: UpdateSubtitleRequest,
    request: Request,
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
    x_csrf_token: Optional[str] = Header(None, alias="X-CSRF-Token"),
):
    """Update subtitle settings (e.g. toggle default/forced, update label)."""
    validate_csrf_header(request, x_csrf_token)
    require_video_owner(await db.get(Video, video_id), current_user)

    stmt = select(VideoSubtitle).where(
        and_(VideoSubtitle.id == subtitle_id, VideoSubtitle.video_id == video_id)
    )
    result = await db.execute(stmt)
    subtitle = result.scalar_one_or_none()
    if not subtitle:
        raise HTTPException(status_code=404, detail="Subtitle not found")

    if body.is_default is not None:
        if body.is_default:
            # Unset any other defaults for this video
            await db.execute(
                update(VideoSubtitle)
                .where(VideoSubtitle.video_id == video_id)
                .values(is_default=False)
            )
        subtitle.is_default = body.is_default

    if body.is_forced is not None:
        subtitle.is_forced = body.is_forced

    if body.label is not None:
        subtitle.label = body.label.strip()[:100]

    await db.commit()
    await db.refresh(subtitle)
    return subtitle


# ---------------------------------------------------------------------------
# Global subtitles index (admin-wide subtitle management page)
# ---------------------------------------------------------------------------
from fastapi import Response as _Response  # local alias to avoid shadowing
from pydantic import BaseModel as _BaseModel, ConfigDict as _ConfigDict

admin_subtitles_router = APIRouter(
    prefix="/admin/subtitles",
    tags=["Admin Subtitles"],
    dependencies=[Depends(require_owner_or_admin), Depends(check_csrf_protection)],
)


class SubtitleIndexItem(_BaseModel):
    id: uuid.UUID
    video_id: uuid.UUID
    video_title: str
    language_code: str
    language_name: str
    label: str
    format: str
    is_default: bool
    is_forced: bool
    is_playable: bool
    created_at: str

    model_config = _ConfigDict(from_attributes=True)


@admin_subtitles_router.get("")
async def list_all_subtitles(
    response: _Response,
    q: Optional[str] = Query(None, max_length=100),
    language: Optional[str] = Query(None, max_length=16),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all subtitle tracks across the library with their video titles."""
    response.headers["Cache-Control"] = "no-store"
    query = select(VideoSubtitle, Video.title).join(Video, VideoSubtitle.video_id == Video.id)
    count_query = select(func.count(VideoSubtitle.id)).select_from(VideoSubtitle).join(
        Video, VideoSubtitle.video_id == Video.id
    )
    scope = owner_scope(current_user)
    if scope is not None:
        query = query.where(Video.user_id == scope)
        count_query = count_query.where(Video.user_id == scope)

    if q:
        like = f"%{q.strip()}%"
        cond = or_(Video.title.ilike(like), VideoSubtitle.label.ilike(like), VideoSubtitle.language_name.ilike(like))
        query = query.where(cond)
        count_query = count_query.where(cond)
    if language:
        query = query.where(VideoSubtitle.language_code == language.strip().lower())
        count_query = count_query.where(VideoSubtitle.language_code == language.strip().lower())

    query = query.order_by(VideoSubtitle.created_at.desc())
    total = (await db.execute(count_query)).scalar() or 0
    offset = (page - 1) * limit
    rows = (await db.execute(query.offset(offset).limit(limit))).all()

    return {
        "items": [
            SubtitleIndexItem(
                id=s.id, video_id=s.video_id, video_title=title,
                language_code=s.language_code, language_name=s.language_name,
                label=s.label, format=s.format, is_default=s.is_default,
                is_forced=s.is_forced, is_playable=s.is_playable,
                created_at=s.created_at.isoformat() if s.created_at else "",
            )
            for s, title in rows
        ],
        "total": total, "page": page, "limit": limit,
    }
