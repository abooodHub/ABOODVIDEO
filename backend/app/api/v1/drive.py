"""Video Drive and File Manager API Endpoints."""
import os
import uuid
import mimetypes
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Literal
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, and_, or_, asc, cast, case, desc, exists, func, String
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis
from app.security.hashing import hash_password
from app.security.share_access import require_share_access, unlock_share
from app.services.download_names import build_download_filename
from app.models.user import User, UserRole
from app.models.video import Video
from app.models.series import Series, Season
from app.models.folder import Folder
from app.models.share import Share
from app.security.auth import require_owner_or_admin
from app.security.csrf import check_csrf_protection
from app.services.drive_service import DriveService
from app.core.logging import get_logger

logger = get_logger("drive_api")

router = APIRouter(
    prefix="/drive",
    tags=["Video Drive & File Manager"],
    dependencies=[Depends(check_csrf_protection)],
)


async def get_current_drive_manager(
    current_user: User = Depends(require_owner_or_admin),
) -> User:
    """Require a real active OWNER or ADMIN session for every Drive endpoint."""
    return current_user


def drive_owner_scope(current_user: User) -> uuid.UUID | None:
    """Admins retain their existing cross-owner Drive management scope."""
    return None if current_user.role == UserRole.ADMIN else current_user.id


# ==========================================
# SCHEMAS
# ==========================================
class CreateFolderPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    parent_id: Optional[str] = None
    folder_type: str = "folder"


class CreateSeriesPayload(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    parent_folder_id: Optional[str] = None
    description: Optional[str] = None
    year: Optional[int] = None


class CreateSeasonPayload(BaseModel):
    series_id: str
    season_number: int = Field(..., ge=1)
    display_name: Optional[str] = None


class RenamePayload(BaseModel):
    type: Literal["video", "folder", "series", "season"]
    id: str
    name: str = Field(..., min_length=1, max_length=255)


class RenameFolderPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class RenameVideoPayload(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)


class DownloadPolicyPayload(BaseModel):
    type: Literal["video", "folder", "series", "season"]
    id: str
    policy: Literal["INHERIT", "ALLOW", "DENY"]


class MoveItemSpec(BaseModel):
    id: str
    type: Literal["video", "folder", "series", "season"]


class MoveItemsPayload(BaseModel):
    items: List[MoveItemSpec]
    target_id: Optional[str] = None
    target_type: Literal["root", "folder", "series", "season"] = "folder"


class CreateSharePayload(BaseModel):
    target_type: Literal["folder", "series", "season", "video"]
    target_id: str
    allow_download: bool = False
    expires_days: Optional[int] = Field(None, ge=1, le=3650)
    password: Optional[str] = Field(None, min_length=1, max_length=1024)


class SharePasswordPayload(BaseModel):
    password: str = Field(..., min_length=1, max_length=1024)


class DetectMediaPayload(BaseModel):
    video_ids: Optional[List[str]] = None
    scope_id: Optional[str] = None


class MediaAssignmentItem(BaseModel):
    video_id: str
    series_name: Optional[str] = None
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    suggested_title: Optional[str] = None


class ApplyMediaDetectionPayload(BaseModel):
    assignments: List[MediaAssignmentItem]
    parent_folder_id: Optional[str] = None


class TrashItemSpec(BaseModel):
    id: str
    type: str = "video"


class TrashItemsPayload(BaseModel):
    items: List[TrashItemSpec]


class RestoreItemsPayload(BaseModel):
    items: List[TrashItemSpec]


# ==========================================
# ENDPOINTS
# ==========================================
@router.get("/tree")
async def get_drive_tree(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_drive_manager),
):
    """Retrieve hierarchical tree (folders, series, seasons)."""
    return await DriveService.get_folder_tree(db, drive_owner_scope(current_user))


