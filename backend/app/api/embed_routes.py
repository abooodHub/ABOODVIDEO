"""Minimal public embed surface, separate from catalog and admin routes."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from urllib.parse import urlparse
import re
import uuid

from app.api.watch_routes import (
    VIEWER_COOKIE_NAME,
    _init_playback_session,
    get_authenticated_admin,
    get_authenticated_playback_user,
    get_library_meta,
    get_watch_subtitles,
    render_not_found_page,
    render_watch_template,
    direct_mp4_playback_url,
    render_unauthorized_domain_page,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.redis import get_redis
from app.models.video import Video, VideoStatus, VideoVisibility
from app.security.playback_token import ViewerSessionManager

from app.services.embed_settings import get_all_embed_settings, build_frame_ancestors, is_embed_host_allowed

router = APIRouter(include_in_schema=False)


@router.get("/embed/{share_id}")
async def embed_video_page(
    share_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis_conn=Depends(get_redis),
):
    """Render the existing player without catalog, episode, or admin chrome, with hotlink protection and customizations."""
    # ─── 0. Load Embed & CDN Settings (cached) ───
    embed_cfg = await get_all_embed_settings(db)
    allowed_domains = embed_cfg.get("allowed_domains", ["localhost", "127.0.0.1"])

    request.state.embed_frame_ancestors = build_frame_ancestors(allowed_domains)

    # ─── 1. Domain Whitelisting & Hotlink Protection ───
    referer = request.headers.get("Referer", "")
    origin = request.headers.get("Origin", "")
    fetch_destination = request.headers.get("Sec-Fetch-Dest", "").lower()
    embed_only = bool(embed_cfg.get("embed_only_mode"))

    # Browser-provided fetch metadata distinguishes an iframe from a direct
    # top-level navigation. Fail closed when embed-only mode is enabled.
    if embed_only and fetch_destination not in {"iframe", "object", "embed"}:
        return render_unauthorized_domain_page("الوصول المباشر")

    # parent_domain is caller-controlled query data and cannot establish the
    # embedding site's identity. A missing/invalid browser origin is denied.
    check_target = referer or origin
    if embed_only and not check_target:
        return render_unauthorized_domain_page("غير معروف")
    if check_target:
        try:
            parsed = urlparse(check_target)
            host = (parsed.hostname or "").lower()
            if not host:
                return render_unauthorized_domain_page("غير معروف")
            is_allowed = is_embed_host_allowed(host, allowed_domains)
            if not is_allowed:
                return render_unauthorized_domain_page(domain=host)
        except (TypeError, ValueError):
            return render_unauthorized_domain_page("غير معروف")

    # ─── 2. Query Parameters Customization (with DB fallbacks) ───
    # Color (hex format: 00d2be or #00d2be)
    raw_color = request.query_params.get("color", "").strip()
    primary_color = embed_cfg.get("default_color", "#00d2be")
    if raw_color:
        clean_hex = raw_color.lstrip("#")
        if re.fullmatch(r"[0-9a-fA-F]{3}|[0-9a-fA-F]{6}", clean_hex):
            primary_color = f"#{clean_hex}"

    # Custom Watermark text
    raw_watermark = request.query_params.get("watermark", "").strip()
    watermark_text = raw_watermark[:40] if raw_watermark else embed_cfg.get("default_watermark", "ABOODVIDEO")

    # Skip intro seconds (0 disables button)
    skip_str = request.query_params.get("skip", "").strip()
    skip_intro_seconds = embed_cfg.get("default_skip_intro", 85)
    if skip_str.isdigit():
        skip_intro_seconds = int(skip_str)

    # Autoplay
    auto_play = request.query_params.get("autoplay") in ("1", "true")

    # Start time
    t_str = request.query_params.get("t", "").strip()
    start_time = None
    try:
        if t_str:
            start_time = float(t_str)
    except ValueError:
        pass

    # Pause Banner
    banner_flag = request.query_params.get("banner", "")
    pause_banner = None
    default_banner = embed_cfg.get("pause_banner", {})
    if banner_flag in ("1", "true") or (not banner_flag and default_banner.get("enabled")):
        pause_banner = {
            "title": request.query_params.get("banner_title") or default_banner.get("title", "تابعنا على قنواتنا الرسمية"),
            "text": request.query_params.get("banner_text") or default_banner.get("text", "انضم إلينا لمتابعة جميع الحلقات والإصدارات الجديدة بجودة 4K"),
            "link": request.query_params.get("banner_link") or default_banner.get("link", ""),
        }

    # ─── 3. Video Access Validation ───
    parsed_uuid = None
    try:
        parsed_uuid = uuid.UUID(share_id)
    except (ValueError, AttributeError):
        pass

    if parsed_uuid:
        stmt = (
            select(Video)
            .options(selectinload(Video.renditions))
            .where(Video.id == parsed_uuid)
        )
    else:
        stmt = (
            select(Video)
            .options(selectinload(Video.renditions))
            .where(Video.share_id == share_id)
        )
    video = (await db.execute(stmt)).scalar_one_or_none()

    unavailable = lambda: render_not_found_page(embed_mode=True)
    if not video or video.trashed_at is not None:
        return unavailable()
    if video.status != VideoStatus.READY or not video.active_generation_id:
        return unavailable()

    admin_user = await get_authenticated_admin(request, db, video)
    playback_user = await get_authenticated_playback_user(request, db)
    if video.visibility == VideoVisibility.PRIVATE and not admin_user:
        return unavailable()
    if video.share_revoked:
        return unavailable()

    effective_share_id = video.share_id or share_id

    viewer_session_id = request.cookies.get(VIEWER_COOKIE_NAME)
    password_required = False
    if video.visibility == VideoVisibility.PASSWORD_PROTECTED:
        password_required = not await ViewerSessionManager.has_password_access(
            redis_conn, viewer_session_id, video.id
        )
        if password_required:
            return render_watch_template(
                title=video.title,
                share_id=effective_share_id,
                is_password_required=True,
                embed_mode=True,
                primary_color=primary_color,
                watermark_text=watermark_text,
                skip_intro_seconds=skip_intro_seconds,
                auto_play=auto_play,
                start_time=start_time,
                pause_banner=pause_banner,
            )

    token, cookie_header = await _init_playback_session(
        request=request,
        db=db,
        redis_conn=redis_conn,
        video=video,
        admin_user=admin_user,
        playback_user=playback_user,
        share_id=effective_share_id,
        viewer_session_id=viewer_session_id,
    )
    subtitles = await get_watch_subtitles(db, video.id)
    is_4k = any(
        r.quality_label in ("2160p", "4K") or (r.resolution_height and r.resolution_height >= 2160)
        for r in (video.renditions or [])
    )
    has_ass = any(
        (s.get("format") or "").lower() in ("ass", "ssa") or s.get("renderer") == "libass"
        for s in subtitles
    )
    return render_watch_template(
        title=video.title,
        share_id=effective_share_id,
        playback_token=token,
        direct_media_url=direct_mp4_playback_url(video, token),
        cookie_header=cookie_header,
        poster_url=f"/api/v1/playback/{token}/poster.webp" if video.poster_path else "",
        subtitles=subtitles,
        library_meta=get_library_meta(video),
        series_name=video.series_name,
        season_number=video.season_number,
        episode_number=video.episode_number,
        description=video.description,
        is_4k=is_4k,
        has_ass=has_ass,
        embed_mode=True,
        primary_color=primary_color,
        watermark_text=watermark_text,
        skip_intro_seconds=skip_intro_seconds,
        auto_play=auto_play,
        start_time=start_time,
        pause_banner=pause_banner,
    )
