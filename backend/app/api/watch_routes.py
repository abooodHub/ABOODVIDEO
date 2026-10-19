"""Watch Page Routes for viewer playback and owner/admin private previews."""
import json
import secrets
import uuid
from pathlib import Path
from urllib.parse import quote
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from shared.i18n import configure_environment, gettext
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
import redis.asyncio as aioredis

from app.core.config import settings
from app.core.database import get_db
from app.core.redis import get_redis
from app.core.logging import get_logger
from app.models.user import User, UserRole
from app.models.share import Share
from app.models.session import UserSession
from app.models.video import Video, VideoStatus, VideoVisibility, VideoSubtitle, VideoFontAsset
from app.core.network import get_real_client_ip
from app.services.geo_access import enforce_geo_policy
from app.security.tokens import hash_token
from app.security.ownership import require_video_owner
from app.security.share_access import share_password_version
from app.services.entitlements import playback_entitlement_for_user
from app.security.playback_token import (
    create_playback_token,
    ViewerSessionManager,
    VIEWER_COOKIE_NAME,
)
from app.security.playback_session import (
    create_playback_principal,
    create_playback_session,
    build_playback_cookie,
)
from app.api.viewer_routes import (
    parse_natural_episode_number,
    format_natural_episode_number,
    format_season_number,
)

logger = get_logger("watch_routes")

router = APIRouter(include_in_schema=False)
TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


async def get_authenticated_admin(request: Request, db: AsyncSession, video: Optional[Video] = None) -> Optional[User]:
    """Check if the request has an active authenticated OWNER or ADMIN session."""
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not token:
        return None

    token_hashed = hash_token(token)
    stmt = (
        select(UserSession)
        .options(selectinload(UserSession.user))
        .where(
            UserSession.token_hash == token_hashed,
            UserSession.expires_at > datetime.now(timezone.utc),
        )
    )
    result = await db.execute(stmt)
    sess = result.scalar_one_or_none()
    if sess and sess.user and sess.user.is_active and sess.user.role in (UserRole.OWNER, UserRole.ADMIN):
        if video is not None and sess.user.role != UserRole.ADMIN and video.user_id != sess.user.id:
            return None
        return sess.user
    return None


async def _init_playback_session(
    request: Request,
    db: AsyncSession,
    redis_conn: aioredis.Redis,
    video: Video,
    admin_user: Optional[User],
    playback_user: Optional[User] = None,
    share_id: Optional[str] = None,
    viewer_session_id: Optional[str] = None,
    scoped_share: Optional[Share] = None,
) -> tuple[str, str]:
    """Create playback principal + short-lived session, return (playback_token, cookie_header)."""
    if not admin_user:
        await enforce_geo_policy(request, db, redis_conn)
    identity_user = playback_user or admin_user
    entitlement = playback_entitlement_for_user(identity_user)
    plan = entitlement.plan.lower()
    user_id = str(identity_user.id) if identity_user else None
    auth_type = "admin" if admin_user else "share" if video.visibility != VideoVisibility.PUBLIC else "public"
    if scoped_share is not None:
        auth_type = "drive_share"
    max_quality = entitlement.max_quality
    download_allowed = entitlement.download_allowed
    if scoped_share is not None:
        from app.services.drive_service import DriveService
        download_allowed = scoped_share.allow_download and await DriveService.get_effective_download_policy(db, video)

    client_ip = get_real_client_ip(request)
    user_agent = request.headers.get("User-Agent", "")

    # Reuse the browser's live identity; each video still gets its own session
    # and independently verified visibility/share/generation entitlement.
    from app.security.playback_session import _hash_secret, _pkey
    principal_secret = request.cookies.get(settings.PLAYBACK_COOKIE_NAME)
    principal = await redis_conn.hgetall(_pkey(_hash_secret(principal_secret))) if principal_secret else {}
    import time
    if (not principal or principal.get("revoked") == "1"
            or int(principal.get("expires_at", "0")) <= int(time.time())
            or principal.get("user_id", "") != (user_id or "")
            or principal.get("plan") != plan):
        principal_secret = await create_playback_principal(
            redis_conn,
            user_id=user_id,
            external_user_id=None,
            plan=plan,
            max_quality=max_quality,
            download_allowed=download_allowed,
            concurrent_stream_limit=settings.PLAYBACK_MAX_CONCURRENT_SESSIONS,
            user_agent=user_agent,
            ip=client_ip,
        )
    sid = await create_playback_session(
        redis_conn,
        principal_secret=principal_secret,
        video_id=video.id,
        generation_id=video.active_generation_id,
        authorization_type=auth_type,
        share_id=share_id or video.share_id,
        viewer_session_id=viewer_session_id,
        max_quality=max_quality,
        download_allowed=download_allowed,
        drive_share_token=scoped_share.token if scoped_share else None,
        drive_share_password_version=share_password_version(scoped_share) if scoped_share else None,
    )
    cookie_hdr = build_playback_cookie(principal_secret, settings.PLAYBACK_SESSION_TTL_SECONDS)
    return f"s/{sid}", cookie_hdr