@router.get("/scope")
@router.get("/folder")
@router.get("/folder/{folder_id}")
async def get_scope_contents(
    folder_id: Optional[str] = None,
    id: Optional[str] = None,
    type: Optional[str] = None,
    filter: Optional[str] = Query(None, description="Content filter: content, uncategorized, test, all, trash"),
    content_filter: Optional[str] = Query(None, description="Alias for filter"),
    q: Optional[str] = Query(None, description="Search query filter"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    trash_type: Optional[str] = Query(None, pattern='^(folder|series|season|video)$'),
    trash_sort: str = Query('trashed_at', pattern='^(trashed_at|name|type)$'),
    trash_direction: str = Query('desc', pattern='^(asc|desc)$'),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_drive_manager),
):
    """
    Retrieve contents (folders, series, seasons, videos) and breadcrumbs
    for any scope (Root, Uncategorized, Folder, Series, Season, or Trash).
    """
    resolved_id = id or folder_id
    effective_filter = content_filter or filter or "content"
    try:
        return await DriveService.get_scope_contents(
            db=db,
            user_id=drive_owner_scope(current_user),
            scope_id=resolved_id,
            scope_type=type,
            search_query=q,
            content_filter=effective_filter,
            page=page,
            limit=limit,
            trash_type=trash_type,
            trash_sort=trash_sort,
            trash_direction=trash_direction,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/folders", status_code=status.HTTP_201_CREATED)
async def create_folder(
    payload: CreateFolderPayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_drive_manager),
):
    """Create a new logical folder."""
    parent_uuid: Optional[uuid.UUID] = None
    if payload.parent_id and payload.parent_id not in ("root", "uncategorized", ""):
        try:
            parent_uuid = uuid.UUID(payload.parent_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid parent folder ID")

    try:
        folder = await DriveService.create_folder(
            db=db,
            user_id=current_user.id,
            name=payload.name,
            parent_id=parent_uuid,
            folder_type=payload.folder_type,
            owner_id=drive_owner_scope(current_user),
        )
        return {
            "success": True,
            "folder": {
                "id": str(folder.id),
                "name": folder.name,
                "parent_id": str(folder.parent_id) if folder.parent_id else None,
                "folder_type": folder.folder_type,
            },
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/series", status_code=status.HTTP_201_CREATED)
async def create_series(
    payload: CreateSeriesPayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_drive_manager),
):
    """Create a new Series entity."""
    parent_uuid: Optional[uuid.UUID] = None
    if payload.parent_folder_id and payload.parent_folder_id not in ("root", "uncategorized", ""):
        try:
            parent_uuid = uuid.UUID(payload.parent_folder_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid parent folder ID")

    try:
        series = await DriveService.create_series(
            db=db,
            user_id=current_user.id,
            title=payload.title,
            parent_folder_id=parent_uuid,
            description=payload.description,
            year=payload.year,
            owner_id=drive_owner_scope(current_user),
        )
        return {
            "success": True,
            "series": {
                "id": str(series.id),
                "title": series.title,
                "parent_folder_id": str(series.parent_folder_id) if series.parent_folder_id else None,
            },
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/seasons", status_code=status.HTTP_201_CREATED)
async def create_season(
    payload: CreateSeasonPayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_drive_manager),
):
    """Create a new Season entity under a Series."""
    try:
        series_uuid = uuid.UUID(payload.series_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid series ID")

    try:
        season = await DriveService.create_season(
            db=db,
            series_id=series_uuid,
            season_number=payload.season_number,
            display_name=payload.display_name,
            owner_id=drive_owner_scope(current_user),
        )
        return {
            "success": True,
            "season": {
                "id": str(season.id),
                "series_id": str(season.series_id),
                "season_number": season.season_number,
                "display_name": season.display_name,
            },
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/rename")
async def rename_entity(
    payload: RenamePayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_drive_manager),
):
    """Polymorphic rename for folder, series, season, or video."""
    try:
        entity_uuid = uuid.UUID(payload.id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid entity ID")

    try:
        res = await DriveService.rename_entity(db, payload.type, entity_uuid, payload.name, drive_owner_scope(current_user))
        return {"success": True, **res}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/folders/{folder_id}/rename")
async def rename_folder_compat(
    folder_id: uuid.UUID,
    payload: RenameFolderPayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_drive_manager),
):
    """Backwards compatible endpoint to rename folder."""
    try:
        res = await DriveService.rename_entity(db, "folder", folder_id, payload.name, drive_owner_scope(current_user))
        return {"success": True, "id": res["id"], "name": res["name"]}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/videos/{video_id}/rename")
async def rename_video_compat(
    video_id: uuid.UUID,
    payload: RenameVideoPayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_drive_manager),
):
    """Backwards compatible endpoint to rename video."""
    try:
        res = await DriveService.rename_entity(db, "video", video_id, payload.title, drive_owner_scope(current_user))
        return {"success": True, "id": res["id"], "title": res["title"]}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/download-policy")
async def update_download_policy(
    payload: DownloadPolicyPayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_drive_manager),
):
    """Update allow_download_policy ('INHERIT', 'ALLOW', 'DENY')."""
    try:
        entity_uuid = uuid.UUID(payload.id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid entity ID")

    try:
        res = await DriveService.update_download_policy(db, payload.type, entity_uuid, payload.policy, drive_owner_scope(current_user))
        return {"success": True, **res}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/move")
async def move_items(
    payload: MoveItemsPayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_drive_manager),
):
    """
    Move multiple videos, folders, series, or seasons with cycle/type safety.
    Returns previous states to allow frontend instant Undo.
    """
    target_uuid: Optional[uuid.UUID] = None
    if payload.target_id and payload.target_id not in ("root", "uncategorized", ""):
        try:
            target_uuid = uuid.UUID(payload.target_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid target ID")

    try:
        raw_items = [{"id": item.id, "type": item.type} for item in payload.items]
        moved_count, previous_states = await DriveService.move_items(
            db, raw_items, target_uuid, target_type=payload.target_type, owner_id=drive_owner_scope(current_user)
        )
        return {
            "success": True,
            "moved_count": moved_count,
            "previous_states": previous_states,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/folders/{folder_id}")
async def delete_folder(
    folder_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_drive_manager),
):
    """Legacy delete alias: normal folder deletion always means Trash."""
    try:
        count, summary = await DriveService.trash_items(
            db, [{"id": str(folder_id), "type": "folder"}], drive_owner_scope(current_user)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "count": count, "summary": summary, "message": "Folder moved to Trash"}


# ==========================================
# TRASH & SOFT-DELETE ENDPOINTS
# ==========================================
@router.post("/trash")
async def trash_items(
    payload: TrashItemsPayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_drive_manager),
):
    """Soft-delete items by setting trashed_at timestamp."""
    raw_items = [{"id": item.id, "type": item.type} for item in payload.items]
    try:
        count, summary = await DriveService.trash_items(db, raw_items, drive_owner_scope(current_user))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "count": count, "summary": summary}


