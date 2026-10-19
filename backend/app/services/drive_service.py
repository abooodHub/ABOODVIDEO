"""Drive and File Manager Business Logic Service.

Separates logical organization metadata from immutable media storage.
Moving, renaming, and nesting operations do NOT touch physical HLS files.
Supports Folder, Series, Season, and Video polymorphic entities and download policies.
"""
import uuid
import secrets
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func, and_, or_, desc, asc, update, delete, literal, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.folder import Folder
from app.models.series import Series, Season
from app.models.share import Share
from app.models.video import (
    Video,
    VideoStatus,
    VideoVisibility,
    VideoRendition,
    VideoSubtitle,
    VideoFontAsset,
    TranscodeJob,
    TranscodeJobStatus,
)
from app.core.logging import get_logger

logger = get_logger("drive_service")

def get_test_video_condition():
    return Video.is_test_content.is_(True)


def format_bytes(size_bytes: Optional[int]) -> str:
    """Format bytes into human-readable string (GB, MB, KB)."""
    if not size_bytes or size_bytes <= 0:
        return "0 MB"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size_bytes)
    unit_index = 0
    while size >= 1024.0 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1
    if unit_index >= 3:  # GB or TB
        return f"{size:.1f} {units[unit_index]}"
    return f"{int(size)} {units[unit_index]}"


def format_duration(seconds: Optional[float]) -> str:
    """Format duration into HH:MM:SS or MM:SS."""
    if not seconds or seconds <= 0:
        return "—"
    total_sec = int(seconds)
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    secs = total_sec % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def get_video_quality(video: Video) -> str:
    """Determine highest quality label for a video."""
    if video.renditions:
        for r in video.renditions:
            if r.quality_label in ("2160p", "4K") or (r.resolution_height and r.resolution_height >= 2160):
                return "4K"
        for r in video.renditions:
            if r.quality_label == "1440p":
                return "1440p"
        for r in video.renditions:
            if r.quality_label == "1080p":
                return "1080p"
        for r in video.renditions:
            if r.quality_label == "720p":
                return "720p"
    if video.source_resolution_height:
        if video.source_resolution_height >= 2160:
            return "4K"
        if video.source_resolution_height >= 1440:
            return "1440p"
        if video.source_resolution_height >= 1080:
            return "1080p"
        if video.source_resolution_height >= 720:
            return "720p"
        return f"{video.source_resolution_height}p"
    return "HD"


def get_video_subtitles_info(video: Video) -> Tuple[str, str, int]:
    """Return (label, detail, font_count) for video subtitles."""
    if not video.subtitles:
        return "بدون", "بدون ترجمة", 0
    has_ass = False
    langs = []
    for s in video.subtitles:
        if (s.format or "").lower() in ("ass", "ssa") or s.playback_renderer == "libass":
            has_ass = True
        lang = s.language_name or s.label or "ترجمة"
        if lang not in langs:
            langs.append(lang)

    font_count = len(video.font_assets) if video.font_assets else 0
    if has_ass:
        detail = f"{', '.join(langs)} — ASS"
        if font_count > 0:
            detail += f" ({font_count} خطوط)"
        return "ASS", detail, font_count
    return "VTT", f"{', '.join(langs)} — VTT", 0