def direct_mp4_playback_url(video: Video, playback_token: str) -> Optional[str]:
    """Return an authorized source URL only for a validated imported MP4."""
    prefix = f"videos/{video.id}/generations/{video.active_generation_id}/"
    for rendition in video.renditions or []:
        stored = rendition.playlist_path or ""
        if rendition.quality_label == "source" and stored.startswith(prefix) and stored.lower().endswith(".mp4"):
            relative = stored[len(prefix):]
            return f"/api/v1/playback/{playback_token}/{quote(relative, safe='/-_.')}"
    return None

def render_watch_template(
    title: str,
    share_id: str,
    playback_token: Optional[str] = None,
    direct_media_url: Optional[str] = None,
    cookie_header: Optional[str] = None,
    poster_url: Optional[str] = None,
    is_password_required: bool = False,
    is_admin_preview: bool = False,
    subtitles: Optional[list] = None,
    library_meta: str = "",
    series_name: Optional[str] = None,
    season_number: Optional[int] = None,
    season_number_formatted: Optional[str] = None,
    episode_number: Optional[int] = None,
    episode_number_formatted: Optional[str] = None,
    description: Optional[str] = None,
    is_4k: bool = False,
    has_ass: bool = False,
    prev_episode: Optional[dict] = None,
    next_episode: Optional[dict] = None,
    more_episodes: Optional[list] = None,
    prefix: str = "",
    embed_mode: bool = False,
    primary_color: Optional[str] = None,
    watermark_text: Optional[str] = None,
    skip_intro_seconds: Optional[int] = 85,
    auto_play: bool = False,
    start_time: Optional[float] = None,
    pause_banner: Optional[dict] = None,
    csp_nonce: str = "",
) -> HTMLResponse:
    """Render watch.html with context and strict no-referrer header."""
    csp_nonce = csp_nonce or secrets.token_urlsafe(18)
    file_path = TEMPLATES_DIR / "watch.html"
    if not file_path.exists():
        return HTMLResponse("Player template not found", status_code=500)

    content = file_path.read_text(encoding="utf-8")

    import jinja2
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )
    configure_environment(env)
    template = env.get_template("watch.html")
    rendered_content = template.render(
        VIDEO_TITLE=title,
        SHARE_ID=share_id,
        PLAYBACK_TOKEN=playback_token or "",
        DIRECT_MEDIA_URL=direct_media_url or None,
        POSTER_URL=poster_url or "",
        IS_PASSWORD_REQUIRED=is_password_required,
        IS_ADMIN_PREVIEW=is_admin_preview,
        SUBTITLES_JSON=json.dumps(subtitles or []).replace("<", "\\u003c"),
        LIBRARY_META=library_meta,
        SERIES_NAME=series_name,
        SEASON_NUMBER=season_number,
        SEASON_NUMBER_FORMATTED=season_number_formatted,
        EPISODE_NUMBER=episode_number,
        EPISODE_NUMBER_FORMATTED=episode_number_formatted,
        VIDEO_DESCRIPTION=description,
        IS_4K=is_4k,
        HAS_ASS=has_ass,
        PREV_EPISODE=prev_episode,
        NEXT_EPISODE=next_episode,
        MORE_EPISODES=more_episodes,
        prefix=prefix,
        EMBED_MODE=embed_mode,
        PRIMARY_COLOR=primary_color,
        WATERMARK_TEXT=watermark_text,
        SKIP_INTRO_SECONDS=skip_intro_seconds,
        AUTO_PLAY=auto_play,
        START_TIME=start_time,
        PAUSE_BANNER=pause_banner,
        CSP_NONCE=csp_nonce,
    )

    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "X-AV-CSP-Nonce": csp_nonce,
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
    if cookie_header:
        headers["Set-Cookie"] = cookie_header
    response = HTMLResponse(rendered_content, headers=headers)
    if cookie_header:
        # Retire the legacy path cookie so it cannot shadow the browser identity.
        response.delete_cookie(settings.PLAYBACK_COOKIE_NAME, path="/api/v1/playback/", httponly=True, secure=settings.SESSION_COOKIE_SECURE)
    return response