@router.post("/restore")
async def restore_items(
    payload: RestoreItemsPayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_drive_manager),
):
    """Restore soft-deleted items back to their original hierarchy."""
    raw_items = [{"id": item.id, "type": item.type} for item in payload.items]
    try:
        count = await DriveService.restore_items(db, raw_items, drive_owner_scope(current_user))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "count": count}


@router.post("/trash/delete-permanent")
async def permanent_delete_items(
    payload: TrashItemsPayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_drive_manager),
):
    """Permanently delete records from the DB. Physical media files remain untouched."""
    raw_items = [{"id": item.id, "type": item.type} for item in payload.items]
    try:
        count = await DriveService.permanent_delete_items(db, raw_items, drive_owner_scope(current_user))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "count": count}


@router.get("/descendant-summary/{target_type}/{target_id}")
async def get_descendant_summary(
    target_type: str,
    target_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_drive_manager),
):
    """Get count of descendant series, seasons, and videos for safe deletion dialog."""
    try:
        summary = await DriveService.get_descendant_summary(db, target_type, target_id, drive_owner_scope(current_user))
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"success": True, "summary": summary}


# ==========================================
# MEDIA AUTO-DETECTION & BATCH IMPORT
# ==========================================
@router.post("/detect-media")
async def detect_media_candidates(
    payload: DetectMediaPayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_drive_manager),
):
    """Run media detection on specified videos or current scope."""
    uuids = None
    if payload.video_ids:
        uuids = [uuid.UUID(x) for x in payload.video_ids if x]
    results = await DriveService.detect_media(db, video_ids=uuids, scope_id=payload.scope_id, owner_id=drive_owner_scope(current_user))
    return {"success": True, "results": results}


