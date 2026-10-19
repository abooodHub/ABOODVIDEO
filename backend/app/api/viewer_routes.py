"""ABOODVIDEO Public Viewer & Safe Live Preview Routes (V1).

Serves the protected library and explicit sharing URLs.
Catalog rendering helpers are retained for internal tooling, without HTTP routes.

Strictly preserves Player V2, HLS, JASSUB, and security policies.
"""
import html as html_lib
import re
import uuid
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from fastapi import APIRouter, Request, Depends, Form, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from sqlalchemy import select, and_, or_, desc, asc, func, cast, String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
import jinja2

from app.core.config import settings
from app.core.database import get_db
from app.core.redis import get_redis
from app.security.share_access import has_share_access, require_share_access, unlock_share
from app.services.download_names import build_download_filename
from app.core.logging import get_logger
from app.models.video import (
    Video,
    VideoStatus,
    VideoVisibility,
    VideoRendition,
    VideoSubtitle,
)
from app.models.user import User, UserRole
from app.security.auth import get_optional_current_user, require_owner_or_admin

logger = get_logger("viewer_routes")
router = APIRouter(include_in_schema=False)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=True,
)


def format_duration(seconds: Optional[float]) -> str:
    """Format duration into HH:MM:SS or MM:SS."""
    if not seconds or seconds <= 0:
        return ""
    total_sec = int(seconds)
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    secs = total_sec % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def parse_natural_episode_number(video: Any) -> float:
    """
    Extract natural float episode number supporting decimals (e.g., 01, 02, 06, 06.5, 07).
    Works on Video model instance or dictionary.
    """
    title = ""
    ep_num = None
    if isinstance(video, dict):
        ep_num = video.get("episode_number")
        title = video.get("title") or ""
    else:
        ep_num = getattr(video, "episode_number", None)
        title = getattr(video, "title", "") or ""

    # Check for decimal episode in title first (e.g. 06.5, 6.5, Episode 06.5, الحلقة 06.5)
    match = re.search(r'(?:الحلقة|الحلقه|EP|Episode|E)[\s_.:-]*([0-9]+(?:\.[0-9]+)?)', title, re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass

    # Decimal anywhere in title (e.g. "Spirit Cage 06.5")
    match_dec = re.search(r'\b([0-9]+\.[0-9]+)\b', title)
    if match_dec:
        try:
            return float(match_dec.group(1))
        except ValueError:
            pass

    if ep_num is not None:
        try:
            return float(ep_num)
        except (ValueError, TypeError):
            pass

    # Generic integer match in title
    match_int = re.search(r'\b([0-9]+)\b', title)
    if match_int:
        try:
            return float(match_int.group(1))
        except ValueError:
            pass

    return 0.0


def format_natural_episode_number(num: float) -> str:
    """Format episode number naturally (01, 02, 06, 06.5, 07)."""
    if num <= 0:
        return ""
    if num.is_integer():
        return f"{int(num):02d}"
    int_part = int(num)
    frac_part = str(num).split(".")[1]
    return f"{int_part:02d}.{frac_part}"


def format_season_number(num: Optional[int]) -> str:
    """Format season number naturally (Season 01, Season 02)."""
    if not num:
        return "Season 01"
    return f"Season {num:02d}"


def resolve_poster_disk_path(video: Video) -> Tuple[Optional[Path], Optional[str]]:
    """
    Safely resolves the physical disk path and Nginx internal redirect path for a video poster.
    Returns (disk_path, nginx_subpath) or (None, None).
    Handles both relative 'generations/...' and 'videos/<id>/...' paths.
    """
    if not video or not video.poster_path:
        return None, None

    clean_path = video.poster_path.strip("/")
    if clean_path.startswith("videos/"):
        clean_path = clean_path[len("videos/"):]
    vid_str = str(video.id)
    if clean_path.startswith(f"{vid_str}/"):
        clean_path = clean_path[len(f"{vid_str}/"):]

    base_dir = (Path(settings.STORAGE_VIDEOS_PATH) / vid_str).resolve()
    target_path = (base_dir / clean_path).resolve()

    if not target_path.is_relative_to(base_dir) or not target_path.is_file():
        return None, None

    nginx_internal_path = f"/internal_media/{vid_str}/{clean_path}"
    return target_path, nginx_internal_path


def generate_deterministic_svg_poster(title: str, is_4k: bool = False, max_quality: Optional[str] = None) -> bytes:
    """Generate a sleek, deterministic SVG poster when physical WebP is unavailable."""
    escaped_title = html_lib.escape(title[:38])
    badge_svg = ""
    if is_4k:
        badge_svg = '<rect x="24" y="24" width="56" height="26" rx="4" fill="#064e3b" stroke="#10b981" stroke-width="1.5"/><text x="52" y="42" font-family="system-ui, sans-serif" font-size="13" font-weight="800" fill="#34d399" text-anchor="middle">4K</text>'
    elif max_quality:
        badge_svg = f'<rect x="24" y="24" width="62" height="26" rx="4" fill="#0c4a6e" stroke="#0ea5e9" stroke-width="1.5"/><text x="55" y="42" font-family="system-ui, sans-serif" font-size="13" font-weight="800" fill="#38bdf8" text-anchor="middle">{html_lib.escape(max_quality)}</text>'

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 600" width="400" height="600">
  <defs>
    <linearGradient id="bg_grad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#111c2e"/>
      <stop offset="50%" stop-color="#090e17"/>
      <stop offset="100%" stop-color="#05080f"/>
    </linearGradient>
    <radialGradient id="card_glow" cx="50%" cy="42%" r="48%">
      <stop offset="0%" stop-color="#1e3a5f" stop-opacity="0.45"/>
      <stop offset="100%" stop-color="#090e17" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="400" height="600" fill="url(#bg_grad)"/>
  <circle cx="200" cy="270" r="180" fill="url(#card_glow)"/>
  {badge_svg}
  <g transform="translate(160, 230)">
    <circle cx="40" cy="40" r="38" fill="#132238" stroke="#334155" stroke-width="2"/>
    <polygon points="34,26 56,40 34,54" fill="#3b82f6"/>
  </g>
  <text x="200" y="360" font-family="system-ui, Segoe UI, sans-serif" font-size="16" font-weight="700" fill="#e2e8f0" text-anchor="middle" direction="rtl">{escaped_title}</text>
  <text x="200" y="390" font-family="system-ui, Segoe UI, sans-serif" font-size="12" font-weight="600" fill="#64748b" text-anchor="middle">ABOODVIDEO MEDIA</text>
</svg>"""
    return svg.encode("utf-8")


def get_nav_prefix(request: Request) -> str:
    """Detect if current request is routed through /preview."""
    if request.url.path.startswith("/preview"):
        return "/preview"
    return ""


async def require_library_manager_page(
    request: Request,
    db: AsyncSession,
) -> Optional[RedirectResponse]:
    """Redirect anonymous or non-management users away from private library pages."""
    user = await get_optional_current_user(request=request, db=db)
    if user and user.role in (UserRole.OWNER, UserRole.ADMIN):
        return None
    return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)


def serialize_viewer_video(video: Video, nav_prefix: str = "") -> Dict[str, Any]:
    """Transform Video model into safe public viewer dictionary with verified poster status."""
    is_4k = False
    max_quality = "HD"
    if video.renditions:
        for r in video.renditions:
            if r.quality_label in ("2160p", "4K") or (r.resolution_height and r.resolution_height >= 2160):
                is_4k = True
                max_quality = "4K"
                break
            elif r.quality_label == "1440p" and max_quality != "4K":
                max_quality = "1440p"
            elif r.quality_label == "1080p" and max_quality not in ("4K", "1440p"):
                max_quality = "1080p"
            elif r.quality_label == "720p" and max_quality not in ("4K", "1440p", "1080p"):
                max_quality = "720p"

    has_ass = False
    subtitle_langs = []
    if video.subtitles:
        for s in video.subtitles:
            if s.language_name and s.language_name not in subtitle_langs:
                subtitle_langs.append(s.language_name)
            elif s.label and s.label not in subtitle_langs:
                subtitle_langs.append(s.label)
            if (s.format or "").lower() in ("ass", "ssa") or s.playback_renderer == "libass":
                has_ass = True

    resolution_str = ""
    if video.source_resolution_width and video.source_resolution_height:
        resolution_str = f"{video.source_resolution_width}×{video.source_resolution_height}"

    # Check real disk file for poster
    disk_path, _ = resolve_poster_disk_path(video)
    has_real_poster = (disk_path is not None and disk_path.is_file())

    poster_slug = video.share_id or str(video.id)
    poster_url = f"{nav_prefix}/poster/{poster_slug}.webp"

    ep_float = parse_natural_episode_number(video)
    ep_formatted = format_natural_episode_number(ep_float) if ep_float > 0 else (str(video.episode_number) if video.episode_number else "")
    season_num = video.season_number or 1
    season_formatted = format_season_number(season_num)

    return {
        "id": str(video.id),
        "title": video.title,
        "description": video.description or "",
        "category": video.category or "فيديو",
        "series_name": video.series_name,
        "season_number": video.season_number,
        "season_number_formatted": season_formatted,
        "episode_number": video.episode_number,
        "episode_number_formatted": ep_formatted,
        "natural_episode_number": ep_float,
        "duration_seconds": video.source_duration_seconds or 0,
        "duration_formatted": format_duration(video.source_duration_seconds),
        "resolution_str": resolution_str,
        "share_id": video.share_id or str(video.id),
        "has_poster": has_real_poster,
        "poster_url": poster_url,
        "is_4k": is_4k,
        "max_quality": max_quality,
        "has_ass": has_ass,
        "subtitle_languages": subtitle_langs,
        "created_at": video.created_at,
        "view_count": video.view_count or 0,
    }


def render_viewer_template(template_name: str, *, status_code: int = 200, **context) -> HTMLResponse:
    """Render a viewer Jinja2 template with security headers."""
    if template_name == "viewer_error.html":
        return HTMLResponse(content="", status_code=status_code if status_code >= 400 else 404,
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    try:
        template = jinja_env.get_template(template_name)
        content = template.render(**context)
    except Exception as e:
        logger.error(f"Failed to render template {template_name}: {e}")
        return HTMLResponse(f"Template render error: {e}", status_code=500)

    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
    return HTMLResponse(content, status_code=status_code, headers=headers)


async def viewer_root_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Root page routing:
    If requested under /preview, serves the Private Video Drive & Media File Manager (requires owner/admin).
    Other mount points remain hidden.
    """
    path = request.url.path.rstrip("/")
    if path == "/preview":
        redirect = await require_library_manager_page(request, db)
        if redirect:
            return redirect
        return render_viewer_template("drive.html")

    return Response(status_code=404)


async def viewer_public_home(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Serve public showcase home page."""
    stmt = (
        select(Video)
        .options(selectinload(Video.renditions), selectinload(Video.subtitles))
        .where(
            Video.status == VideoStatus.READY,
            Video.trashed_at.is_(None),
            Video.active_generation_id.isnot(None),
            Video.share_revoked == False,
            Video.visibility.in_([VideoVisibility.PUBLIC, VideoVisibility.UNLISTED]),
        )
        .order_by(Video.created_at.desc())
        .limit(16)
    )
    res = await db.execute(stmt)
    vids = [serialize_viewer_video(v, get_nav_prefix(request)) for v in res.scalars().all()]
    featured = vids[0] if vids else None
    uhd = [v for v in vids if v.get("is_4k")]
    series_vids = [v for v in vids if v.get("series_name")]
    return render_viewer_template(
        "home.html",
        featured=featured,
        latest_videos=vids,
        series_videos=series_vids,
        uhd_videos=uhd,
        total_count=len(vids),
        prefix=get_nav_prefix(request),
        active_nav="home",
    )


async def viewer_public_browse(
    request: Request,
    q: Optional[str] = Query(None, description="بحث"),
    type: Optional[str] = Query(None, description="النوع"),
    quality: Optional[str] = Query(None, description="الجودة"),
    sub: Optional[str] = Query(None, description="الترجمة"),
    sort: Optional[str] = Query("latest", description="الترتيب"),
    db: AsyncSession = Depends(get_db),
):
    """Serve public browse catalog with live search, series, quality, and subtitle filters."""
    stmt = (
        select(Video)
        .options(selectinload(Video.renditions), selectinload(Video.subtitles))
        .where(
            Video.status == VideoStatus.READY,
            Video.trashed_at.is_(None),
            Video.active_generation_id.isnot(None),
            Video.share_revoked == False,
            Video.visibility.in_([VideoVisibility.PUBLIC, VideoVisibility.UNLISTED]),
        )
    )
    if q and q.strip():
        search_term = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Video.title.ilike(search_term),
                Video.series_name.ilike(search_term),
                Video.description.ilike(search_term),
            )
        )
    if type == "series":
        stmt = stmt.where(Video.series_name.isnot(None))

    if sort == "title":
        stmt = stmt.order_by(Video.title.asc())
    elif sort == "duration":
        stmt = stmt.order_by(Video.source_duration_seconds.desc().nullslast())
    else:  # latest
        stmt = stmt.order_by(Video.created_at.desc())

    stmt = stmt.limit(50)
    res = await db.execute(stmt)
    raw_vids = res.scalars().all()
    nav_prefix = get_nav_prefix(request)
    vids = [serialize_viewer_video(v, nav_prefix) for v in raw_vids]

    # Post-filter by quality and subtitle
    if quality:
        q_upper = quality.upper()
        if q_upper == "4K":
            vids = [v for v in vids if v.get("is_4k") or v.get("max_quality") in ("4K", "2160p")]
        elif q_upper in ("1440P", "2K"):
            vids = [v for v in vids if v.get("max_quality") in ("1440p", "2K", "4K")]
        elif q_upper in ("1080P", "FHD"):
            vids = [v for v in vids if v.get("max_quality") in ("1080p", "1440p", "4K")]
        elif q_upper in ("720P", "HD"):
            vids = [v for v in vids if v.get("max_quality") in ("720p", "1080p", "1440p", "4K")]

    if sub:
        sub_lower = sub.lower()
        if sub_lower == "ass":
            vids = [v for v in vids if v.get("has_ass")]
        elif sub_lower == "vtt":
            vids = [v for v in vids if v.get("subtitle_languages") and not v.get("has_ass")]

    return render_viewer_template(
        "browse.html",
        videos=vids,
        search_query=q or "",
        type_filter=type or "",
        quality_filter=quality or "",
        sub_filter=sub or "",
        sort_by=sort or "latest",
        prefix=nav_prefix,
        active_nav="series" if type == "series" else "library",
    )


async def viewer_series_page(
    series_name: str,
    request: Request,
    season: Optional[int] = Query(1, description="رقم الموسم"),
    db: AsyncSession = Depends(get_db),
):
    """Series Details View: structurally strong header, season tabs, compact 16:9 episode cards."""
    prefix = get_nav_prefix(request)

    stmt = (
        select(Video)
        .options(
            selectinload(Video.renditions),
            selectinload(Video.subtitles),
        )
        .where(
            Video.series_name == series_name,
            Video.status == VideoStatus.READY,
            Video.active_generation_id.isnot(None),
            Video.share_revoked == False,
            Video.visibility.in_([VideoVisibility.PUBLIC, VideoVisibility.UNLISTED]),
        )
    )
    result = await db.execute(stmt)
    videos_raw = result.scalars().all()

    if not videos_raw:
        # Fallback search by title if series_name column is unpopulated
        term = f"%{series_name}%"
        stmt_fallback = (
            select(Video)
            .options(selectinload(Video.renditions), selectinload(Video.subtitles))
            .where(
                Video.title.ilike(term),
                Video.status == VideoStatus.READY,
                Video.active_generation_id.isnot(None),
                Video.share_revoked == False,
                Video.visibility.in_([VideoVisibility.PUBLIC, VideoVisibility.UNLISTED]),
            )
            .order_by(Video.created_at.asc())
        )
        res_fallback = await db.execute(stmt_fallback)
        videos_raw = res_fallback.scalars().all()

    if not videos_raw:
        return render_viewer_template("viewer_error.html", prefix=prefix, error_title="المسلسل غير موجود", error_message="عذراً، هذا المسلسل غير متاح.")

    all_episodes = [serialize_viewer_video(v, nav_prefix=prefix) for v in videos_raw]

    # Group by Season
    seasons_map: Dict[int, List[Dict[str, Any]]] = {}
    for ep in all_episodes:
        s_num = ep["season_number"] or 1
        if s_num not in seasons_map:
            seasons_map[s_num] = []
        seasons_map[s_num].append(ep)

    # Sort episodes naturally within each season: 01, 02, 06, 06.5, 07
    for s_num in seasons_map:
        seasons_map[s_num].sort(key=lambda ep: (ep.get("natural_episode_number", 0.0), ep.get("created_at") or 0))

    available_seasons = sorted(seasons_map.keys())
    current_season = season if season in seasons_map else (available_seasons[0] if available_seasons else 1)
    season_episodes = seasons_map.get(current_season, [])

    # Series lead item
    lead_item = all_episodes[0]
    for ep in all_episodes:
        if ep["has_poster"]:
            lead_item = ep
            break

    return render_viewer_template(
        "series.html",
        prefix=prefix,
        active_nav="series",
        series_name=series_name,
        lead_item=lead_item,
        available_seasons=available_seasons,
        current_season=current_season,
        episodes=season_episodes,
        total_episodes_count=len(all_episodes),
    )


async def viewer_title_detail_page(
    video_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Title details view: technical specifications, story, and direct playback."""
    prefix = get_nav_prefix(request)

    # Try UUID lookup, fallback to share_id
    try:
        vid_uuid = uuid.UUID(video_id)
        id_clause = or_(Video.id == vid_uuid, Video.share_id == video_id)
    except Exception:
        id_clause = or_(cast(Video.id, String) == video_id, Video.share_id == video_id)

    stmt = (
        select(Video)
        .options(
            selectinload(Video.renditions),
            selectinload(Video.subtitles),
        )
        .where(
            id_clause,
            Video.status == VideoStatus.READY,
            Video.active_generation_id.isnot(None),
            Video.share_revoked == False,
            Video.visibility.in_([VideoVisibility.PUBLIC, VideoVisibility.UNLISTED]),
        )
    )
    result = await db.execute(stmt)
    raw_video = result.scalar_one_or_none()

    if not raw_video:
        return render_viewer_template("viewer_error.html", prefix=prefix, error_title="المحتوى غير موجود", error_message="عذراً، هذا الفيديو غير متاح.")

    video = serialize_viewer_video(raw_video, nav_prefix=prefix)

    # Fetch related videos
    related_stmt = (
        select(Video)
        .options(selectinload(Video.renditions), selectinload(Video.subtitles))
        .where(
            Video.id != raw_video.id,
            Video.status == VideoStatus.READY,
            Video.active_generation_id.isnot(None),
            Video.share_revoked == False,
            Video.visibility.in_([VideoVisibility.PUBLIC, VideoVisibility.UNLISTED]),
        )
    )
    if raw_video.series_name:
        related_stmt = related_stmt.where(Video.series_name == raw_video.series_name)

    related_stmt = related_stmt.limit(6)
    related_res = await db.execute(related_stmt)
    related_videos = [serialize_viewer_video(r, nav_prefix=prefix) for r in related_res.scalars().all()]

    return render_viewer_template(
        "title_detail.html",
        prefix=prefix,
        active_nav="library",
        video=video,
        related_videos=related_videos,
    )


@router.get("/poster/{share_id}.webp")
async def serve_public_poster(
    share_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_owner_or_admin),
):
    """
    Safely deliver public video poster WebP asset.
    If physical WebP exists on disk: delivers via Nginx X-Accel-Redirect.
    If physical WebP is missing: delivers a deterministic SVG poster fallback (HTTP 200).
    NO BROKEN IMAGE ICONS EVER.
    """
    try:
        vid_uuid = uuid.UUID(share_id)
        match_clause = or_(Video.share_id == share_id, Video.id == vid_uuid)
    except Exception:
        match_clause = or_(Video.share_id == share_id, cast(Video.id, String) == share_id)

    stmt = (
        select(Video)
        .options(selectinload(Video.renditions))
        .where(
            match_clause,
            Video.status == VideoStatus.READY,
            Video.share_revoked == False,
        )
    )
    result = await db.execute(stmt)
    video = result.scalar_one_or_none()

    headers = {
        "Cache-Control": "public, max-age=86400",
        "X-Content-Type-Options": "nosniff",
    }

    if video:
        target_path, nginx_subpath = resolve_poster_disk_path(video)
        if target_path and target_path.is_file():
            headers["Content-Type"] = "image/webp"
            if settings.SERVE_MEDIA_VIA_NGINX and nginx_subpath:
                headers["X-Accel-Redirect"] = nginx_subpath
                return Response(status_code=200, headers=headers)
            return FileResponse(path=str(target_path), media_type="image/webp", headers=headers)

    # Deterministic beautiful vector SVG fallback
    title = video.title if video else "ABOODVIDEO"
    is_4k = False
    max_q = "HD"
    if video and video.renditions:
        for r in video.renditions:
            if r.resolution_height and r.resolution_height >= 2160:
                is_4k = True
                break
    svg_data = generate_deterministic_svg_poster(title, is_4k=is_4k, max_quality=max_q)
    headers["Content-Type"] = "image/svg+xml; charset=utf-8"
    return Response(content=svg_data, media_type="image/svg+xml", status_code=200, headers=headers)


async def viewer_error_page(request: Request):
    """Polished, non-disclosing viewer error page."""
    prefix = get_nav_prefix(request)
    return render_viewer_template("viewer_error.html", prefix=prefix, error_title="فيديو غير متاح", error_message="عذراً، هذا الفيديو غير متاح.")


@router.get("/share/{token}")
async def shared_anonymous_view_page(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis_conn=Depends(get_redis),
):
    """
    Shared Anonymous Viewer: Scoped strictly to the shared resource.
    No admin sidebar, no server storage telemetry, clean video list with download enforcement.
    """
    from app.services.drive_service import DriveService
    share = await DriveService.get_share_by_token(db, token)
    if not share:
        return Response(status_code=404, headers={"Cache-Control": "no-store"})

    if not await has_share_access(request, share, redis_conn):
        return render_viewer_template("share_unlock.html", token=token)
    try:
        shared_data = await DriveService.get_shared_scope_contents(db, share)
        user = await get_optional_current_user(request=request, db=db)
        return render_viewer_template(
            "drive_share.html",
            share=shared_data,
            items=shared_data.get("items", []),
            prefix=get_nav_prefix(request),
            can_preview_private=bool(user and (user.role == UserRole.ADMIN or
                                     (user.role == UserRole.OWNER and user.id == share.owner_id))),
        )
    except Exception as e:
        logger.error("Failed to fetch shared contents for share %s: %s", share.id, type(e).__name__)
        return Response(status_code=404, headers={"Cache-Control": "no-store"})


@router.post("/share/{token}/unlock")
async def unlock_shared_view(
    token: str, request: Request, password: str = Form(..., min_length=1, max_length=1024),
    db: AsyncSession = Depends(get_db), redis_conn=Depends(get_redis),
):
    from app.services.drive_service import DriveService
    share = await DriveService.get_share_by_token(db, token)
    if not share:
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    response = RedirectResponse(url=f"/preview/share/{token}", status_code=303)
    try:
        await unlock_share(request, response, share, password, redis_conn)
    except HTTPException as exc:
        response = render_viewer_template(
            "share_unlock.html", status_code=exc.status_code, token=token,
            error_message="كلمة المرور غير صحيحة" if exc.status_code == 401 else "محاولات كثيرة؛ يرجى الانتظار ثم المحاولة مجددًا",
        )
        response.headers.update(exc.headers or {})
    return response


@router.get("/share/{token}/watch/{video_id}")
async def watch_shared_resource(
    token: str, video_id: uuid.UUID, request: Request,
    db: AsyncSession = Depends(get_db), redis_conn=Depends(get_redis),
):
    from app.services.drive_service import DriveService
    from app.api.watch_routes import (
        _init_playback_session, get_authenticated_playback_user, get_watch_subtitles,
        get_authenticated_admin, render_watch_template, render_not_found_page,
    )
    share = await DriveService.get_share_by_token(db, token)
    if not share:
        return render_not_found_page()
    if not await has_share_access(request, share, redis_conn):
        return render_viewer_template("share_unlock.html", token=token)
    video = await DriveService.get_shared_video_if_available(db, share, video_id)
    if (not video or video.status != VideoStatus.READY or not video.active_generation_id
            or video.share_revoked):
        return render_not_found_page()
    admin_user = await get_authenticated_admin(request, db, video)
    if video.visibility == VideoVisibility.PRIVATE and not admin_user:
        return render_not_found_page()
    from app.security.playback_token import ViewerSessionManager, VIEWER_COOKIE_NAME
    viewer_session_id = request.cookies.get(VIEWER_COOKIE_NAME)
    if video.visibility == VideoVisibility.PASSWORD_PROTECTED:
        if not video.share_id:
            return render_not_found_page()
        if not await ViewerSessionManager.has_password_access(redis_conn, viewer_session_id, video.id):
            return render_watch_template(title=video.title, share_id=video.share_id, is_password_required=True)
    playback_token, cookie = await _init_playback_session(
        request=request, db=db, redis_conn=redis_conn, video=video,
        admin_user=admin_user if video.visibility == VideoVisibility.PRIVATE else None,
        playback_user=await get_authenticated_playback_user(request, db), scoped_share=share,
        viewer_session_id=viewer_session_id,
    )
    return render_watch_template(
        title=video.title, share_id=str(video.id), playback_token=playback_token,
        cookie_header=cookie, subtitles=await get_watch_subtitles(db, video.id),
        poster_url=f"/api/v1/playback/{playback_token}/poster.webp" if video.poster_path else "",
        series_name=video.series_name, season_number=video.season_number,
        episode_number=video.episode_number, description=video.description,
    )


@router.get("/share/{token}/download/{video_id}")
async def download_shared_resource_video(
    token: str,
    video_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis_conn=Depends(get_redis),
):
    """Anonymous download route routed via /preview/share/{token}/download/{video_id}."""
    import os
    from app.services.drive_service import DriveService
    share = await DriveService.get_share_by_token(db, token)
    if not share:
        raise HTTPException(status_code=404, detail="الرابط غير صالح أو منتهي الصلاحية")

    await require_share_access(request, share, redis_conn)
    if not share.allow_download:
        raise HTTPException(status_code=403, detail="التنزيل غير مسموح به عبر هذا الرابط")

    video = await DriveService.get_shared_video_if_available(db, share, video_id)
    if not video:
        # Keep target lifecycle and scope failures non-disclosing.
        raise HTTPException(status_code=404, detail="المحتوى غير متاح")

    # Verify inherited download policy
    can_download = await DriveService.get_effective_download_policy(db, video)
    if not can_download:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="التنزيل معطل لهذا الفيديو وفقاً لسياسة التنزيل المحددة",
        )

    file_path = video.source_path
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="ملف الفيديو غير متوفر على القرص")

    safe_filename = build_download_filename(video)
    from app.api.v1.admin_transfer_settings import get_transfer_policy
    transfer_policy = await get_transfer_policy(db)
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    if transfer_policy["playback_kbps"] > 0:
        headers["X-Accel-Limit-Rate"] = str(transfer_policy["playback_kbps"] * 125)
    return FileResponse(
        path=file_path,
        media_type="application/octet-stream",
        filename=safe_filename,
        headers=headers,
    )