def render_unauthorized_domain_page(domain: str) -> HTMLResponse:
    """Render Arabic 403 screen when an unauthorized domain attempts to hotlink/embed player."""
    title = "غير مصرح بتضمين المشغل"
    message = f"تم حظر تشغيل الفيديو لأن النطاق الحالي ({domain}) غير مسجل في قائمة النطاقات المعتمدة لمنصة ABOODVIDEO."
    err_template = TEMPLATES_DIR / "viewer_error.html"
    if err_template.exists():
        try:
            import jinja2
            tpl = configure_environment(jinja2.Environment(
                loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
                autoescape=True
            )).get_template("viewer_error.html")
            html = tpl.render(
                error_title=gettext(title),
                error_message=gettext(message),
                error_code="403_FORBIDDEN",
                EMBED_MODE=True,
            )
            return HTMLResponse(html, status_code=403, headers={"Referrer-Policy": "no-referrer"})
        except Exception:
            pass

    html = f"""<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8"><title>{title}</title></head><body style="background:#070a10;color:#f8fafc;font-family:sans-serif;text-align:center;padding:50px;"><h2>{title}</h2><p>{message}</p></body></html>"""
    return HTMLResponse(html, status_code=403, headers={"Referrer-Policy": "no-referrer"})


def render_not_found_page(
    title: str = "عذراً، هذا الفيديو غير متاح",
    message: str = "قد يكون الرابط غير صحيح، أو تم إلغاؤه، أو لا تملك الصلاحية اللازمة للوصول إليه.",
    prefix: str = "",
    embed_mode: bool = False,
) -> HTMLResponse:
    """Keep invalid, revoked, private and unavailable watch links undisclosed."""
    return HTMLResponse(
        content="",
        status_code=404,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


async def get_watch_subtitles(db: AsyncSession, video_id: uuid.UUID) -> list[dict]:
    rows = (await db.execute(
        select(VideoSubtitle).where(VideoSubtitle.video_id == video_id)
        .order_by(VideoSubtitle.is_default.desc(), VideoSubtitle.created_at.asc())
    )).scalars().all()

    font_rows = (await db.execute(
        select(VideoFontAsset).where(VideoFontAsset.video_id == video_id)
    )).scalars().all()
    font_map = {str(f.id): {
        "id": str(f.id),
        "family": f.family_name,
        "filename": f.filename,
        "size": f.size_bytes,
        "sha256": f.sha256,
        "mime": f.mime_type,
    } for f in font_rows}

    res = []
    for s in rows:
        s_fonts = []
        if s.font_asset_ids:
            for fid in s.font_asset_ids:
                if fid in font_map:
                    s_fonts.append(font_map[fid])
        if not s_fonts and font_rows and (s.format or "").lower() in ("ass", "ssa"):
            s_fonts = list(font_map.values())

        renderer = s.playback_renderer or ("libass" if (s.format or "").lower() in ("ass", "ssa") else "html5")

        res.append({
            "id": str(s.id),
            "label": s.label,
            "lang": s.language_code,
            "name": s.language_name,
            "is_default": s.is_default,
            "is_forced": s.is_forced,
            "is_playable": s.is_playable,
            "format": s.format,
            "renderer": renderer,
            "fonts": s_fonts,
        })
    return res


def get_library_meta(video: Video) -> str:
    parts = [part for part in (video.category, video.series_name) if part]
    if video.season_number is not None:
        parts.append(gettext(f"الموسم {video.season_number}"))
    if video.episode_number is not None:
        parts.append(gettext(f"الحلقة {video.episode_number}"))
    return " • ".join(parts)


@router.get("/watch/{share_id}")
@router.get("/preview/watch/{share_id}")
async def watch_video_page(
    share_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis_conn: aioredis.Redis = Depends(get_redis),
):
    """Serve viewer watch page enforcing video visibility, share link validity, and password requirements."""
    prefix = "/preview" if request.url.path.startswith("/preview") else ""
    embed_mode = request.url.path.startswith("/embed/")

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
    result = await db.execute(stmt)
    video = result.scalar_one_or_none()

    # Video must exist
    if not video:
        return render_not_found_page(prefix=prefix, embed_mode=embed_mode)

    # Informative errors if video exists but is not ready or is trashed
    if video.trashed_at is not None:
        return render_not_found_page(
            title="الفيديو محذوف",
            message="هذا الفيديو تم نقله إلى سلة المحذوفات وغير متاح للمشاهدة حالياً.",
            prefix=prefix,
            embed_mode=embed_mode,
        )

    if video.status != VideoStatus.READY or not video.active_generation_id:
        return render_not_found_page(
            title="الفيديو قيد المعالجة",
            message="هذا الفيديو لا يزال قيد المعالجة ولم يكتمل تجهيزه بعد. الرجاء المحاولة لاحقاً.",
            prefix=prefix,
            embed_mode=embed_mode,
        )

    admin_user = await get_authenticated_admin(request, db, video)
    playback_user = await get_authenticated_playback_user(request, db)

    # If accessed by canonical UUID, authenticated owner/admin can watch directly
    if parsed_uuid:
        if admin_user:
            token, cookie_hdr = await _init_playback_session(
                request=request,
                db=db,
                redis_conn=redis_conn,
                video=video,
                admin_user=admin_user,
                playback_user=playback_user,
                share_id=video.share_id or str(video.id),
            )
            subtitles = await get_watch_subtitles(db, video.id)
            is_4k = any(r.quality_label in ("2160p", "4K") or (r.resolution_height and r.resolution_height >= 2160) for r in (video.renditions or []))
            has_ass = any((s.get("format") or "").lower() in ("ass", "ssa") or s.get("renderer") == "libass" for s in subtitles)
            return render_watch_template(
                title=video.title,
                share_id=video.share_id or str(video.id),
                playback_token=token,
                direct_media_url=direct_mp4_playback_url(video, token),
                cookie_header=cookie_hdr,
                poster_url=f"/api/v1/playback/{token}/poster.webp" if video.poster_path else "",
                is_admin_preview=True,
                subtitles=subtitles,
                library_meta=get_library_meta(video),
                series_name=video.series_name,
                season_number=video.season_number,
                episode_number=video.episode_number,
                description=video.description,
                is_4k=is_4k,
                has_ass=has_ass,
                prefix=prefix,
                embed_mode=embed_mode,
            )
        elif video.visibility != VideoVisibility.PUBLIC:
            # UUID accessed by unauthenticated non-admin for non-public video
            return render_not_found_page(prefix=prefix, embed_mode=embed_mode)

    # 0. Check Platform Embed-Only (CDN) Mode for direct public visits
    if not admin_user and not embed_mode:
        from app.services.embed_settings import get_all_embed_settings
        embed_cfg = await get_all_embed_settings(db)
        if embed_cfg.get("embed_only_mode"):
            target_url = embed_cfg.get("external_redirect_url", "")
            return RedirectResponse(url=target_url, status_code=307) if target_url else Response(status_code=404)

    # 1. PRIVATE videos: Never accessible publicly via share_id unless requester is OWNER/ADMIN
    if video.visibility == VideoVisibility.PRIVATE:
        if not admin_user:
            return render_not_found_page(prefix=prefix, embed_mode=embed_mode)
        # Admin can view
        token, cookie_hdr = await _init_playback_session(
            request=request,
            db=db,
            redis_conn=redis_conn,
            video=video,
            admin_user=admin_user,
                playback_user=playback_user,
            share_id=share_id,
        )
        subtitles = await get_watch_subtitles(db, video.id)
        is_4k = any(r.quality_label in ("2160p", "4K") or (r.resolution_height and r.resolution_height >= 2160) for r in (video.renditions or []))
        has_ass = any((s.get("format") or "").lower() in ("ass", "ssa") or s.get("renderer") == "libass" for s in subtitles)
        return render_watch_template(
            title=video.title,
            share_id=share_id,
            playback_token=token,
            direct_media_url=direct_mp4_playback_url(video, token),
            cookie_header=cookie_hdr,
            poster_url=f"/api/v1/playback/{token}/poster.webp" if video.poster_path else "",
            is_admin_preview=True,
            subtitles=subtitles,
            library_meta=get_library_meta(video),
            series_name=video.series_name,
            season_number=video.season_number,
            episode_number=video.episode_number,
            description=video.description,
            is_4k=is_4k,
            has_ass=has_ass,
            prefix=prefix,
            embed_mode=embed_mode,
        )

    # 2. Check share link revocation for unlisted / password-protected videos
    if video.share_revoked:
        return render_not_found_page(prefix=prefix, embed_mode=embed_mode)

    # 3. Fetch active subtitles
    subtitles = await get_watch_subtitles(db, video.id)
    is_4k = any(r.quality_label in ("2160p", "4K") or (r.resolution_height and r.resolution_height >= 2160) for r in (video.renditions or []))
    has_ass = any((s.get("format") or "").lower() in ("ass", "ssa") or s.get("renderer") == "libass" for s in subtitles)

    # Query series neighbors
    prev_episode = None
    next_episode = None
    more_episodes = []
    cur_ep_float = parse_natural_episode_number(video)
    ep_formatted = format_natural_episode_number(cur_ep_float) if cur_ep_float > 0 else (str(video.episode_number) if video.episode_number else "")
    season_formatted = format_season_number(video.season_number) if video.season_number else "Season 01"

    if video.series_id or video.series_name:
        series_res = await db.execute(
            select(Video)
            .where(
                Video.series_id == video.series_id if video.series_id else and_(
                    Video.series_id.is_(None), Video.series_name == video.series_name),
                Video.user_id == video.user_id,
                Video.trashed_at.is_(None),
                Video.status == VideoStatus.READY,
                Video.active_generation_id.isnot(None),
                Video.share_revoked == False,
                Video.visibility.in_([VideoVisibility.PUBLIC, VideoVisibility.UNLISTED]),
            )
        )
        raw_series = series_res.scalars().all()
        all_series = sorted(
            raw_series,
            key=lambda v: (
                v.season_number or 1,
                parse_natural_episode_number(v),
                v.created_at,
            )
        )
        for idx, item in enumerate(all_series):
            if item.id == video.id:
                if idx > 0:
                    prev_item = all_series[idx - 1]
                    prev_ep_float = parse_natural_episode_number(prev_item)
                    prev_episode = {
                        "title": prev_item.title,
                        "share_id": prev_item.share_id,
                        "episode_number": prev_item.episode_number,
                        "episode_number_formatted": format_natural_episode_number(prev_ep_float) if prev_ep_float > 0 else (str(prev_item.episode_number) if prev_item.episode_number else ""),
                    }
                if idx < len(all_series) - 1:
                    next_item = all_series[idx + 1]
                    next_ep_float = parse_natural_episode_number(next_item)
                    next_episode = {
                        "title": next_item.title,
                        "share_id": next_item.share_id,
                        "episode_number": next_item.episode_number,
                        "episode_number_formatted": format_natural_episode_number(next_ep_float) if next_ep_float > 0 else (str(next_item.episode_number) if next_item.episode_number else ""),
                    }
            else:
                item_ep_float = parse_natural_episode_number(item)
                more_episodes.append({
                    "title": item.title,
                    "share_id": item.share_id,
                    "episode_number": item.episode_number,
                    "episode_number_formatted": format_natural_episode_number(item_ep_float) if item_ep_float > 0 else (str(item.episode_number) if item.episode_number else ""),
                    "poster_url": f"{prefix}/poster/{item.share_id}.webp" if item.poster_path else "",
                })

    # 4. PASSWORD_PROTECTED videos
    if video.visibility == VideoVisibility.PASSWORD_PROTECTED:
        viewer_session_id = request.cookies.get(VIEWER_COOKIE_NAME)
        has_access = await ViewerSessionManager.has_password_access(
            redis_conn, viewer_session_id, video.id
        )
        if not has_access:
            # Render password prompt
            return render_watch_template(
                title=video.title,
                share_id=share_id,
                is_password_required=True,
                series_name=video.series_name,
                season_number=video.season_number,
                season_number_formatted=season_formatted,
                episode_number=video.episode_number,
                episode_number_formatted=ep_formatted,
                description=video.description,
                prefix=prefix,
                embed_mode=embed_mode,
            )

        # Viewer has valid password session
        token, cookie_hdr = await _init_playback_session(
            request=request,
            db=db,
            redis_conn=redis_conn,
            video=video,
            admin_user=admin_user,
                playback_user=playback_user,
            share_id=share_id,
            viewer_session_id=viewer_session_id,
        )
        return render_watch_template(
            title=video.title,
            share_id=share_id,
            playback_token=token,
            direct_media_url=direct_mp4_playback_url(video, token),
            cookie_header=cookie_hdr,
            poster_url=f"/api/v1/playback/{token}/poster.webp" if video.poster_path else "",
            subtitles=subtitles,
            library_meta=get_library_meta(video),
            series_name=video.series_name,
            season_number=video.season_number,
            season_number_formatted=season_formatted,
            episode_number=video.episode_number,
            episode_number_formatted=ep_formatted,
            description=video.description,
            is_4k=is_4k,
            has_ass=has_ass,
            prev_episode=prev_episode,
            next_episode=next_episode,
            more_episodes=more_episodes[:10],
            prefix=prefix,
            embed_mode=embed_mode,
        )

    # 5. UNLISTED or PUBLIC videos
    token, cookie_hdr = await _init_playback_session(
        request=request,
        db=db,
        redis_conn=redis_conn,
        video=video,
        admin_user=admin_user,
                playback_user=playback_user,
        share_id=share_id,
    )
    return render_watch_template(
        title=video.title,
        share_id=share_id,
        playback_token=token,
        direct_media_url=direct_mp4_playback_url(video, token),
        cookie_header=cookie_hdr,
        poster_url=f"/api/v1/playback/{token}/poster.webp" if video.poster_path else "",
        subtitles=subtitles,
        library_meta=get_library_meta(video),
        series_name=video.series_name,
        season_number=video.season_number,
        season_number_formatted=season_formatted,
        episode_number=video.episode_number,
        episode_number_formatted=ep_formatted,
        description=video.description,
        is_4k=is_4k,
        has_ass=has_ass,
        prev_episode=prev_episode,
        next_episode=next_episode,
        more_episodes=more_episodes[:10],
        prefix=prefix,
        embed_mode=embed_mode,
    )


@router.get("/admin/videos/{video_id}/watch")
async def admin_video_preview_page(
    video_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis_conn: aioredis.Redis = Depends(get_redis),
):
    """Dedicated authenticated Owner/Admin preview path for PRIVATE and unshared videos."""
    admin_user = await get_authenticated_admin(request, db)
    if not admin_user:
        raise HTTPException(status_code=401, detail="Admin authentication required")

    stmt = (
        select(Video)
        .options(selectinload(Video.renditions))
        .where(Video.id == video_id)
    )
    res = await db.execute(stmt)
    video = res.scalar_one_or_none()
    if not video or video.status != VideoStatus.READY or not video.active_generation_id:
        raise HTTPException(status_code=404, detail="Video is not ready for playback")
    require_video_owner(video, admin_user)

    # Fetch active subtitles
    subtitles = await get_watch_subtitles(db, video.id)
    is_4k = any(r.quality_label in ("2160p", "4K") or (r.resolution_height and r.resolution_height >= 2160) for r in (video.renditions or []))
    has_ass = any((s.get("format") or "").lower() in ("ass", "ssa") or s.get("renderer") == "libass" for s in subtitles)

    token, cookie_hdr = await _init_playback_session(
        request=request,
        db=db,
        redis_conn=redis_conn,
        video=video,
        admin_user=admin_user,
        share_id=video.share_id,
    )

    return render_watch_template(
        title=video.title,
        share_id=video.share_id or "private-preview",
        playback_token=token,
        direct_media_url=direct_mp4_playback_url(video, token),
        cookie_header=cookie_hdr,
        poster_url=f"/api/v1/playback/{token}/poster.webp" if video.poster_path else "",
        is_admin_preview=True,
        subtitles=subtitles,
        library_meta=get_library_meta(video),
        series_name=video.series_name,
        season_number=video.season_number,
        episode_number=video.episode_number,
        description=video.description,
        is_4k=is_4k,
        has_ass=has_ass,
    )


async def get_authenticated_playback_user(request: Request, db: AsyncSession) -> Optional[User]:
    """Resolve any active local account for plan-aware playback."""
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not token:
        return None
    result = await db.execute(
        select(UserSession)
        .options(selectinload(UserSession.user))
        .where(
            UserSession.token_hash == hash_token(token),
            UserSession.expires_at > datetime.now(timezone.utc),
        )
    )
    session = result.scalar_one_or_none()
    if not session or not session.user or not session.user.is_active or session.user.role == UserRole.BANNED:
        return None
    return session.user