@router.post("/apply-media-detection")
async def apply_media_detection(
    payload: ApplyMediaDetectionPayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_drive_manager),
):
    """Apply approved auto-detection assignments."""
    parent_uuid = None
    if payload.parent_folder_id and payload.parent_folder_id not in ("root", "uncategorized", ""):
        try:
            parent_uuid = uuid.UUID(payload.parent_folder_id)
        except ValueError:
            pass

    assignments = [item.model_dump() for item in payload.assignments]
    res = await DriveService.apply_media_detection(db, current_user.id, assignments, parent_uuid, is_admin=current_user.role == UserRole.ADMIN)
    return res


# ==========================================
# SHARING ENDPOINTS
# ==========================================
@router.post("/shares")
async def create_share(
    payload: CreateSharePayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_drive_manager),
):
    """Create or update share link for folder, series, season, or video."""
    try:
        target_uuid = uuid.UUID(payload.target_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid target ID")

    try:
        share = await DriveService.create_share(
            db=db,
            owner_id=current_user.id,
            target_type=payload.target_type,
            target_id=target_uuid,
            allow_download=payload.allow_download,
            expires_days=payload.expires_days,
            password_hash=hash_password(payload.password) if payload.password else None,
            update_password="password" in payload.model_fields_set,
        )
        return {
            "success": True,
            "token": share.token,
            "share_url": f"/preview/share/{share.token}",
            "allow_download": share.allow_download,
            "expires_at": share.expires_at.isoformat() if share.expires_at else None,
            "password_required": bool(share.password_hash),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/shares/target/{target_type}/{target_id}")
async def get_active_share_for_target(
    target_type: str,
    target_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_drive_manager),
):
    """Check if an active share already exists for the given entity."""
    stmt = select(Share).where(
        Share.owner_id == current_user.id,
        Share.target_type == target_type,
        Share.target_id == target_id,
        Share.revoked_at.is_(None),
        or_(Share.expires_at.is_(None), Share.expires_at > datetime.now(timezone.utc)),
    ).order_by(Share.created_at.desc(), Share.id.desc()).limit(1)
    res = await db.execute(stmt)
    share = res.scalar_one_or_none()
    if share and share.is_active:
        return {
            "exists": True,
            "token": share.token,
            "share_url": f"/preview/share/{share.token}",
            "allow_download": share.allow_download,
            "password_required": bool(share.password_hash),
            "expires_at": share.expires_at.isoformat() if share.expires_at else None,
        }
    return {"exists": False}


@router.get("/shares")
async def list_user_shares(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_drive_manager),
    status_filter: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    q: Optional[str] = Query(None, max_length=100),
    target_type: Optional[str] = Query(None, pattern="^(video|folder|series|season)$"),
    sort: str = Query("created_at", pattern="^(created_at|expires_at|target_type|status)$"),
    direction: str = Query("desc", pattern="^(asc|desc)$"),
) -> dict:
    """List a bounded page of share links created by the current authenticated user."""
    from datetime import datetime, timezone
    from sqlalchemy import asc, desc

    now = datetime.now(timezone.utc)
    computed_status = case(
        (Share.revoked_at.is_not(None), "revoked"),
        (and_(Share.expires_at.is_not(None), Share.expires_at < now), "expired"),
        else_="active",
    )
    stmt = select(Share).where(Share.owner_id == current_user.id)
    if status_filter:
        if status_filter not in {"active", "expired", "revoked"}:
            raise HTTPException(status_code=422, detail="Invalid share status")
        if status_filter == "revoked":
            stmt = stmt.where(Share.revoked_at.is_not(None))
        elif status_filter == "expired":
            stmt = stmt.where(Share.revoked_at.is_(None), Share.expires_at.is_not(None), Share.expires_at < now)
        else:
            stmt = stmt.where(Share.revoked_at.is_(None), or_(Share.expires_at.is_(None), Share.expires_at >= now))
    if target_type:
        stmt = stmt.where(Share.target_type == target_type)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(or_(
            Share.token.ilike(pattern),
            Share.target_type.ilike(pattern),
            cast(Share.target_id, String).ilike(pattern),
            exists(select(1).where(Video.id == Share.target_id, Share.target_type == "video", Video.title.ilike(pattern))),
            exists(select(1).where(Folder.id == Share.target_id, Share.target_type == "folder", Folder.name.ilike(pattern))),
            exists(select(1).where(Series.id == Share.target_id, Share.target_type == "series", Series.title.ilike(pattern))),
            exists(select(1).where(Season.id == Share.target_id, Share.target_type == "season", Season.display_name.ilike(pattern))),
        ))

    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    sort_columns = {
        "created_at": Share.created_at,
        "expires_at": Share.expires_at,
        "target_type": Share.target_type,
        "status": computed_status,
    }
    ordering = asc if direction == "asc" else desc
    stmt = stmt.order_by(ordering(sort_columns[sort]).nulls_last(), ordering(Share.id))
    shares = list((await db.execute(stmt.offset((page - 1) * limit).limit(limit))).scalars().all())

    # Pre-fetch target names in bulk per target_type to avoid N+1 queries
    video_ids = {s.target_id for s in shares if s.target_type == "video"}
    folder_ids = {s.target_id for s in shares if s.target_type == "folder"}
    series_ids = {s.target_id for s in shares if s.target_type == "series"}
    season_ids = {s.target_id for s in shares if s.target_type == "season"}

    video_names: dict = {}
    folder_names: dict = {}
    series_names: dict = {}
    season_names: dict = {}

    if video_ids:
        rows = await db.execute(select(Video.id, Video.title).where(Video.id.in_(video_ids)))
        video_names = {r.id: r.title for r in rows}

    if folder_ids:
        rows = await db.execute(select(Folder.id, Folder.name).where(Folder.id.in_(folder_ids)))
        folder_names = {r.id: r.name for r in rows}

    if series_ids:
        rows = await db.execute(select(Series.id, Series.title).where(Series.id.in_(series_ids)))
        series_names = {r.id: r.title for r in rows}

    if season_ids:
        rows = await db.execute(
            select(Season.id, Season.display_name, Season.season_number).where(Season.id.in_(season_ids))
        )
        season_names = {
            r.id: r.display_name or f"الموسم {r.season_number}"
            for r in rows
        }

    def _resolve_name(share: Share) -> Optional[str]:
        if share.target_type == "video":
            return video_names.get(share.target_id)
        if share.target_type == "folder":
            return folder_names.get(share.target_id)
        if share.target_type == "series":
            return series_names.get(share.target_id)
        if share.target_type == "season":
            return season_names.get(share.target_id)
        return None

    def _compute_status(share: Share) -> str:
        if share.revoked_at is not None:
            return "revoked"
        if share.expires_at is not None:
            now = datetime.now(timezone.utc)
            exp = share.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < now:
                return "expired"
        return "active"

    output = []
    for share in shares:
        computed_status = _compute_status(share)
        output.append({
            "id": str(share.id),
            "token": share.token,
            "target_type": share.target_type,
            "target_id": str(share.target_id),
            "target_name": _resolve_name(share),
            "created_at": share.created_at.isoformat() if share.created_at else None,
            "expires_at": share.expires_at.isoformat() if share.expires_at else None,
            "revoked_at": share.revoked_at.isoformat() if share.revoked_at else None,
            "allow_download": share.allow_download,
            "status": computed_status,
            "password_required": bool(share.password_hash),
            "share_url": f"/preview/share/{share.token}",
        })

    counts_stmt = select(
        func.count().filter(Share.revoked_at.is_(None), or_(Share.expires_at.is_(None), Share.expires_at >= now)).label("active"),
        func.count().filter(Share.revoked_at.is_(None), Share.expires_at.is_not(None), Share.expires_at < now).label("expired"),
        func.count().filter(Share.revoked_at.is_not(None)).label("revoked"),
    ).where(Share.owner_id == current_user.id)
    counts = (await db.execute(counts_stmt)).one()
    return {
        "shares": output,
        "items": output,
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit,
        "status_counts": {"active": counts.active, "expired": counts.expired, "revoked": counts.revoked},
    }