class DriveService:
    @staticmethod
    async def get_effective_download_policy(db: AsyncSession, video: Video) -> bool:
        """
        Evaluate recursive download policy:
        Video policy -> Season policy -> Series policy -> Folder hierarchy -> Default ALLOW (True).
        Returns True if download allowed, False if denied.
        """
        # 1. Video explicit policy
        if video.allow_download_policy == "ALLOW":
            return True
        if video.allow_download_policy == "DENY":
            return False

        # 2. Season policy
        series_id = video.series_id
        if video.season_id:
            season = await db.get(Season, video.season_id)
            if season:
                if season.allow_download_policy == "ALLOW":
                    return True
                if season.allow_download_policy == "DENY":
                    return False
                if not series_id:
                    series_id = season.series_id

        # 3. Series policy
        curr_folder_id = video.folder_id
        if series_id:
            series = await db.get(Series, series_id)
            if series:
                if series.allow_download_policy == "ALLOW":
                    return True
                if series.allow_download_policy == "DENY":
                    return False
                if series.parent_folder_id:
                    curr_folder_id = series.parent_folder_id

        # 4. Folder hierarchy
        visited = set()
        while curr_folder_id and curr_folder_id not in visited:
            visited.add(curr_folder_id)
            folder = await db.get(Folder, curr_folder_id)
            if not folder:
                break
            if folder.allow_download_policy == "ALLOW":
                return True
            if folder.allow_download_policy == "DENY":
                return False
            curr_folder_id = folder.parent_id

        # Default fallback is ALLOW
        return True

    @staticmethod
    async def get_folder_tree(db: AsyncSession, user_id: Optional[uuid.UUID] = None) -> Dict[str, Any]:
        """Build hierarchical folder, series, and season tree for move destination picker (excludes trashed)."""
        stmt = select(Folder).where(Folder.trashed_at.is_(None)).order_by(Folder.name.asc())
        if user_id:
            stmt = stmt.where(Folder.user_id == user_id)
        result = await db.execute(stmt)
        folders = result.scalars().all()

        # Query all series with seasons (non-trashed)
        series_stmt = (
            select(Series)
            .options(selectinload(Series.seasons))
            .where(Series.trashed_at.is_(None))
            .order_by(Series.title.asc())
        )
        if user_id:
            series_stmt = series_stmt.where(Series.owner_id == user_id)
        series_res = await db.execute(series_stmt)
        series_list = series_res.scalars().all()

        # Query uncategorized real videos count
        test_cond = get_test_video_condition()
        uncat_stmt = select(func.count(Video.id)).where(
            and_(
                Video.folder_id.is_(None),
                Video.trashed_at.is_(None),
                ~test_cond,
            )
        )
        if user_id:
            uncat_stmt = uncat_stmt.where(Video.user_id == user_id)
        uncat_res = await db.execute(uncat_stmt)
        uncategorized_count = uncat_res.scalar() or 0

        # Query trash count
        trash_v_stmt = select(func.count(Video.id)).where(Video.trashed_at.is_not(None))
        trash_f_stmt = select(func.count(Folder.id)).where(Folder.trashed_at.is_not(None))
        trash_s_stmt = select(func.count(Series.id)).where(Series.trashed_at.is_not(None))
        if user_id:
            trash_v_stmt = trash_v_stmt.where(Video.user_id == user_id)
            trash_f_stmt = trash_f_stmt.where(Folder.user_id == user_id)
            trash_s_stmt = trash_s_stmt.where(Series.owner_id == user_id)
        trash_v_count = (await db.execute(trash_v_stmt)).scalar() or 0
        trash_f_count = (await db.execute(trash_f_stmt)).scalar() or 0
        trash_s_count = (await db.execute(trash_s_stmt)).scalar() or 0
        total_trash_count = trash_v_count + trash_f_count + trash_s_count

        # Build folder map
        folder_dict: Dict[uuid.UUID, Dict[str, Any]] = {}
        for f in folders:
            folder_dict[f.id] = {
                "id": str(f.id),
                "type": "folder",
                "name": f.name,
                "parent_id": str(f.parent_id) if f.parent_id else None,
                "allow_download_policy": f.allow_download_policy,
                "children": [],
            }

        # Nest folders into tree
        root_nodes: List[Dict[str, Any]] = []
        for f in folders:
            item = folder_dict[f.id]
            if f.parent_id and f.parent_id in folder_dict:
                folder_dict[f.parent_id]["children"].append(item)
            else:
                root_nodes.append(item)

        return {
            "tree": root_nodes,
            "uncategorized_count": uncategorized_count,
            "trash_count": total_trash_count,
            "total_folders": len(folders),
            "total_series": len(series_list),
        }

    @staticmethod
    async def get_breadcrumbs(
        db: AsyncSession,
        scope_type: str,
        scope_id: Optional[uuid.UUID],
        user_id: Optional[uuid.UUID] = None,
    ) -> List[Dict[str, Any]]:
        """Compute complete breadcrumb path from root to current entity with sibling locations."""
        crumbs = [{"id": "root", "type": "root", "name": "مكتبة المحتوى", "siblings": []}]
        if not scope_id or scope_type == "root":
            # Add top-level folder siblings to root crumb
            root_f_stmt = select(Folder).where(and_(Folder.parent_id.is_(None), Folder.trashed_at.is_(None)))
            if user_id:
                root_f_stmt = root_f_stmt.where(Folder.user_id == user_id)
            root_f_res = await db.execute(root_f_stmt.order_by(Folder.name.asc()))
            crumbs[0]["siblings"] = [
                {"id": str(f.id), "name": f.name, "type": "folder"} for f in root_f_res.scalars().all()
            ]
            return crumbs

        if scope_type == "trash":
            return crumbs + [{"id": "trash", "type": "trash", "name": "سلة المحذوفات", "siblings": []}]

        if scope_type == "uncategorized":
            return crumbs + [{"id": "uncategorized", "type": "uncategorized", "name": "غير مصنف", "siblings": []}]

        if scope_type == "test":
            return crumbs + [{"id": "test", "type": "test", "name": "بيانات الاختبار", "siblings": []}]

        path: List[Dict[str, Any]] = []

        if scope_type == "season":
            season = await db.get(Season, scope_id)
            if season and season.trashed_at is None:
                # Sibling seasons
                sib_seasons_res = await db.execute(
                    select(Season)
                    .where(and_(Season.series_id == season.series_id, Season.trashed_at.is_(None)))
                    .order_by(Season.season_number.asc())
                )
                sib_seasons = [
                    {"id": str(s.id), "name": s.display_name, "type": "season"}
                    for s in sib_seasons_res.scalars().all()
                ]
                path.append({"id": str(season.id), "type": "season", "name": season.display_name, "siblings": sib_seasons})

                series = await db.get(Series, season.series_id)
                if series and series.trashed_at is None:
                    # Sibling series
                    sib_series_stmt = select(Series).where(and_(Series.trashed_at.is_(None)))
                    if user_id:
                        sib_series_stmt = sib_series_stmt.where(Series.owner_id == user_id)
                    if series.parent_folder_id:
                        sib_series_stmt = sib_series_stmt.where(Series.parent_folder_id == series.parent_folder_id)
                    else:
                        sib_series_stmt = sib_series_stmt.where(Series.parent_folder_id.is_(None))
                    sib_series_res = await db.execute(sib_series_stmt.order_by(Series.title.asc()))
                    sib_series = [
                        {"id": str(s.id), "name": s.title, "type": "series"}
                        for s in sib_series_res.scalars().all()
                    ]
                    path.append({"id": str(series.id), "type": "series", "name": series.title, "siblings": sib_series})

                    curr_folder_id = series.parent_folder_id
                    while curr_folder_id:
                        f = await db.get(Folder, curr_folder_id)
                        if not f or f.trashed_at is not None or (user_id and f.user_id != user_id):
                            break
                        sib_f_stmt = select(Folder).where(and_(Folder.trashed_at.is_(None)))
                        if user_id:
                            sib_f_stmt = sib_f_stmt.where(Folder.user_id == user_id)
                        if f.parent_id:
                            sib_f_stmt = sib_f_stmt.where(Folder.parent_id == f.parent_id)
                        else:
                            sib_f_stmt = sib_f_stmt.where(Folder.parent_id.is_(None))
                        sib_f_res = await db.execute(sib_f_stmt.order_by(Folder.name.asc()))
                        sib_f = [
                            {"id": str(sf.id), "name": sf.name, "type": "folder"}
                            for sf in sib_f_res.scalars().all()
                        ]
                        path.append({"id": str(f.id), "type": "folder", "name": f.name, "siblings": sib_f})
                        curr_folder_id = f.parent_id

        elif scope_type == "series":
            series = await db.get(Series, scope_id)
            if series and series.trashed_at is None:
                sib_series_stmt = select(Series).where(and_(Series.trashed_at.is_(None)))
                if user_id:
                    sib_series_stmt = sib_series_stmt.where(Series.owner_id == user_id)
                if series.parent_folder_id:
                    sib_series_stmt = sib_series_stmt.where(Series.parent_folder_id == series.parent_folder_id)
                else:
                    sib_series_stmt = sib_series_stmt.where(Series.parent_folder_id.is_(None))
                sib_series_res = await db.execute(sib_series_stmt.order_by(Series.title.asc()))
                sib_series = [
                    {"id": str(s.id), "name": s.title, "type": "series"}
                    for s in sib_series_res.scalars().all()
                ]
                path.append({"id": str(series.id), "type": "series", "name": series.title, "siblings": sib_series})

                curr_folder_id = series.parent_folder_id
                while curr_folder_id:
                    f = await db.get(Folder, curr_folder_id)
                    if not f or f.trashed_at is not None or (user_id and f.user_id != user_id):
                        break
                    sib_f_stmt = select(Folder).where(and_(Folder.trashed_at.is_(None)))
                    if user_id:
                        sib_f_stmt = sib_f_stmt.where(Folder.user_id == user_id)
                    if f.parent_id:
                        sib_f_stmt = sib_f_stmt.where(Folder.parent_id == f.parent_id)
                    else:
                        sib_f_stmt = sib_f_stmt.where(Folder.parent_id.is_(None))
                    sib_f_res = await db.execute(sib_f_stmt.order_by(Folder.name.asc()))
                    sib_f = [
                        {"id": str(sf.id), "name": sf.name, "type": "folder"}
                        for sf in sib_f_res.scalars().all()
                    ]
                    path.append({"id": str(f.id), "type": "folder", "name": f.name, "siblings": sib_f})
                    curr_folder_id = f.parent_id

        elif scope_type == "folder":
            curr_folder_id = scope_id
            visited = set()
            while curr_folder_id and curr_folder_id not in visited:
                visited.add(curr_folder_id)
                f = await db.get(Folder, curr_folder_id)
                if not f or f.trashed_at is not None or (user_id and f.user_id != user_id):
                    break
                sib_f_stmt = select(Folder).where(and_(Folder.trashed_at.is_(None)))
                if user_id:
                    sib_f_stmt = sib_f_stmt.where(Folder.user_id == user_id)
                if f.parent_id:
                    sib_f_stmt = sib_f_stmt.where(Folder.parent_id == f.parent_id)
                else:
                    sib_f_stmt = sib_f_stmt.where(Folder.parent_id.is_(None))
                sib_f_res = await db.execute(sib_f_stmt.order_by(Folder.name.asc()))
                sib_f = [
                    {"id": str(sf.id), "name": sf.name, "type": "folder"}
                    for sf in sib_f_res.scalars().all()
                ]
                path.append({"id": str(f.id), "type": "folder", "name": f.name, "siblings": sib_f})
                curr_folder_id = f.parent_id

        path.reverse()
        return crumbs + path

    @staticmethod
    async def get_scope_contents(
        db: AsyncSession,
        user_id: Optional[uuid.UUID] = None,
        scope_id: Optional[str] = None,
        scope_type: Optional[str] = None,
        search_query: Optional[str] = None,
        content_filter: str = "content",
        page: int = 1,
        limit: int = 20,
        trash_type: Optional[str] = None,
        trash_sort: str = "trashed_at",
        trash_direction: str = "desc",
    ) -> Dict[str, Any]:
        """
        Authoritative unified navigation resolver for Root, Uncategorized, Folder, Series, Season, and Trash.
        Supports 4 authoritative content filters: 'content' (default), 'uncategorized', 'test', 'all'.
        """
        is_trash = (scope_id == "trash" or scope_type == "trash" or content_filter == "trash")
        is_uncategorized = (scope_id == "uncategorized" or scope_type == "uncategorized" or content_filter == "uncategorized")
        is_test = (content_filter == "test")
        is_root = (scope_id is None or scope_id == "root" or scope_type == "root" or scope_id == "")

        test_cond = get_test_video_condition()

        # ==========================================
        # 0. SPECIAL SCOPE: TRASH (سلة المحذوفات)
        # ==========================================
        if is_trash:
            return await DriveService._get_trash_page(
                db=db,
                user_id=user_id,
                search_query=search_query,
                page=page,
                limit=limit,
                trash_type=trash_type,
                sort=trash_sort,
                direction=trash_direction,
            )

        # Search spans the complete active library regardless of the open folder.
        if search_query and not is_test and not is_uncategorized:
            stmt = (
                select(Video)
                .options(selectinload(Video.renditions), selectinload(Video.subtitles), selectinload(Video.font_assets))
                .where(Video.trashed_at.is_(None), ~test_cond)
                .where(or_(Video.title.ilike(f"%{search_query}%"), Video.source_filename.ilike(f"%{search_query}%")))
                .order_by(Video.title.asc())
            )
            if user_id:
                stmt = stmt.where(Video.user_id == user_id)
            videos = (await db.execute(stmt)).scalars().all()
            result_items = []
            for video in videos:
                folder_path = []
                folder_id = video.folder_id
                visited = set()
                while folder_id and folder_id not in visited:
                    visited.add(folder_id)
                    folder = await db.get(Folder, folder_id)
                    if not folder or folder.trashed_at is not None or (user_id and folder.user_id != user_id):
                        break
                    folder_path.append(folder.name)
                    folder_id = folder.parent_id
                folder_path.reverse()
                if video.folder_id and folder_id:
                    continue
                subtitle_label, subtitle_detail, font_count = get_video_subtitles_info(video)
                result_items.append({
                    "id": str(video.id), "title": video.title, "source_filename": video.source_filename,
                    "type": "حلقة" if video.episode_number is not None else "فيديو",
                    "folder_id": str(video.folder_id) if video.folder_id else None,
                    "series_id": str(video.series_id) if video.series_id else None,
                    "season_id": str(video.season_id) if video.season_id else None,
                    "episode_number": video.episode_number, "quality": get_video_quality(video),
                    "subtitles": subtitle_label, "subtitles_detail": subtitle_detail, "font_count": font_count,
                    "size_bytes": video.source_size_bytes or 0, "size_formatted": format_bytes(video.source_size_bytes),
                    "duration_seconds": video.source_duration_seconds or 0,
                    "duration_formatted": format_duration(video.source_duration_seconds),
                    "created_at": video.created_at.strftime("%Y-%m-%d") if video.created_at else "",
                    "status": video.status.value, "visibility": video.visibility.value,
                    "location_path": "مكتبة المحتوى" + (" / " + " / ".join(folder_path) if folder_path else ""),
                })
            crumbs = [{"id": "root", "type": "root", "name": "مكتبة المحتوى", "siblings": []}]
            return {"current_scope": {"id": "root", "type": "root", "name": "نتائج البحث", "parent_id": None},
                    "breadcrumbs": crumbs, "folders": [], "series": [], "seasons": [], "videos": result_items,
                    "trash_items": [], "items": result_items, "total_items": len(result_items)}

        # ==========================================
        # 0.1 SPECIAL SCOPE: UNCATEGORIZED (غير مصنف)
        # ==========================================
        if is_uncategorized:
            videos_list = []
            video_stmt = (
                select(Video)
                .options(
                    selectinload(Video.renditions),
                    selectinload(Video.subtitles),
                    selectinload(Video.font_assets),
                )
                .where(
                    and_(
                        Video.folder_id.is_(None),
                        Video.trashed_at.is_(None),
                        ~test_cond,
                    )
                )
                .order_by(Video.created_at.desc())
            )
            if user_id:
                video_stmt = video_stmt.where(Video.user_id == user_id)
            if search_query:
                video_stmt = video_stmt.where(
                    or_(
                        Video.title.ilike(f"%{search_query}%"),
                        Video.source_filename.ilike(f"%{search_query}%"),
                    )
                )
            for v in (await db.execute(video_stmt)).scalars().all():
                effective_dl = await DriveService.get_effective_download_policy(db, v)
                sub_label, sub_detail, font_count = get_video_subtitles_info(v)
                videos_list.append({
                    "id": str(v.id),
                    "title": v.title,
                    "source_filename": v.source_filename,
                    "type": "فيديو",
                    "folder_id": None,
                    "series_id": None,
                    "season_id": None,
                    "episode_number": v.episode_number,
                    "quality": get_video_quality(v),
                    "subtitles": sub_label,
                    "subtitles_detail": sub_detail,
                    "font_count": font_count,
                    "size_bytes": v.source_size_bytes or 0,
                    "size_formatted": format_bytes(v.source_size_bytes),
                    "duration_seconds": v.source_duration_seconds or 0,
                    "duration_formatted": format_duration(v.source_duration_seconds),
                    "view_count": v.view_count or 0,
                    "created_at": v.created_at.strftime("%Y-%m-%d") if v.created_at else "",
                    "allow_download_policy": v.allow_download_policy,
                    "effective_allow_download": effective_dl,
                    "status": v.status.value,
                    "visibility": v.visibility.value,
                })
            return {
                "current_scope": {"id": "uncategorized", "type": "uncategorized", "name": "غير مصنف", "parent_id": "root"},
                "breadcrumbs": [
                    {"id": "root", "type": "root", "name": "مكتبة المحتوى", "siblings": []},
                    {"id": "uncategorized", "type": "uncategorized", "name": "غير مصنف", "siblings": []},
                ],
                "folders": [],
                "series": [],
                "seasons": [],
                "videos": videos_list,
                "trash_items": [],
                "items": videos_list,
                "total_items": len(videos_list),
            }

        # ==========================================
        # 0.2 SPECIAL SCOPE: TEST DATA (بيانات الاختبار)
        # ==========================================
        if is_test:
            videos_list = []
            video_stmt = (
                select(Video)
                .options(
                    selectinload(Video.renditions),
                    selectinload(Video.subtitles),
                    selectinload(Video.font_assets),
                )
                .where(
                    and_(
                        Video.trashed_at.is_(None),
                        test_cond,
                    )
                )
                .order_by(Video.created_at.desc())
            )
            if user_id:
                video_stmt = video_stmt.where(Video.user_id == user_id)
            if search_query:
                video_stmt = video_stmt.where(
                    or_(
                        Video.title.ilike(f"%{search_query}%"),
                        Video.source_filename.ilike(f"%{search_query}%"),
                    )
                )
            for v in (await db.execute(video_stmt)).scalars().all():
                effective_dl = await DriveService.get_effective_download_policy(db, v)
                sub_label, sub_detail, font_count = get_video_subtitles_info(v)
                videos_list.append({
                    "id": str(v.id),
                    "title": v.title,
                    "source_filename": v.source_filename,
                    "type": "فيديو تجريبي",
                    "folder_id": str(v.folder_id) if v.folder_id else None,
                    "series_id": str(v.series_id) if v.series_id else None,
                    "season_id": str(v.season_id) if v.season_id else None,
                    "episode_number": v.episode_number,
                    "quality": get_video_quality(v),
                    "subtitles": sub_label,
                    "subtitles_detail": sub_detail,
                    "font_count": font_count,
                    "size_bytes": v.source_size_bytes or 0,
                    "size_formatted": format_bytes(v.source_size_bytes),
                    "duration_seconds": v.source_duration_seconds or 0,
                    "duration_formatted": format_duration(v.source_duration_seconds),
                    "view_count": v.view_count or 0,
                    "created_at": v.created_at.strftime("%Y-%m-%d") if v.created_at else "",
                    "allow_download_policy": v.allow_download_policy,
                    "effective_allow_download": effective_dl,
                    "status": v.status.value,
                    "visibility": v.visibility.value,
                })
            return {
                "current_scope": {"id": "test", "type": "test", "name": "بيانات الاختبار", "parent_id": "root"},
                "breadcrumbs": [
                    {"id": "root", "type": "root", "name": "مكتبة المحتوى", "siblings": []},
                    {"id": "test", "type": "test", "name": "بيانات الاختبار", "siblings": []},
                ],
                "folders": [],
                "series": [],
                "seasons": [],
                "videos": videos_list,
                "trash_items": [],
                "items": videos_list,
                "total_items": len(videos_list),
            }

        # Resolve entity UUID for Season, Series, Folder, Root
        resolved_type = "root"
        resolved_uuid: Optional[uuid.UUID] = None

        if not is_root and scope_id:
            try:
                resolved_uuid = uuid.UUID(scope_id)
                if not scope_type or scope_type == "auto":
                    if await db.get(Season, resolved_uuid):
                        resolved_type = "season"
                    elif await db.get(Series, resolved_uuid):
                        resolved_type = "series"
                    elif await db.get(Folder, resolved_uuid):
                        resolved_type = "folder"
                    else:
                        is_root = True
                else:
                    resolved_type = scope_type
            except ValueError:
                is_root = True

        if user_id and resolved_uuid and resolved_type in {"folder", "series", "season"}:
            model = {"folder": Folder, "series": Series, "season": Season}[resolved_type]
            scoped_entity = await db.get(model, resolved_uuid)
            if resolved_type == "season":
                parent_series = await db.get(Series, scoped_entity.series_id) if scoped_entity else None
                scoped_owner = parent_series.owner_id if parent_series else None
            elif resolved_type == "series":
                scoped_owner = scoped_entity.owner_id if scoped_entity else None
            else:
                scoped_owner = scoped_entity.user_id if scoped_entity else None
            if scoped_owner != user_id:
                raise PermissionError("Drive scope is not owned by the current user")

        breadcrumbs = await DriveService.get_breadcrumbs(db, resolved_type, resolved_uuid, user_id)

        current_scope_info = {
            "id": "root" if is_root else str(resolved_uuid),
            "type": resolved_type,
            "name": "مكتبة المحتوى" if is_root else "",
            "parent_id": None,
            "allow_download_policy": "ALLOW",
        }

        folders_list: List[Dict[str, Any]] = []
        series_list_out: List[Dict[str, Any]] = []
        seasons_list_out: List[Dict[str, Any]] = []
        videos_list: List[Dict[str, Any]] = []

        # ==========================================
        # 1. SCOPE: SEASON
        # ==========================================
        if resolved_type == "season" and resolved_uuid:
            season = await db.get(Season, resolved_uuid)
            if season and season.trashed_at is None:
                series = await db.get(Series, season.series_id)
                current_scope_info.update({
                    "name": season.display_name,
                    "season_number": season.season_number,
                    "series_id": str(season.series_id),
                    "series_name": series.title if series else "",
                    "parent_id": str(season.series_id),
                    "allow_download_policy": season.allow_download_policy,
                })

                ep_stmt = (
                    select(Video)
                    .options(
                        selectinload(Video.renditions),
                        selectinload(Video.subtitles),
                        selectinload(Video.font_assets),
                    )
                    .where(and_(Video.season_id == resolved_uuid, Video.trashed_at.is_(None)))
                    .order_by(asc(Video.episode_number), asc(Video.title))
                )
                if user_id:
                    ep_stmt = ep_stmt.where(Video.user_id == user_id)
                if content_filter == "content":
                    ep_stmt = ep_stmt.where(~test_cond)
                if search_query:
                    ep_stmt = ep_stmt.where(
                        or_(
                            Video.title.ilike(f"%{search_query}%"),
                            Video.source_filename.ilike(f"%{search_query}%"),
                        )
                    )
                ep_res = await db.execute(ep_stmt)
                for ep in ep_res.scalars().all():
                    effective_dl = await DriveService.get_effective_download_policy(db, ep)
                    sub_label, sub_detail, font_count = get_video_subtitles_info(ep)
                    quality = get_video_quality(ep)
                    videos_list.append({
                        "id": str(ep.id),
                        "title": ep.title,
                        "source_filename": ep.source_filename,
                        "type": "حلقة",
                        "season_id": str(ep.season_id) if ep.season_id else None,
                        "series_id": str(ep.series_id) if ep.series_id else None,
                        "folder_id": str(ep.folder_id) if ep.folder_id else None,
                        "episode_number": ep.episode_number,
                        "season_number": ep.season_number or season.season_number,
                        "quality": quality,
                        "subtitles": sub_label,
                        "subtitles_detail": sub_detail,
                        "font_count": font_count,
                        "size_bytes": ep.source_size_bytes or 0,
                        "size_formatted": format_bytes(ep.source_size_bytes),
                        "duration_seconds": ep.source_duration_seconds or 0,
                        "duration_formatted": format_duration(ep.source_duration_seconds),
                        "view_count": ep.view_count or 0,
                        "created_at": ep.created_at.strftime("%Y-%m-%d") if ep.created_at else "",
                        "allow_download_policy": ep.allow_download_policy,
                        "effective_allow_download": effective_dl,
                        "status": ep.status.value,
                        "visibility": ep.visibility.value,
                    })

        # ==========================================
        # 2. SCOPE: SERIES
        # ==========================================
        elif resolved_type == "series" and resolved_uuid:
            series = await db.get(Series, resolved_uuid)
            if series and series.trashed_at is None:
                current_scope_info.update({
                    "name": series.title,
                    "description": series.description or "",
                    "year": series.year,
                    "parent_id": str(series.parent_folder_id) if series.parent_folder_id else "root",
                    "allow_download_policy": series.allow_download_policy,
                })

                s_stmt = select(Season).where(and_(Season.series_id == resolved_uuid, Season.trashed_at.is_(None))).order_by(asc(Season.sort_order), asc(Season.season_number))
                if search_query:
                    s_stmt = s_stmt.where(Season.display_name.ilike(f"%{search_query}%"))
                s_res = await db.execute(s_stmt)
                for sn in s_res.scalars().all():
                    ep_count_stmt = select(
                        func.count(Video.id),
                        func.coalesce(func.sum(Video.source_size_bytes), 0)
                    ).where(and_(Video.season_id == sn.id, Video.trashed_at.is_(None)))
                    if content_filter == "content":
                        ep_count_stmt = ep_count_stmt.where(~test_cond)
                    ep_count_res = await db.execute(ep_count_stmt)
                    ep_count, ep_size = ep_count_res.one()

                    seasons_list_out.append({
                        "id": str(sn.id),
                        "type": "season",
                        "name": sn.display_name,
                        "season_number": sn.season_number,
                        "series_id": str(series.id),
                        "series_name": series.title,
                        "items_count": ep_count,
                        "size_bytes": ep_size,
                        "size_formatted": format_bytes(ep_size),
                        "allow_download_policy": sn.allow_download_policy,
                        "updated_at": sn.updated_at.strftime("%Y-%m-%d") if sn.updated_at else "",
                    })

                # Unassigned videos in this series
                uv_stmt = (
                    select(Video)
                    .options(
                        selectinload(Video.renditions),
                        selectinload(Video.subtitles),
                        selectinload(Video.font_assets),
                    )
                    .where(and_(Video.series_id == resolved_uuid, Video.season_id.is_(None), Video.trashed_at.is_(None)))
                    .order_by(Video.created_at.desc())
                )
                if user_id:
                    uv_stmt = uv_stmt.where(Video.user_id == user_id)
                if content_filter == "content":
                    uv_stmt = uv_stmt.where(~test_cond)
                if search_query:
                    uv_stmt = uv_stmt.where(
                        or_(
                            Video.title.ilike(f"%{search_query}%"),
                            Video.source_filename.ilike(f"%{search_query}%"),
                        )
                    )
                for ep in (await db.execute(uv_stmt)).scalars().all():
                    effective_dl = await DriveService.get_effective_download_policy(db, ep)
                    sub_label, sub_detail, font_count = get_video_subtitles_info(ep)
                    videos_list.append({
                        "id": str(ep.id),
                        "title": ep.title,
                        "source_filename": ep.source_filename,
                        "type": "حلقة" if ep.episode_number is not None else "فيديو",
                        "series_id": str(ep.series_id),
                        "season_id": None,
                        "episode_number": ep.episode_number,
                        "quality": get_video_quality(ep),
                        "subtitles": sub_label,
                        "subtitles_detail": sub_detail,
                        "font_count": font_count,
                        "size_bytes": ep.source_size_bytes or 0,
                        "size_formatted": format_bytes(ep.source_size_bytes),
                        "duration_seconds": ep.source_duration_seconds or 0,
                        "duration_formatted": format_duration(ep.source_duration_seconds),
                        "view_count": ep.view_count or 0,
                        "created_at": ep.created_at.strftime("%Y-%m-%d") if ep.created_at else "",
                        "allow_download_policy": ep.allow_download_policy,
                        "effective_allow_download": effective_dl,
                        "status": ep.status.value,
                        "visibility": ep.visibility.value,
                    })

        # ==========================================
        # 3. SCOPE: FOLDER OR ROOT
        # ==========================================
        elif resolved_type in ("folder", "root"):
            if resolved_type == "folder" and resolved_uuid:
                folder = await db.get(Folder, resolved_uuid)
                if folder and folder.trashed_at is None:
                    current_scope_info.update({
                        "name": folder.name,
                        "parent_id": str(folder.parent_id) if folder.parent_id else "root",
                        "allow_download_policy": folder.allow_download_policy,
                    })

            # Subfolders (non-trashed)
            subfolder_stmt = select(Folder).where(Folder.trashed_at.is_(None)).order_by(Folder.name.asc())
            if user_id:
                subfolder_stmt = subfolder_stmt.where(Folder.user_id == user_id)
            if is_root:
                subfolder_stmt = subfolder_stmt.where(Folder.parent_id.is_(None))
            else:
                subfolder_stmt = subfolder_stmt.where(Folder.parent_id == resolved_uuid)
            if search_query:
                subfolder_stmt = subfolder_stmt.where(Folder.name.ilike(f"%{search_query}%"))

            for sf in (await db.execute(subfolder_stmt)).scalars().all():
                v_count_stmt = select(
                    func.count(Video.id),
                    func.coalesce(func.sum(Video.source_size_bytes), 0)
                ).where(and_(Video.folder_id == sf.id, Video.trashed_at.is_(None)))
                if content_filter == "content":
                    v_count_stmt = v_count_stmt.where(~test_cond)
                v_count, v_size = (await db.execute(v_count_stmt)).one()

                sf_count = (await db.execute(select(func.count(Folder.id)).where(and_(Folder.parent_id == sf.id, Folder.trashed_at.is_(None))))).scalar() or 0
                folders_list.append({
                    "id": str(sf.id),
                    "type": "folder",
                    "name": sf.name,
                    "parent_id": str(sf.parent_id) if sf.parent_id else None,
                    "items_count": v_count + sf_count,
                    "size_bytes": v_size,
                    "size_formatted": format_bytes(v_size),
                    "allow_download_policy": sf.allow_download_policy,
                    "updated_at": sf.updated_at.strftime("%Y-%m-%d") if sf.updated_at else "",
                })

            # Series in this folder or root
            series_stmt = select(Series).where(Series.trashed_at.is_(None)).order_by(Series.title.asc())
            if user_id:
                series_stmt = series_stmt.where(Series.owner_id == user_id)
            if is_root:
                series_stmt = series_stmt.where(Series.parent_folder_id.is_(None))
            else:
                series_stmt = series_stmt.where(Series.parent_folder_id == resolved_uuid)
            if search_query:
                series_stmt = series_stmt.where(Series.title.ilike(f"%{search_query}%"))
            for s in (await db.execute(series_stmt)).scalars().all():
                sn_count = (await db.execute(select(func.count(Season.id)).where(Season.series_id == s.id))).scalar() or 0
                ep_count = (await db.execute(select(func.count(Video.id)).where(Video.series_id == s.id))).scalar() or 0
                series_list_out.append({
                    "id": str(s.id),
                    "type": "series",
                    "name": s.title,
                    "parent_id": str(s.parent_folder_id) if s.parent_folder_id else None,
                    "seasons_count": sn_count,
                    "episodes_count": ep_count,
                    "items_count": ep_count,
                    "size_formatted": "—",
                    "allow_download_policy": s.allow_download_policy,
                    "updated_at": s.updated_at.strftime("%Y-%m-%d") if s.updated_at else "",
                })

            # Independent Videos in this folder (or root unassigned)
            video_stmt = (
                select(Video)
                .options(
                    selectinload(Video.renditions),
                    selectinload(Video.subtitles),
                    selectinload(Video.font_assets),
                )
                .where(Video.trashed_at.is_(None))
                .order_by(Video.created_at.desc())
            )
            if user_id:
                video_stmt = video_stmt.where(Video.user_id == user_id)
            if is_root:
                video_stmt = video_stmt.where(
                    and_(
                        Video.folder_id.is_(None),
                    )
                )
            else:
                video_stmt = video_stmt.where(
                    and_(
                        Video.folder_id == resolved_uuid,
                    )
                )
            if content_filter == "content":
                video_stmt = video_stmt.where(~test_cond)
            if search_query:
                video_stmt = video_stmt.where(
                    or_(
                        Video.title.ilike(f"%{search_query}%"),
                        Video.source_filename.ilike(f"%{search_query}%"),
                    )
                )

            for v in (await db.execute(video_stmt)).scalars().all():
                effective_dl = await DriveService.get_effective_download_policy(db, v)
                sub_label, sub_detail, font_count = get_video_subtitles_info(v)
                is_ep = (v.episode_number is not None or v.season_number is not None)
                videos_list.append({
                    "id": str(v.id),
                    "title": v.title,
                    "source_filename": v.source_filename,
                    "type": "حلقة" if is_ep else "فيديو",
                    "folder_id": str(v.folder_id) if v.folder_id else None,
                    "series_id": str(v.series_id) if v.series_id else None,
                    "season_id": str(v.season_id) if v.season_id else None,
                    "episode_number": v.episode_number,
                    "quality": get_video_quality(v),
                    "subtitles": sub_label,
                    "subtitles_detail": sub_detail,
                    "font_count": font_count,
                    "size_bytes": v.source_size_bytes or 0,
                    "size_formatted": format_bytes(v.source_size_bytes),
                    "duration_seconds": v.source_duration_seconds or 0,
                    "duration_formatted": format_duration(v.source_duration_seconds),
                    "view_count": v.view_count or 0,
                    "created_at": v.created_at.strftime("%Y-%m-%d") if v.created_at else "",
                    "allow_download_policy": v.allow_download_policy,
                    "effective_allow_download": effective_dl,
                    "status": v.status.value,
                    "visibility": v.visibility.value,
                })

        all_items = folders_list + series_list_out + seasons_list_out + videos_list
        return {
            "current_scope": current_scope_info,
            "breadcrumbs": breadcrumbs,
            "folders": folders_list,
            "series": series_list_out,
            "seasons": seasons_list_out,
            "videos": videos_list,
            "trash_items": [],
            "items": all_items,
            "total_items": len(all_items),
        }

    # ==========================================
    # LOGICAL TRASH, RESTORE & PERMANENT DELETE
    # ==========================================
    _TRASH_TYPE_ALIASES = {
        "folder": "folder", "series": "series", "season": "season", "video": "video",
        "مجلد": "folder", "مسلسل": "series", "موسم": "season",
        "فيديو": "video", "فيديو تجريبي": "video", "حلقة": "video",
    }

    @staticmethod
    def _normalize_trash_type(value: str) -> str:
        normalized = DriveService._TRASH_TYPE_ALIASES.get((value or "").strip().lower())
        if not normalized:
            raise ValueError("Unsupported Drive entity type")
        return normalized

    @staticmethod
    async def _trash_entity(db: AsyncSession, entity_type: str, entity_id: uuid.UUID):
        model = {"folder": Folder, "series": Series, "season": Season, "video": Video}[entity_type]
        entity = await db.get(model, entity_id)
        if not entity:
            raise ValueError("Drive entity was not found")
        return entity

    @staticmethod
    async def _descendant_keys(db: AsyncSession, entity_type: str, entity_id: uuid.UUID) -> set[tuple[str, uuid.UUID]]:
        """Return a logical subtree without touching physical media paths."""
        keys = {(entity_type, entity_id)}
        folder_ids: set[uuid.UUID] = set()
        series_ids: set[uuid.UUID] = set()
        season_ids: set[uuid.UUID] = set()

        if entity_type == "folder":
            folder_ids.add(entity_id)
            frontier = {entity_id}
            while frontier:
                rows = (await db.execute(select(Folder.id).where(Folder.parent_id.in_(frontier)))).scalars().all()
                frontier = set(rows) - folder_ids
                folder_ids.update(frontier)
        elif entity_type == "series":
            series_ids.add(entity_id)
        elif entity_type == "season":
            season_ids.add(entity_id)

        if series_ids:
            season_ids.update((await db.execute(select(Season.id).where(Season.series_id.in_(series_ids)))).scalars().all())

        for folder_id in folder_ids:
            keys.add(("folder", folder_id))
        for series_id in series_ids:
            keys.add(("series", series_id))
        for season_id in season_ids:
            keys.add(("season", season_id))

        video_conditions = []
        if folder_ids:
            video_conditions.append(Video.folder_id.in_(folder_ids))
        if series_ids:
            video_conditions.append(Video.series_id.in_(series_ids))
        if season_ids:
            video_conditions.append(Video.season_id.in_(season_ids))
        if entity_type == "video":
            keys.add(("video", entity_id))
        elif video_conditions:
            video_ids = (await db.execute(select(Video.id).where(or_(*video_conditions)))).scalars().all()
            keys.update(("video", video_id) for video_id in video_ids)
        return keys

    @staticmethod
    async def _assert_trash_ownership(
        db: AsyncSession,
        entity_type: str,
        entity: Any,
        owner_id: uuid.UUID | None,
    ) -> None:
        """Require non-admin callers to own every logical record they mutate."""
        if owner_id is None:
            return
        if entity_type == "season":
            series = await db.get(Series, entity.series_id)
            entity_owner_id = series.owner_id if series else None
        elif entity_type == "series":
            entity_owner_id = entity.owner_id
        else:
            entity_owner_id = entity.user_id
        if entity_owner_id != owner_id:
            raise ValueError("Drive entity is not owned by the current user")

    @staticmethod
    async def _prepare_trash_roots(
        db: AsyncSession,
        items: List[Dict[str, str]],
        owner_id: uuid.UUID | None = None,
    ) -> list[tuple[str, Any, set[tuple[str, uuid.UUID]]]]:
        """Validate the complete request and discard selections covered by another selected ancestor."""
        if not items:
            raise ValueError("At least one Drive entity is required")
        requested: list[tuple[str, uuid.UUID, Any]] = []
        seen: set[tuple[str, uuid.UUID]] = set()
        for item in items:
            try:
                entity_id = uuid.UUID(str(item["id"]))
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError("Invalid Drive entity id") from exc
            entity_type = DriveService._normalize_trash_type(item.get("type", ""))
            key = (entity_type, entity_id)
            if key not in seen:
                entity = await DriveService._trash_entity(db, entity_type, entity_id)
                await DriveService._assert_trash_ownership(db, entity_type, entity, owner_id)
                requested.append((entity_type, entity_id, entity))
                seen.add(key)

        expanded = [(kind, entity, await DriveService._descendant_keys(db, kind, entity_id)) for kind, entity_id, entity in requested]
        roots = []
        for index, candidate in enumerate(expanded):
            candidate_key = (candidate[0], candidate[1].id)
            covered = set().union(*(other[2] for other_index, other in enumerate(expanded) if other_index != index))
            if candidate_key not in covered:
                roots.append(candidate)
        # A parent can contain malformed legacy records owned by someone else.
        # Validate the entire expanded mutation set before any state changes.
        for entity_type, entity in await DriveService._load_trash_entities(
            db, set().union(*(root[2] for root in roots))
        ):
            await DriveService._assert_trash_ownership(db, entity_type, entity, owner_id)
        return roots

    @staticmethod
    async def _load_trash_entities(db: AsyncSession, keys: set[tuple[str, uuid.UUID]]) -> list[tuple[str, Any]]:
        entities = []
        for entity_type, entity_id in keys:
            entities.append((entity_type, await DriveService._trash_entity(db, entity_type, entity_id)))
        return entities

    @staticmethod
    async def _commit_or_rollback(db: AsyncSession) -> None:
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    @staticmethod
    async def trash_items(
        db: AsyncSession,
        items: List[Dict[str, str]],
        owner_id: uuid.UUID | None = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """
        Soft-delete items by setting trashed_at timestamp.
        Physical media files remain untouched.
        Folders and series are trashed logically as single hierarchies.
        """
        summary = {"folders": 0, "series": 0, "seasons": 0, "videos": 0}
        roots = await DriveService._prepare_trash_roots(db, items, owner_id)
        all_keys = set().union(*(root[2] for root in roots))
        entities = await DriveService._load_trash_entities(db, all_keys)
        if any(entity.trashed_at is not None for _, entity in entities):
            raise ValueError("Drive entity is already in Trash")
        now = datetime.now(timezone.utc)
        for entity_type, entity in entities:
            entity.trashed_at = now
            summary[f"{entity_type}s" if entity_type != "series" else "series"] += 1
        await DriveService._commit_or_rollback(db)
        for entity_type, entity in entities:
            logger.info("drive.audit action=%s.trash entity_id=%s", entity_type, entity.id)
        return len(entities), summary

    @staticmethod
    async def restore_items(
        db: AsyncSession,
        items: List[Dict[str, str]],
        owner_id: uuid.UUID | None = None,
    ) -> int:
        """
        Restore soft-deleted items by resetting trashed_at to None.
        They return to their original logical locations and hierarchy.
        """
        roots = await DriveService._prepare_trash_roots(db, items, owner_id)
        if any(root[1].trashed_at is None for root in roots):
            raise ValueError("Only trashed Drive entities can be restored")
        count = 0
        restored_entities: list[tuple[str, Any]] = []
        for _, root, keys in roots:
            root_timestamp = root.trashed_at
            for _, entity in await DriveService._load_trash_entities(db, keys):
                if entity.trashed_at == root_timestamp:
                    entity.trashed_at = None
                    restored_entities.append((_, entity))
                    count += 1
        await DriveService._commit_or_rollback(db)
        for entity_type, entity in restored_entities:
            logger.info("drive.audit action=%s.restore entity_id=%s", entity_type, entity.id)
        return count

    @staticmethod
    async def permanent_delete_items(
        db: AsyncSession,
        items: List[Dict[str, str]],
        owner_id: uuid.UUID | None = None,
    ) -> int:
        """
        Permanently remove logical records from the database.
        Physical media files (HLS, master, fonts, etc.) remain untouched on disk.
        """
        roots = await DriveService._prepare_trash_roots(db, items, owner_id)
        all_keys = set().union(*(root[2] for root in roots))
        entities = await DriveService._load_trash_entities(db, all_keys)
        if any(entity.trashed_at is None for _, entity in entities):
            raise ValueError("Permanent delete is allowed only from Trash")

        video_ids = [entity.id for kind, entity in entities if kind == "video"]
        if video_ids:
            active_jobs = (
                await db.execute(
                    select(TranscodeJob.id)
                    .where(
                        TranscodeJob.video_id.in_(video_ids),
                        TranscodeJob.status.in_(
                            [
                                TranscodeJobStatus.QUEUED,
                                TranscodeJobStatus.PROBING,
                                TranscodeJobStatus.PROCESSING,
                                TranscodeJobStatus.VERIFYING,
                            ]
                        ),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if active_jobs is not None:
                raise ValueError("Cannot permanently delete videos with active transcode jobs")

        await DriveService.remove_shares_for_targets(db, all_keys)
        # Delete the full validated logical subtree. In particular, deleting a
        # Folder alone would otherwise allow the database's SET NULL relation
        # on Series.parent_folder_id to leave trashed series records behind.
        delete_order = {"video": 0, "season": 1, "series": 2, "folder": 3}
        folder_parents = {entity.id: entity.parent_id for kind, entity in entities if kind == "folder"}

        def sort_key(item: tuple[str, Any]) -> tuple[int, int]:
            kind, entity = item
            if kind != "folder":
                return (delete_order[kind], 0)
            depth = 0
            parent_id = entity.parent_id
            while parent_id in folder_parents:
                depth += 1
                parent_id = folder_parents[parent_id]
            return (delete_order[kind], -depth)

        for _, entity in sorted(entities, key=sort_key):
            await db.delete(entity)
        await DriveService._commit_or_rollback(db)
        return len(entities)

    @staticmethod
    async def get_descendant_summary(
        db: AsyncSession,
        entity_type: str,
        entity_id: uuid.UUID,
        owner_id: uuid.UUID | None = None,
    ) -> Dict[str, Any]:
        """
        Calculate counts of descendants for clean deletion modal.
        """
        t = entity_type.lower()
        if owner_id is not None:
            normalized_type = DriveService._normalize_trash_type(entity_type)
            entity = await DriveService._trash_entity(db, normalized_type, entity_id)
            await DriveService._assert_trash_ownership(db, normalized_type, entity, owner_id)
        if t == "folder":
            folder = await db.get(Folder, entity_id)
            if not folder:
                return {}
            if owner_id is not None and folder.user_id != owner_id:
                raise ValueError("Drive entity is not owned by the current user")
            keys = await DriveService._descendant_keys(db, "folder", entity_id)
            folder_ids = [item_id for kind, item_id in keys if kind == "folder"]
            video_ids = (await db.execute(select(Video.id).where(Video.folder_id.in_(folder_ids)))).scalars().all()
            series_count = (await db.execute(select(func.count(Series.id)).where(Series.parent_folder_id.in_(folder_ids)))).scalar() or 0
            seasons_count = (await db.execute(select(func.count(Season.id)).join(Series).where(Series.parent_folder_id.in_(folder_ids)))).scalar() or 0
            videos_count = len(video_ids)
            subfolder_count = max(0, len(folder_ids) - 1)
            return {
                "name": folder.name,
                "type": "folder",
                "series_count": series_count,
                "seasons_count": seasons_count,
                "videos_count": videos_count,
                "folders_count": subfolder_count,
            }
        elif t == "series":
            series = await db.get(Series, entity_id)
            if not series:
                return {}
            sn_res = await db.execute(select(func.count(Season.id)).where(Season.series_id == entity_id))
            seasons_count = sn_res.scalar() or 0
            v_res = await db.execute(select(func.count(Video.id)).where(Video.series_id == entity_id))
            videos_count = v_res.scalar() or 0
            return {
                "name": series.title,
                "type": "series",
                "seasons_count": seasons_count,
                "videos_count": videos_count,
            }
        elif t == "season":
            season = await db.get(Season, entity_id)
            if not season:
                return {}
            v_res = await db.execute(select(func.count(Video.id)).where(Video.season_id == entity_id))
            videos_count = v_res.scalar() or 0
            return {
                "name": season.display_name,
                "type": "season",
                "videos_count": videos_count,
            }
        elif t in ("video", "حلقة"):
            v = await db.get(Video, entity_id)
            if not v:
                return {}
            return {
                "name": v.title,
                "type": "video",
                "videos_count": 1,
            }
        return {}

    # ==========================================
    # ENTITY CRUD OPERATIONS
    # ==========================================
    @staticmethod
    async def create_folder(
        db: AsyncSession,
        user_id: uuid.UUID,
        name: str,
        parent_id: Optional[uuid.UUID] = None,
        folder_type: str = "folder",
        owner_id: uuid.UUID | None = None,
    ) -> Folder:
        """Create a new logical folder."""
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("اسم المجلد مطلوب")
        if folder_type != "folder":
            raise ValueError("إنشاء المسلسلات والمواسم متاح من إدارة المحتوى فقط")

        if parent_id:
            parent = await db.get(Folder, parent_id)
            if not parent:
                raise ValueError("المجلد الرئيسي غير موجود")
            if parent.trashed_at is not None:
                raise ValueError("لا يمكن إنشاء مجلد داخل مجلد في سلة المحذوفات")
            if owner_id is not None and parent.user_id != owner_id:
                raise ValueError("المجلد الرئيسي غير مملوك للمستخدم الحالي")

        folder = Folder(
            id=uuid.uuid4(),
            user_id=user_id,
            parent_id=parent_id,
            name=clean_name,
            folder_type=folder_type,
            allow_download=True,
            allow_download_policy="ALLOW",
        )
        db.add(folder)
        await DriveService._commit_or_rollback(db)
        await db.refresh(folder)
        logger.info("drive.audit action=folder.create folder_id=%s parent_id=%s", folder.id, folder.parent_id)
        return folder

    @staticmethod
    async def create_series(
        db: AsyncSession,
        user_id: uuid.UUID,
        title: str,
        parent_folder_id: Optional[uuid.UUID] = None,
        description: Optional[str] = None,
        year: Optional[int] = None,
        owner_id: uuid.UUID | None = None,
    ) -> Series:
        """Create a new Series entity."""
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("اسم المسلسل مطلوب")

        if parent_folder_id:
            parent = await db.get(Folder, parent_folder_id)
            if not parent:
                raise ValueError("المجلد الأب غير موجود")
            if parent.trashed_at is not None or (owner_id is not None and parent.user_id != owner_id):
                raise ValueError("المجلد الهدف غير متاح للمستخدم الحالي")

        series = Series(
            id=uuid.uuid4(),
            owner_id=user_id,
            parent_folder_id=parent_folder_id,
            title=clean_title,
            description=description,
            year=year,
            allow_download_policy="INHERIT",
        )
        db.add(series)
        await db.commit()
        await db.refresh(series)
        return series

    @staticmethod
    async def create_season(
        db: AsyncSession,
        series_id: uuid.UUID,
        season_number: int,
        display_name: Optional[str] = None,
        owner_id: uuid.UUID | None = None,
    ) -> Season:
        """Create a new Season entity under a Series."""
        series = await db.get(Series, series_id)
        if not series:
            raise ValueError("المسلسل غير موجود")
        if series.trashed_at is not None or (owner_id is not None and series.owner_id != owner_id):
            raise ValueError("المسلسل غير متاح للمستخدم الحالي")

        clean_name = display_name.strip() if display_name else f"الموسم {season_number}"

        season = Season(
            id=uuid.uuid4(),
            series_id=series_id,
            season_number=season_number,
            display_name=clean_name,
            sort_order=season_number,
            allow_download_policy="INHERIT",
        )
        db.add(season)
        await db.commit()
        await db.refresh(season)
        return season

    @staticmethod
    async def rename_entity(
        db: AsyncSession,
        entity_type: str,
        entity_id: uuid.UUID,
        new_name: str,
        owner_id: uuid.UUID | None = None,
    ) -> Dict[str, Any]:
        """Polymorphic rename for folder, series, season, or video."""
        clean_name = new_name.strip()
        if not clean_name:
            raise ValueError("الاسم الجديد مطلوب")

        now = datetime.now(timezone.utc)
        if entity_type == "video":
            video = await db.get(Video, entity_id)
            if not video:
                raise ValueError("الفيديو غير موجود")
            await DriveService._assert_trash_ownership(db, "video", video, owner_id)
            video.title = clean_name
            video.updated_at = now
            await db.commit()
            logger.info("drive.audit action=video.rename video_id=%s", video.id)
            return {"id": str(video.id), "title": video.title, "type": "video"}

        elif entity_type == "folder":
            folder = await db.get(Folder, entity_id)
            if not folder:
                raise ValueError("المجلد غير موجود")
            await DriveService._assert_trash_ownership(db, "folder", folder, owner_id)
            folder.name = clean_name
            folder.updated_at = now
            await db.commit()
            logger.info("drive.audit action=folder.rename folder_id=%s", folder.id)
            return {"id": str(folder.id), "name": folder.name, "type": "folder"}

        elif entity_type == "series":
            series = await db.get(Series, entity_id)
            if not series:
                raise ValueError("المسلسل غير موجود")
            await DriveService._assert_trash_ownership(db, "series", series, owner_id)
            series.title = clean_name
            series.updated_at = now
            await db.commit()
            logger.info("drive.audit action=series.rename series_id=%s", series.id)
            return {"id": str(series.id), "name": series.title, "type": "series"}

        elif entity_type == "season":
            season = await db.get(Season, entity_id)
            if not season:
                raise ValueError("الموسم غير موجود")
            await DriveService._assert_trash_ownership(db, "season", season, owner_id)
            season.display_name = clean_name
            season.updated_at = now
            await db.commit()
            logger.info("drive.audit action=season.rename season_id=%s", season.id)
            return {"id": str(season.id), "name": season.display_name, "type": "season"}

        raise ValueError(f"نوع العنصر غير مدعوم: {entity_type}")

    @staticmethod
    async def update_download_policy(
        db: AsyncSession,
        entity_type: str,
        entity_id: uuid.UUID,
        policy: str,
        owner_id: uuid.UUID | None = None,
    ) -> Dict[str, Any]:
        """Update allow_download_policy ('INHERIT', 'ALLOW', 'DENY')."""
        if policy not in ("INHERIT", "ALLOW", "DENY"):
            raise ValueError("سياسة التنزيل يجب أن تكون INHERIT أو ALLOW أو DENY")
        if owner_id is not None:
            entity = await DriveService._trash_entity(db, entity_type, entity_id)
            await DriveService._assert_trash_ownership(db, entity_type, entity, owner_id)

        if entity_type == "video":
            video = await db.get(Video, entity_id)
            if not video:
                raise ValueError("الفيديو غير موجود")
            video.allow_download_policy = policy
            await db.commit()
            effective = await DriveService.get_effective_download_policy(db, video)
            return {"id": str(video.id), "policy": policy, "effective": effective}

        elif entity_type == "season":
            season = await db.get(Season, entity_id)
            if not season:
                raise ValueError("الموسم غير موجود")
            season.allow_download_policy = policy
            await db.commit()
            return {"id": str(season.id), "policy": policy}

        elif entity_type == "series":
            series = await db.get(Series, entity_id)
            if not series:
                raise ValueError("المسلسل غير موجود")
            series.allow_download_policy = policy
            await db.commit()
            return {"id": str(series.id), "policy": policy}

        elif entity_type == "folder":
            folder = await db.get(Folder, entity_id)
            if not folder:
                raise ValueError("المجلد غير موجود")
            folder.allow_download_policy = policy
            await db.commit()
            return {"id": str(folder.id), "policy": policy}

        raise ValueError(f"نوع العنصر غير معروف: {entity_type}")

    # ==========================================
    # MOVE ITEMS WITH SAFETY VALIDATIONS
    # ==========================================
    @staticmethod
    async def move_items(
        db: AsyncSession,
        items: List[Dict[str, str]],
        target_id: Optional[uuid.UUID],
        target_type: str = "folder",
        owner_id: uuid.UUID | None = None,
    ) -> Tuple[int, List[Dict[str, Any]]]:
        """Move library organization references atomically; video metadata stays intact."""
        if target_type not in ("folder", "root"):
            raise ValueError("يمكن نقل العناصر إلى مجلد أو إلى المكتبة فقط")
        target_folder = None
        if target_id and target_type != "root":
            target_folder = await db.get(Folder, target_id)
            if not target_folder:
                raise ValueError("المجلد الهدف غير موجود")
            if target_folder.trashed_at is not None:
                raise ValueError("لا يمكن النقل إلى مجلد في سلة المحذوفات")
            if owner_id is not None and target_folder.user_id != owner_id:
                raise ValueError("المجلد الهدف غير مملوك للمستخدم الحالي")
        if not items:
            raise ValueError("حدد عنصراً واحداً على الأقل للنقل")

        resolved: list[tuple[str, Any]] = []
        seen: set[tuple[str, uuid.UUID]] = set()
        for item in items:
            try:
                item_id = uuid.UUID(str(item.get("id", "")))
            except (ValueError, TypeError) as exc:
                raise ValueError("معرّف عنصر غير صالح") from exc
            kind = (item.get("type") or "video").lower()
            if kind not in ("video", "folder", "series"):
                raise ValueError("نوع العنصر غير مدعوم للنقل")
            key = (kind, item_id)
            if key in seen:
                continue
            seen.add(key)
            entity = await DriveService._trash_entity(db, kind, item_id)
            await DriveService._assert_trash_ownership(db, kind, entity, owner_id)
            if entity.trashed_at is not None:
                raise ValueError("لا يمكن نقل عنصر موجود في سلة المحذوفات")
            resolved.append((kind, entity))

        folder_ids = {entity.id for kind, entity in resolved if kind == "folder"}
        if target_folder and folder_ids:
            curr: uuid.UUID | None = target_folder.id
            visited: set[uuid.UUID] = set()
            while curr and curr not in visited:
                if curr in folder_ids:
                    raise ValueError("لا يمكن نقل المجلد إلى نفسه أو إلى أحد مجلداته الفرعية")
                visited.add(curr)
                parent = await db.get(Folder, curr)
                if not parent:
                    raise ValueError("مسار المجلد الهدف غير صالح")
                curr = parent.parent_id

        now = datetime.now(timezone.utc)
        previous_states: List[Dict[str, Any]] = []
        for kind, entity in resolved:
            if kind == "video":
                previous_states.append({"id": str(entity.id), "type": kind, "folder_id": str(entity.folder_id) if entity.folder_id else None})
                entity.folder_id = target_folder.id if target_folder else None
            elif kind == "folder":
                previous_states.append({"id": str(entity.id), "type": kind, "parent_id": str(entity.parent_id) if entity.parent_id else None})
                entity.parent_id = target_folder.id if target_folder else None
            else:
                previous_states.append({"id": str(entity.id), "type": kind, "parent_folder_id": str(entity.parent_folder_id) if entity.parent_folder_id else None})
                entity.parent_folder_id = target_folder.id if target_folder else None
            entity.updated_at = now
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        for kind, entity in resolved:
            logger.info("drive.audit action=%s.move entity_id=%s", kind, entity.id)
        return len(resolved), previous_states

    # ==========================================
    # SHARING SYSTEM
    # ==========================================
    _SHARE_TARGET_TYPES = frozenset(("folder", "series", "season", "video"))

    @staticmethod
    async def _share_target_entity(db: AsyncSession, share: Share) -> Optional[tuple[str, Any]]:
        """Resolve a polymorphic share target defensively, without revealing its state."""
        if share.target_type not in DriveService._SHARE_TARGET_TYPES:
            return None
        try:
            target = await DriveService._trash_entity(db, share.target_type, share.target_id)
        except ValueError:
            return None
        if target.trashed_at is not None:
            return None

        if share.target_type == "season":
            series = await db.get(Series, target.series_id)
            owner_id = series.owner_id if series else None
        elif share.target_type == "series":
            owner_id = target.owner_id
        else:
            owner_id = target.user_id
        if owner_id != share.owner_id:
            return None
        # A target remains unavailable if an ancestor was trashed independently.
        if share.target_type == "season" and (not series or series.trashed_at is not None):
            return None
        if share.target_type == "series" and target.parent_folder_id:
            folder = await db.get(Folder, target.parent_folder_id)
            if not folder or folder.trashed_at is not None or folder.user_id != owner_id:
                return None
        if share.target_type == "folder":
            parent_id = target.parent_id
            while parent_id:
                parent = await db.get(Folder, parent_id)
                if not parent or parent.trashed_at is not None or parent.user_id != owner_id:
                    return None
                parent_id = parent.parent_id
        return share.target_type, target

    @staticmethod
    async def _is_visible_share_video(db: AsyncSession, video: Video, owner_id: uuid.UUID) -> bool:
        """Reject trashed/malformed descendants and cross-owner records from a share scope."""
        if video.user_id != owner_id or video.trashed_at is not None:
            return False
        if video.season_id:
            season = await db.get(Season, video.season_id)
            if not season or season.trashed_at is not None:
                return False
        if video.series_id:
            series = await db.get(Series, video.series_id)
            if not series or series.trashed_at is not None or series.owner_id != owner_id:
                return False
            if series.parent_folder_id:
                folder = await db.get(Folder, series.parent_folder_id)
                if not folder or folder.trashed_at is not None or folder.user_id != owner_id:
                    return False
        if video.folder_id:
            folder = await db.get(Folder, video.folder_id)
            if not folder or folder.trashed_at is not None or folder.user_id != owner_id:
                return False
        return True

    @staticmethod
    async def get_share_scope_videos(db: AsyncSession, share: Share, video_id: Optional[uuid.UUID] = None) -> list[Video]:
        """Return only active videos genuinely contained in an active share's target subtree."""
        resolved = await DriveService._share_target_entity(db, share)
        if not resolved:
            return []
        target_type, target = resolved
        keys = await DriveService._descendant_keys(db, target_type, target.id)
        video_ids = [entity_id for entity_type, entity_id in keys if entity_type == "video"]
        if video_id is not None:
            # Individual media authorization need not load every video's metadata.
            video_ids = [video_id] if video_id in video_ids else []
        if not video_ids:
            return []
        videos = (await db.execute(
            select(Video)
            .options(selectinload(Video.renditions), selectinload(Video.subtitles), selectinload(Video.font_assets), selectinload(Video.season))
            .where(Video.id.in_(video_ids), Video.trashed_at.is_(None))
            .order_by(asc(Video.season_number), asc(Video.episode_number), asc(Video.title))
        )).scalars().all()
        return [video for video in videos if await DriveService._is_visible_share_video(db, video, share.owner_id)]

    @staticmethod
    async def get_shared_video_if_available(db: AsyncSession, share: Share, video_id: uuid.UUID) -> Optional[Video]:
        """Authoritative scope check shared by anonymous listing and download paths."""
        for video in await DriveService.get_share_scope_videos(db, share, video_id=video_id):
            if video.id == video_id:
                return video
        return None

    @staticmethod
    async def remove_shares_for_targets(
        db: AsyncSession,
        target_keys: set[tuple[str, uuid.UUID]],
    ) -> int:
        """Remove shares for a permanent-delete subtree; caller owns the transaction."""
        if not target_keys:
            return 0
        clauses = [and_(Share.target_type == target_type, Share.target_id == target_id) for target_type, target_id in target_keys]
        shares = (await db.execute(select(Share).where(or_(*clauses)))).scalars().all()
        for share in shares:
            await db.delete(share)
        return len(shares)

    @staticmethod
    async def reconcile_orphan_shares(db: AsyncSession, *, apply: bool = False) -> list[uuid.UUID]:
        """Identify (and optionally remove) invalid/missing polymorphic share targets; never commits."""
        bad_ids = []
        for share in (await db.execute(select(Share))).scalars().all():
            try:
                valid = await DriveService._share_target_entity(db, share)
            except ValueError:
                valid = None
            if not valid:
                bad_ids.append(share.id)
                if apply:
                    await db.delete(share)
        return bad_ids

    @staticmethod
    async def create_share(
        db: AsyncSession,
        owner_id: uuid.UUID,
        target_type: str,
        target_id: uuid.UUID,
        allow_download: bool = False,
        expires_days: Optional[int] = None,
        password_hash: Optional[str] = None,
        update_password: bool = False,
    ) -> Share:
        """Create a share link for folder, series, season, or video."""
        if target_type not in DriveService._SHARE_TARGET_TYPES:
            raise ValueError(f"نوع المشاركة غير مدعوم: {target_type}")
        candidate = Share(owner_id=owner_id, target_type=target_type, target_id=target_id, token="validation")
        resolved = await DriveService._share_target_entity(db, candidate)
        if not resolved:
            raise ValueError("المورد المطلوب مشاركته غير متاح")
        # Serialize creation for this target, including a target with no share yet.
        target = resolved[1]
        model = type(target)
        locked = await db.execute(select(model).where(model.id == target_id).with_for_update()
                                  .execution_options(populate_existing=True))
        if locked.scalar_one_or_none() is None or not await DriveService._share_target_entity(db, candidate):
            raise ValueError("المورد المطلوب مشاركته غير متاح")

        # Check existing active share for this target
        stmt = select(Share).where(
            Share.owner_id == owner_id,
            Share.target_type == target_type,
            Share.target_id == target_id,
            Share.revoked_at.is_(None),
            or_(Share.expires_at.is_(None), Share.expires_at > datetime.now(timezone.utc)),
        ).order_by(Share.created_at.desc(), Share.id.desc()).limit(1).with_for_update()
        res = await db.execute(stmt)
        existing = res.scalar_one_or_none()
        if existing and existing.is_active:
            # Update settings on existing
            existing.allow_download = allow_download
            existing.permission = "VIEW_AND_DOWNLOAD" if allow_download else "VIEW_ONLY"
            if update_password or password_hash is not None:
                existing.password_hash = password_hash
            if expires_days:
                existing.expires_at = datetime.now(timezone.utc) + timedelta(days=expires_days)
            await db.commit()
            return existing

        token = secrets.token_urlsafe(16)
        expires_at = None
        if expires_days and expires_days > 0:
            expires_at = datetime.now(timezone.utc) + timedelta(days=expires_days)

        share = Share(
            id=uuid.uuid4(),
            owner_id=owner_id,
            target_type=target_type,
            target_id=target_id,
            token=token,
            permission="VIEW_AND_DOWNLOAD" if allow_download else "VIEW_ONLY",
            allow_download=allow_download,
            password_hash=password_hash,
            expires_at=expires_at,
        )
        db.add(share)
        await db.commit()
        await db.refresh(share)
        return share

    @staticmethod
    async def revoke_share(db: AsyncSession, token: str) -> bool:
        """Revoke a share link immediately."""
        stmt = select(Share).where(Share.token == token)
        res = await db.execute(stmt)
        share = res.scalar_one_or_none()
        if not share:
            return False

        share.revoked_at = datetime.now(timezone.utc)
        await db.commit()
        return True

    @staticmethod
    async def get_share_by_token(db: AsyncSession, token: str) -> Optional[Share]:
        """Fetch an active share whose polymorphic target remains available."""
        stmt = select(Share).where(Share.token == token)
        res = await db.execute(stmt)
        share = res.scalar_one_or_none()
        if not share or not share.is_active:
            return None
        try:
            if not await DriveService._share_target_entity(db, share):
                return None
        except ValueError:
            return None
        return share

    @staticmethod
    async def get_shared_scope_contents(db: AsyncSession, share: Share) -> Dict[str, Any]:
        """
        Fetch isolated contents for an anonymous shared view.
        Strictly scoped to the shared resource without exposing any other user files.
        """
        resolved = await DriveService._share_target_entity(db, share)
        if not resolved:
            raise ValueError("المحتوى المشارك غير متاح")
        target_type, target = resolved
        target_id = target.id

        target_title = ""
        target_description = ""
        items: List[Dict[str, Any]] = []

        if target_type == "folder":
            folder = target
            target_title = folder.name
            for ep in await DriveService.get_share_scope_videos(db, share):
                effective_dl = await DriveService.get_effective_download_policy(db, ep)
                sub_label, sub_detail, font_count = get_video_subtitles_info(ep)
                items.append({
                    "id": str(ep.id), "visibility": ep.visibility.value, "title": ep.title, "episode_number": ep.episode_number,
                    "quality": get_video_quality(ep), "subtitles": sub_label, "subtitles_detail": sub_detail,
                    "font_count": font_count, "duration_formatted": format_duration(ep.source_duration_seconds),
                    "size_formatted": format_bytes(ep.source_size_bytes),
                    "can_download": share.allow_download and effective_dl, "share_token": share.token,
                })

        elif target_type == "season":
            season = target
            series = await db.get(Series, season.series_id)
            target_title = f"{series.title} — {season.display_name}" if series else season.display_name

            ep_stmt = (
                select(Video)
                .options(
                    selectinload(Video.renditions),
                    selectinload(Video.subtitles),
                    selectinload(Video.font_assets),
                )
                .where(Video.season_id == target_id, Video.trashed_at.is_(None))
                .order_by(asc(Video.episode_number), asc(Video.title))
            )
            ep_res = await db.execute(ep_stmt)
            for ep in [ep for ep in ep_res.scalars().all() if await DriveService._is_visible_share_video(db, ep, share.owner_id)]:
                effective_dl = await DriveService.get_effective_download_policy(db, ep)
                # Share allow_download AND video effective download policy must BOTH be true
                can_download = share.allow_download and effective_dl
                sub_label, sub_detail, font_count = get_video_subtitles_info(ep)
                quality = get_video_quality(ep)

                items.append({
                    "id": str(ep.id), "visibility": ep.visibility.value,
                    "title": ep.title,
                    "episode_number": ep.episode_number,
                    "quality": quality,
                    "subtitles": sub_label,
                    "subtitles_detail": sub_detail,
                    "font_count": font_count,
                    "duration_formatted": format_duration(ep.source_duration_seconds),
                    "size_formatted": format_bytes(ep.source_size_bytes),
                    "can_download": can_download,
                    "share_token": share.token,
                })

        elif target_type == "series":
            series = target
            target_title = series.title
            target_description = series.description or ""

            # Fetch all episodes across all seasons in this series
            ep_stmt = (
                select(Video)
                .options(
                    selectinload(Video.renditions),
                    selectinload(Video.subtitles),
                    selectinload(Video.font_assets),
                    selectinload(Video.season),
                )
                .where(Video.series_id == target_id, Video.trashed_at.is_(None))
                .order_by(asc(Video.season_number), asc(Video.episode_number))
            )
            ep_res = await db.execute(ep_stmt)
            for ep in [ep for ep in ep_res.scalars().all() if await DriveService._is_visible_share_video(db, ep, share.owner_id)]:
                effective_dl = await DriveService.get_effective_download_policy(db, ep)
                can_download = share.allow_download and effective_dl
                sub_label, sub_detail, font_count = get_video_subtitles_info(ep)
                quality = get_video_quality(ep)

                items.append({
                    "id": str(ep.id), "visibility": ep.visibility.value,
                    "title": ep.title,
                    "season_name": ep.season.display_name if ep.season else None,
                    "episode_number": ep.episode_number,
                    "quality": quality,
                    "subtitles": sub_label,
                    "subtitles_detail": sub_detail,
                    "font_count": font_count,
                    "duration_formatted": format_duration(ep.source_duration_seconds),
                    "size_formatted": format_bytes(ep.source_size_bytes),
                    "can_download": can_download,
                    "share_token": share.token,
                })

        elif target_type == "video":
            # Load the scoped video's media relationships before serializing.
            # Lazy loading them here cannot run inside an async request.
            video = await DriveService.get_shared_video_if_available(db, share, target.id)
            if video is None:
                raise ValueError("المحتوى المشارك غير متاح")
            target_title = video.title
            effective_dl = await DriveService.get_effective_download_policy(db, video)
            can_download = share.allow_download and effective_dl
            sub_label, sub_detail, font_count = get_video_subtitles_info(video)

            items.append({
                "id": str(video.id), "visibility": video.visibility.value,
                "title": video.title,
                "episode_number": video.episode_number,
                "quality": get_video_quality(video),
                "subtitles": sub_label,
                "subtitles_detail": sub_detail,
                "font_count": font_count,
                "duration_formatted": format_duration(video.source_duration_seconds),
                "size_formatted": format_bytes(video.source_size_bytes),
                "can_download": can_download,
                "share_token": share.token,
            })

        return {
            "token": share.token,
            "target_type": target_type,
            "target_id": str(target_id),
            "target_title": target_title,
            "target_description": target_description,
            "allow_download": share.allow_download,
            "items": items,
            "total_items": len(items),
        }

    # ==========================================
    # MEDIA AUTO-DETECTION AND BATCH IMPORT
    # ==========================================
    @staticmethod
    async def detect_media(
        db: AsyncSession,
        video_ids: Optional[List[uuid.UUID]] = None,
        scope_id: Optional[str] = None,
        owner_id: uuid.UUID | None = None,
    ) -> List[Dict[str, Any]]:
        """Run deterministic media auto-detection on specified or scope videos."""
        from app.services.media_detector import parse_filename_metadata

        video_stmt = select(Video).where(Video.trashed_at.is_(None))
        if owner_id is not None:
            video_stmt = video_stmt.where(Video.user_id == owner_id)
        if video_ids:
            all_requested = (await db.execute(select(Video).where(Video.id.in_(video_ids)))).scalars().all()
            if len(all_requested) != len(set(video_ids)) or any(v.trashed_at is not None or (owner_id is not None and v.user_id != owner_id) for v in all_requested):
                raise ValueError("المحتوى المطلوب غير متاح للمستخدم الحالي")
            video_stmt = video_stmt.where(Video.id.in_(video_ids))
        elif scope_id and scope_id not in ("root", "uncategorized"):
            try:
                scope_uuid = uuid.UUID(scope_id)
                folder = await db.get(Folder, scope_uuid)
                series = await db.get(Series, scope_uuid) if not folder else None
                season = await db.get(Season, scope_uuid) if not folder and not series else None
                if season:
                    series = await db.get(Series, season.series_id)
                target = folder or season or series
                target_owner = folder.user_id if folder else series.owner_id if series else None
                if not target or target.trashed_at is not None or (series and series.trashed_at is not None) or (owner_id is not None and target_owner != owner_id):
                    raise ValueError("نطاق الاكتشاف غير متاح للمستخدم الحالي")
                video_stmt = video_stmt.where(
                    or_(
                        Video.folder_id == scope_uuid,
                        Video.series_id == scope_uuid,
                        Video.season_id == scope_uuid,
                    )
                )
            except ValueError as exc:
                raise ValueError("نطاق الاكتشاف غير متاح للمستخدم الحالي") from exc
        else:
            # Detect on uncategorized / root videos
            video_stmt = video_stmt.where(
                and_(
                    Video.folder_id.is_(None),
                    Video.series_id.is_(None),
                    Video.season_id.is_(None),
                )
            )

        video_stmt = video_stmt.order_by(Video.created_at.desc()).limit(100)
        res = await db.execute(video_stmt)
        videos = res.scalars().all()

        results = []
        for v in videos:
            context = {}
            if v.series_name:
                context["series_name"] = v.series_name
            parsed = parse_filename_metadata(v.source_filename, folder_context=context)

            results.append({
                "video_id": str(v.id),
                "current_title": v.title,
                "filename": v.source_filename,
                "detected": parsed,
            })

        return results

    @staticmethod
    async def apply_media_detection(
        db: AsyncSession,
        user_id: uuid.UUID,
        assignments: List[Dict[str, Any]],
        parent_folder_id: Optional[uuid.UUID] = None,
        is_admin: bool = False,
    ) -> Dict[str, Any]:
        """
        Apply reviewed auto-detection assignments:
        Creates Series / Season if needed and updates Video metadata without touching physical storage.
        """
        scope = None if is_admin else user_id
        if parent_folder_id:
            parent = await db.get(Folder, parent_folder_id)
            if not parent or parent.trashed_at is not None or (scope is not None and parent.user_id != scope):
                raise ValueError("المجلد الهدف غير متاح")
        # Validate every item before applying the first mutation.
        validated = {}
        for item in assignments:
            try:
                vid = uuid.UUID(str(item.get("video_id", "")))
            except (ValueError, TypeError) as exc:
                raise ValueError("معرف فيديو غير صالح") from exc
            video = await db.get(Video, vid)
            if not video or video.trashed_at is not None or (scope is not None and video.user_id != scope):
                raise ValueError("الفيديو غير متاح للمستخدم الحالي")
            for field in ("season_number", "episode_number"):
                if item.get(field) is not None and int(item[field]) < 0:
                    raise ValueError("رقم الموسم أو الحلقة غير صالح")
            validated[vid] = video
        series_cache: Dict[str, Series] = {}
        season_cache: Dict[Tuple[uuid.UUID, int], Season] = {}
        applied_count = 0

        for item in assignments:
            video_id_str = item.get("video_id")
            if not video_id_str:
                continue
            try:
                v_uuid = uuid.UUID(video_id_str)
            except ValueError:
                continue

            video = await db.get(Video, v_uuid)
            if not video:
                continue

            series_name = (item.get("series_name") or "").strip()
            season_num = item.get("season_number")
            episode_num = item.get("episode_number")
            suggested_title = (item.get("suggested_title") or "").strip()

            series_entity = None
            if series_name:
                if series_name in series_cache:
                    series_entity = series_cache[series_name]
                else:
                    # Check if series exists in DB
                    st_stmt = select(Series).where(Series.title == series_name, Series.owner_id == user_id, Series.trashed_at.is_(None))
                    st_res = await db.execute(st_stmt)
                    series_entity = st_res.scalar_one_or_none()
                    if not series_entity:
                        series_entity = Series(
                            id=uuid.uuid4(),
                            owner_id=user_id,
                            parent_folder_id=parent_folder_id,
                            title=series_name,
                            allow_download_policy="INHERIT",
                        )
                        db.add(series_entity)
                        await db.flush()
                    series_cache[series_name] = series_entity

            season_entity = None
            if series_entity and season_num is not None:
                key = (series_entity.id, int(season_num))
                if key in season_cache:
                    season_entity = season_cache[key]
                else:
                    sn_stmt = select(Season).where(
                        Season.series_id == series_entity.id,
                        Season.season_number == int(season_num),
                    )
                    sn_res = await db.execute(sn_stmt)
                    season_entity = sn_res.scalar_one_or_none()
                    if not season_entity:
                        season_entity = Season(
                            id=uuid.uuid4(),
                            series_id=series_entity.id,
                            season_number=int(season_num),
                            display_name=f"الموسم {season_num}",
                            sort_order=int(season_num),
                            allow_download_policy="INHERIT",
                        )
                        db.add(season_entity)
                        await db.flush()
                    season_cache[key] = season_entity

            # Apply to video
            if suggested_title:
                video.title = suggested_title
            if episode_num is not None:
                video.episode_number = int(episode_num)
            if season_num is not None:
                video.season_number = int(season_num)

            if series_entity:
                video.series_id = series_entity.id
                video.series_name = series_entity.title
            if season_entity:
                video.season_id = season_entity.id
            if parent_folder_id:
                video.folder_id = parent_folder_id

            video.updated_at = datetime.now(timezone.utc)
            applied_count += 1

        await db.commit()
        return {"success": True, "applied_count": applied_count}


    @staticmethod
    async def _get_trash_page(
        db: AsyncSession,
        user_id: Optional[uuid.UUID],
        search_query: Optional[str],
        page: int,
        limit: int,
        trash_type: Optional[str],
        sort: str,
        direction: str,
    ) -> Dict[str, Any]:
        """Return one database-paged, globally ordered page of mixed Trash entities."""
        pattern = f"%{search_query.strip()}%" if search_query and search_query.strip() else None

        folder_stmt = select(
            literal("folder").label("kind"), Folder.id.label("id"),
            Folder.name.label("name"), Folder.trashed_at.label("trashed_at"),
        ).where(Folder.trashed_at.is_not(None))
        if user_id:
            folder_stmt = folder_stmt.where(Folder.user_id == user_id)
        if pattern:
            folder_stmt = folder_stmt.where(Folder.name.ilike(pattern))

        series_stmt = select(
            literal("series").label("kind"), Series.id.label("id"),
            Series.title.label("name"), Series.trashed_at.label("trashed_at"),
        ).where(Series.trashed_at.is_not(None))
        if user_id:
            series_stmt = series_stmt.where(Series.owner_id == user_id)
        if pattern:
            series_stmt = series_stmt.where(Series.title.ilike(pattern))

        season_stmt = select(
            literal("season").label("kind"), Season.id.label("id"),
            Season.display_name.label("name"), Season.trashed_at.label("trashed_at"),
        ).where(Season.trashed_at.is_not(None))
        if user_id:
            season_stmt = season_stmt.join(Series, Season.series_id == Series.id).where(Series.owner_id == user_id)
        if pattern:
            season_stmt = season_stmt.where(Season.display_name.ilike(pattern))

        video_stmt = select(
            literal("video").label("kind"), Video.id.label("id"),
            Video.title.label("name"), Video.trashed_at.label("trashed_at"),
        ).where(Video.trashed_at.is_not(None))
        if user_id:
            video_stmt = video_stmt.where(Video.user_id == user_id)
        if pattern:
            video_stmt = video_stmt.where(or_(Video.title.ilike(pattern), Video.source_filename.ilike(pattern)))

        union = union_all(folder_stmt, series_stmt, season_stmt, video_stmt).subquery("trash_entities")
        if trash_type:
            union_query = select(union).where(union.c.kind == trash_type)
        else:
            union_query = select(union)
        total = int(await db.scalar(select(func.count()).select_from(union_query.subquery())) or 0)

        sort_columns = {"trashed_at": union.c.trashed_at, "name": union.c.name, "type": union.c.kind}
        ordering = asc if direction == "asc" else desc
        rows = (await db.execute(
            union_query.order_by(ordering(sort_columns[sort]).nulls_last(), asc(union.c.kind), asc(union.c.id))
            .offset((page - 1) * limit).limit(limit)
        )).all()
        page_ids: dict[str, set[uuid.UUID]] = {kind: set() for kind in ("folder", "series", "season", "video")}
        for row in rows:
            page_ids[row.kind].add(row.id)

        async def load_entities(model: Any, ids: set[uuid.UUID]) -> dict[uuid.UUID, Any]:
            if not ids:
                return {}
            entities = (await db.execute(select(model).where(model.id.in_(ids)))).scalars().all()
            return {entity.id: entity for entity in entities}

        folders = await load_entities(Folder, page_ids["folder"])
        series = await load_entities(Series, page_ids["series"])
        seasons = await load_entities(Season, page_ids["season"])
        videos = await load_entities(Video, page_ids["video"])

        folder_parent_ids = {item.parent_id for item in folders.values() if item.parent_id}
        folder_parent_ids.update(item.parent_folder_id for item in series.values() if item.parent_folder_id)
        folder_parent_ids.update(item.folder_id for item in videos.values() if item.folder_id)
        series_parent_ids = {item.series_id for item in seasons.values()}
        series_parent_ids.update(item.series_id for item in videos.values() if item.series_id)
        season_parent_ids = {item.season_id for item in videos.values() if item.season_id}

        async def load_names(model: Any, ids: set[uuid.UUID], field: str) -> dict[uuid.UUID, str]:
            if not ids:
                return {}
            return {row.id: row.name for row in await db.execute(select(model.id, field.label("name")).where(model.id.in_(ids)))}

        folder_names = await load_names(Folder, folder_parent_ids, Folder.name)
        series_names = await load_names(Series, series_parent_ids, Series.title)
        season_names = await load_names(Season, season_parent_ids, Season.display_name)

        series_season_counts: dict[uuid.UUID, int] = {}
        series_video_counts: dict[uuid.UUID, int] = {}
        season_video_counts: dict[uuid.UUID, int] = {}
        if page_ids["series"]:
            series_season_counts = dict((await db.execute(
                select(Season.series_id, func.count(Season.id)).where(Season.series_id.in_(page_ids["series"]))
                .group_by(Season.series_id)
            )).all())
            series_video_counts = dict((await db.execute(
                select(Video.series_id, func.count(Video.id)).where(Video.series_id.in_(page_ids["series"]))
                .group_by(Video.series_id)
            )).all())
        if page_ids["season"]:
            season_video_counts = dict((await db.execute(
                select(Video.season_id, func.count(Video.id)).where(Video.season_id.in_(page_ids["season"]))
                .group_by(Video.season_id)
            )).all())

        trashed_items: List[Dict[str, Any]] = []
        for row in rows:
            kind = row.kind
            if kind == "folder":
                entity = folders[row.id]
                parent_name = folder_names.get(entity.parent_id, "مكتبة المحتوى")
                summary = await DriveService.get_descendant_summary(db, "folder", entity.id, user_id)
                subfolders = summary.get("folders_count", 0)
                videos_count = summary.get("videos_count", 0)
                series_count = summary.get("series_count", 0)
                items_count = f"{subfolders} مجلدات · {videos_count} فيديوهات" if subfolders else f"{videos_count} فيديوهات"
                if series_count:
                    items_count += f" · {series_count} مسلسلات مرتبطة"
                type_label, size_formatted, size_bytes = "مجلد", "—", 0
            elif kind == "series":
                entity = series[row.id]
                parent_name = folder_names.get(entity.parent_folder_id, "مكتبة المحتوى")
                items_count = f"{series_season_counts.get(entity.id, 0)} مواسم · {series_video_counts.get(entity.id, 0)} حلقة"
                type_label, size_formatted, size_bytes = "مسلسل", "—", 0
            elif kind == "season":
                entity = seasons[row.id]
                parent_name = series_names.get(entity.series_id, "مسلسل")
                items_count = f"{season_video_counts.get(entity.id, 0)} حلقة"
                type_label, size_formatted, size_bytes = "موسم", "—", 0
            else:
                entity = videos[row.id]
                parent_name = "مكتبة المحتوى"
                if entity.season_id:
                    parent_name = season_names.get(entity.season_id, "مكتبة المحتوى")
                elif entity.series_id:
                    parent_name = series_names.get(entity.series_id, "مكتبة المحتوى")
                elif entity.folder_id:
                    parent_name = folder_names.get(entity.folder_id, "مكتبة المحتوى")
                items_count = format_duration(entity.source_duration_seconds)
                type_label = "حلقة" if entity.episode_number is not None else "فيديو"
                size_formatted = format_bytes(entity.source_size_bytes)
                size_bytes = entity.source_size_bytes or 0

            trashed_items.append({
                "id": str(entity.id), "type": kind, "type_label": type_label,
                "name": row.name, "previous_location": parent_name,
                "size_formatted": size_formatted, "size_bytes": size_bytes,
                "trashed_at": row.trashed_at.strftime("%Y-%m-%d %H:%M") if row.trashed_at else "",
                "items_count": items_count,
            })

        return {
            "current_scope": {"id": "trash", "type": "trash", "name": "سلة المحذوفات", "parent_id": "root", "allow_download_policy": "DENY"},
            "breadcrumbs": [
                {"id": "root", "type": "root", "name": "مكتبة المحتوى", "siblings": []},
                {"id": "trash", "type": "trash", "name": "سلة المحذوفات", "siblings": []},
            ],
            "folders": [], "series": [], "seasons": [], "videos": [],
            "trash_items": trashed_items, "items": trashed_items,
            "total_items": total, "total": total, "page": page, "limit": limit,
            "pages": (total + limit - 1) // limit,
        }