async def viewer_error_code_page(request: Request, error_code: str):
    """Serve semantic, browser-friendly error pages for specific error codes.

    Maps slug/numeric codes to Arabic titles and descriptive messages, then
    renders viewer_error.html with the appropriate icon branch selected via
    the error_code template variable.
    """
    prefix = get_nav_prefix(request)
    error_map = {
        "404": (
            "الصفحة غير موجودة",
            "عذراً، الصفحة التي تبحث عنها غير موجودة.",
            "404_PAGE_NOT_FOUND",
        ),
        "403": (
            "الوصول محظور",
            "ليس لديك صلاحية للوصول إلى هذا المحتوى.",
            "403_FORBIDDEN",
        ),
        "video-unavailable": (
            "الفيديو غير متاح",
            "هذا الفيديو غير متاح حالياً.",
            "VIDEO_UNAVAILABLE",
        ),
        "video-not-ready": (
            "الفيديو قيد المعالجة",
            "هذا الفيديو لا يزال قيد المعالجة. الرجاء المحاولة لاحقاً.",
            "VIDEO_NOT_READY",
        ),
        "video-removed": (
            "الفيديو محذوف",
            "هذا الفيديو لم يعد متاحاً.",
            "VIDEO_REMOVED",
        ),
        "share-expired": (
            "الرابط منتهي الصلاحية",
            "انتهت صلاحية رابط المشاركة هذا.",
            "SHARE_EXPIRED",
        ),
        "share-revoked": (
            "تم إلغاء الرابط",
            "تم إلغاء رابط المشاركة هذا من قبل المالك.",
            "SHARE_REVOKED",
        ),
        "playback-error": (
            "خطأ في التشغيل",
            "تعذر تشغيل هذا الفيديو. يرجى المحاولة مرة أخرى.",
            "PLAYBACK_ERROR",
        ),
        "server-error": (
            "خطأ في الخادم",
            "حدث خطأ غير متوقع. يرجى المحاولة لاحقاً.",
            "SERVER_ERROR",
        ),
    }
    title, message, code = error_map.get(
        error_code,
        ("خطأ", "حدث خطأ غير متوقع.", error_code.upper()),
    )
    return render_viewer_template(
        "viewer_error.html",
        prefix=prefix,
        error_title=title,
        error_message=message,
        error_code=code,
    )