@router.delete("/shares/{identifier}")
@router.patch("/shares/{identifier}/revoke")
async def revoke_share(
    identifier: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_drive_manager),
):
    """Revoke a share link by token or UUID."""
    import uuid
    from datetime import datetime, timezone

    stmt = select(Share)
    try:
        val_uuid = uuid.UUID(identifier)
        stmt = stmt.where(or_(Share.token == identifier, Share.id == val_uuid))
    except ValueError:
        stmt = stmt.where(Share.token == identifier)

    owner_scope = drive_owner_scope(current_user)
    if owner_scope is not None:
        stmt = stmt.where(Share.owner_id == owner_scope)

    res = await db.execute(stmt)
    share = res.scalar_one_or_none()
    if not share:
        raise HTTPException(status_code=404, detail="الرابط غير موجود")

    share.revoked_at = datetime.now(timezone.utc)
    await db.commit()
    return {"success": True, "message": "تم تعطيل الرابط بنجاح"}


@router.get("/shares/{token}/info")
async def get_share_public_info(
    token: str,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis_conn=Depends(get_redis),
):
    """Public info endpoint for anonymous shared viewer."""
    share = await DriveService.get_share_by_token(db, token)
    if not share:
        raise HTTPException(status_code=404, detail="الرابط غير صالح أو منتهي الصلاحية")

    await require_share_access(request, share, redis_conn)
    response.headers["Cache-Control"] = "no-store"
    try:
        data = await DriveService.get_shared_scope_contents(db, share)
        return {"success": True, "share": data}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/shares/{token}/verify-password")
async def verify_share_password(
    token: str, payload: SharePasswordPayload, request: Request, response: Response,
    db: AsyncSession = Depends(get_db), redis_conn=Depends(get_redis),
):
    share = await DriveService.get_share_by_token(db, token)
    if not share:
        raise HTTPException(status_code=404, detail="Share unavailable")
    await unlock_share(request, response, share, payload.password, redis_conn)
    return {"success": True}


# ==========================================
# SECURE DOWNLOAD ENDPOINTS
# ==========================================
@router.get("/download/{video_id}")
async def download_video_owner(
    video_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_drive_manager),
):
    """Download video with inherited policy enforcement for owner/preview."""
    video = await db.get(Video, video_id)
    owner_scope = drive_owner_scope(current_user)
    if not video or video.trashed_at is not None or (owner_scope is not None and video.user_id != owner_scope):
        raise HTTPException(status_code=404, detail="الفيديو غير موجود")

    # Verify download permission
    can_download = await DriveService.get_effective_download_policy(db, video)
    if not can_download:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="التنزيل معطل لهذا الفيديو وفقاً لسياسة التنزيل المحددة",
        )

    file_path = video.source_path
    if not file_path or not os.path.exists(file_path):
        # Fallback check
        raise HTTPException(status_code=404, detail="ملف الفيديو غير متوفر على القرص")

    safe_filename = build_download_filename(video)
    return FileResponse(
        path=file_path,
        media_type="application/octet-stream",
        filename=safe_filename,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/shares/{token}/download/{video_id}")
async def download_shared_video(
    token: str,
    video_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis_conn=Depends(get_redis),
):
    """Download video from an anonymous shared link with strict validation."""
    share = await DriveService.get_share_by_token(db, token)
    if not share:
        raise HTTPException(status_code=404, detail="الرابط غير صالح أو منتهي الصلاحية")

    await require_share_access(request, share, redis_conn)
    if not share.allow_download:
        raise HTTPException(status_code=403, detail="التنزيل غير مسموح به عبر هذا الرابط")

    video = await DriveService.get_shared_video_if_available(db, share, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="الفيديو غير موجود")

    # Verify inherited download policy on the video itself
    can_download = await DriveService.get_effective_download_policy(db, video)
    if not can_download:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="التنزيل معطل لهذا الفيديو وفقاً لسياسة التنزيل",
        )

    file_path = video.source_path
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="ملف الفيديو غير متوفر على القرص")

    safe_filename = build_download_filename(video)
    return FileResponse(
        path=file_path,
        media_type="application/octet-stream",
        filename=safe_filename,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
