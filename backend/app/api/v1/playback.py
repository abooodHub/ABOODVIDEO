"""Secure HLS Playback and Media Delivery Controller.

Two authorization schemes are served by this controller:

1. Session-authorized routes (``/s/{session_id}/...``) — the production path.
   Every watch page creates a short-lived playback session in Redis and an
   HttpOnly ``abood_playback`` cookie. A request is authorized only when BOTH
   the session id in the URL and the cookie secret validate, the video still
   matches live database state, and the requested rendition is within the
   session's entitlement. Copied URLs are therefore visible but not reusable.

2. Legacy opaque-token routes (``/{token}/...``) — kept during the transition
   so watch pages rendered before a deployment keep working until their
   30-minute token TTL lapses. New pages always use sessions.

Media bytes are never proxied through FastAPI in production: authorization
returns lightweight responses carrying ``X-Accel-Redirect`` and nginx serves
the file from the private ``/internal_media/`` location.
"""
import hashlib
import json
import re
import urllib.parse
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, update, text
import redis.asyncio as aioredis

from app.core.config import settings
from app.core.database import get_db
from app.core.redis import get_redis
from app.core.logging import get_logger
from app.core.network import get_real_client_ip
from app.services.geo_access import enforce_geo_policy, trusted_country_code
from app.models.video import Video, VideoSubtitle, VideoFontAsset, PlaybackSession, VideoRendition, VideoVisibility
from app.models.setting import SystemSetting
from app.security.auth import require_owner_or_admin
from app.security.ownership import require_video_owner
from app.security.playback_token import (
    verify_playback_token,
    mask_token_for_logs,
)
from app.security.playback_session import (
    verify_playback_session,
    session_allows_quality,
    mask_session_for_logs,
    quality_rank,
)
from app.services.subtitles import SubtitleService
from app.services import hls_crypto

logger = get_logger("playback_api")

router = APIRouter(prefix="/playback", tags=["Playback"])


class PlaybackEventRequest(BaseModel):
    event: str  # "start", "heartbeat", "complete"
    position: Optional[float] = 0.0
    duration: Optional[float] = 0.0
    bytes_received: Optional[int] = 0
    quality: Optional[str] = None
    device_type: Optional[str] = None
    referrer: Optional[str] = None


def escape_m3u8_string(val: Optional[str]) -> str:
    """Sanitize and escape string attributes for HLS playlist tags (RFC 8216)."""
    if not val:
        return ""
    # Strip carriage returns and line feeds to prevent HTTP/playlist splitting
    sanitized = val.replace("\r", "").replace("\n", "")
    # Escape backslashes and double quotes
    sanitized = sanitized.replace("\\", "\\\\").replace('"', '\\"')
    return sanitized


def sanitize_and_resolve_path(base_dir: Path, user_path: str) -> Path:
    """Canonicalize path and enforce strict containment within base_dir.

    Rejects null bytes, percent-encoded traversal, and any resolved path outside base_dir.
    """
    # 1. Reject null bytes
    if "\x00" in user_path:
        raise HTTPException(status_code=400, detail="Invalid path")

    # 2. Decode percent-encoding safely
    try:
        decoded_path = urllib.parse.unquote(user_path)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path encoding")

    # 3. Quick check for traversal strings
    if ".." in decoded_path or decoded_path.startswith("/") or decoded_path.startswith("\\"):
        raise HTTPException(status_code=403, detail="Path traversal rejected")

    # 4. Canonical path resolution & containment check
    resolved = (base_dir / decoded_path).resolve()
    base_resolved = base_dir.resolve()

    if not resolved.is_relative_to(base_resolved):
        raise HTTPException(status_code=403, detail="Path traversal rejected")

    return resolved


# ---------------------------------------------------------------------------
# Shared media-serving internals
# ---------------------------------------------------------------------------

_M3U8 = "application/vnd.apple.mpegurl"
_PLAYBACK_QUALITY_SETTING = "playback_quality_policy"


async def _viewer_quality_policy(db: AsyncSession) -> set[str]:
    """Return configured viewer renditions (separate from transcode generation)."""
    row = await db.get(SystemSetting, _PLAYBACK_QUALITY_SETTING)
    if not row:
        return {"360p", "480p", "720p", "1080p", "1440p", "2160p"}
    try:
        configured = json.loads(row.value)
        if isinstance(configured, list) and configured:
            allowed = {str(value) for value in configured}
            if not settings.ENABLE_4K_TRANSCODING:
                allowed.discard("2160p")
            return allowed
    except (TypeError, ValueError):
        pass
    return {"360p", "480p", "720p", "1080p", "1440p", "2160p"}


def _auth_log(sid: str, video_id: str, asset: str, decision: str, status_code: int) -> None:
    """Privacy-safe playback authorization log line (no cookies/tokens/secrets)."""
    logger.info(
        f"playback_auth session={mask_session_for_logs(sid)} video={video_id} "
        f"asset={asset} decision={decision} status={status_code}"
    )


async def _enforce_legacy_geo(request: Request, db: AsyncSession, redis_conn: aioredis.Redis, token_data: dict) -> None:
    if token_data.get("authorization_type") != "admin":
        await enforce_geo_policy(request, db, redis_conn)


async def _authorize_session(
    request: Request,
    db: AsyncSession,
    redis_conn: aioredis.Redis,
    sid: str,
    asset: str,
) -> dict:
    """Authorize a session-scoped playback request and return its context."""
    secret = request.cookies.get(settings.PLAYBACK_COOKIE_NAME)
    ctx, reason = await verify_playback_session(db, redis_conn, sid, secret)
    if not ctx:
        status = 401 if reason in (
            "missing_credentials", "session_not_found", "session_expired",
            "session_corrupt", "principal_not_found", "principal_expired",
            "principal_mismatch", "video_unavailable", "generation_mismatch",
        ) else 403
        _auth_log(sid, "-", asset, f"deny:{reason}", status)
        raise HTTPException(status_code=status, detail="Playback session invalid or expired")
    if (ctx["session"].get("authorization_type") == "admin"
            or ctx["video"].visibility == VideoVisibility.PRIVATE):
        # Owner previews must retain their live login authority. The separate
        # playback cookie alone must not survive logout or account revocation.
        from app.security.auth import get_current_user
        from app.models.user import UserRole
        user = await get_current_user(request, db)
        if (user.role not in (UserRole.OWNER, UserRole.ADMIN)
                or str(user.id) != ctx["principal"].get("user_id")
                or (user.role != UserRole.ADMIN and ctx["video"].user_id != user.id)):
            raise HTTPException(status_code=403, detail="Playback preview authority is no longer valid")
    else:
        await enforce_geo_policy(request, db, redis_conn)
    if ctx.get("renew_cookie"):
        # Redis has a sliding lifetime; the browser credential needs the same
        # renewal. Mark it only after live login/ownership/geo checks succeed.
        request.state.playback_cookie_refresh = secret
    return ctx


async def _enforce_quality(ctx: dict, subpath: str, sid: str, db: AsyncSession) -> None:
    """Server-side rendition entitlement: requested quality must be allowed."""
    session = ctx["session"]
    if not session_allows_quality(session, subpath):
        video = ctx["video"]
        req_rank = quality_rank(subpath.split("/", 1)[0])
        _auth_log(sid, str(video.id), subpath, f"deny:quality_cap:{req_rank}>{session.get('max_quality_rank')}", 403)
        raise HTTPException(status_code=403, detail="Quality not permitted for this session")
    quality = (subpath or "").split("/", 1)[0]
    global_allowed = await _viewer_quality_policy(db)
    rank = quality_rank(quality)
    canonical_quality = f"{rank}p" if rank else quality
    if rank is not None and canonical_quality not in global_allowed:
        _auth_log(sid, str(ctx["video"].id), subpath, f"deny:viewer_quality_policy:{canonical_quality}", 403)
        raise HTTPException(status_code=403, detail="This quality is disabled by platform policy")
    rendition = await db.scalar(select(VideoRendition).where(
        VideoRendition.video_id == ctx["video"].id,
        VideoRendition.quality_label == quality,
        VideoRendition.is_ready.is_(True),
    ))
    if rendition and rendition.is_vip_only and not _plan_can_access_vip_renditions(ctx.get("principal")):
        _auth_log(sid, str(ctx["video"].id), subpath, "deny:vip_rendition", 403)
        raise HTTPException(status_code=403, detail="VIP entitlement required for this rendition")



def _filter_master_variants(master_text: str, max_rank: Optional[int], denied_ranks: Optional[set[int]] = None) -> str:
    """Remove renditions above the session's entitlement from the master playlist."""
    lines = master_text.splitlines()
    out = []
    skip_next_uri = False
    for line in lines:
        if line.startswith("#EXT-X-STREAM-INF:"):
            m = re.search(r"RESOLUTION=\d+x(\d+)", line)
            height = int(m.group(1)) if m else None
            if height and ((max_rank is not None and height > max_rank) or height in (denied_ranks or set())):
                skip_next_uri = True
                continue
        if skip_next_uri:
            skip_next_uri = False
            if line.startswith("#"):
                out.append(line)
            continue
        out.append(line)
    return "\n".join(out) + "\n"



async def _build_master_playlist(
    db: AsyncSession,
    video: Video,
    active_gen_id: uuid.UUID,
    max_quality_rank: Optional[int] = None,
    allow_vip_renditions: bool = True,
) -> str:
    """Dynamically build the master playlist with subtitle integration."""
    master_disk_path = (
        Path(settings.STORAGE_VIDEOS_PATH)
        / str(video.id)
        / "generations"
        / str(active_gen_id)
        / "master.m3u8"
    )

    if not master_disk_path.exists():
        logger.error(f"Master playlist missing on disk: {master_disk_path}")
        raise HTTPException(status_code=404, detail="Master playlist missing")

    raw_master = master_disk_path.read_text(encoding="utf-8")

    denied_vip_ranks: set[int] = set()
    if not allow_vip_renditions:
        vip_rows = await db.execute(select(VideoRendition.resolution_height).where(
            VideoRendition.video_id == video.id,
            VideoRendition.is_ready.is_(True),
            VideoRendition.is_vip_only.is_(True),
        ))
        denied_vip_ranks = {int(row[0]) for row in vip_rows.all() if row[0]}
    if max_quality_rank or denied_vip_ranks:
        raw_master = _filter_master_variants(raw_master, max_quality_rank, denied_vip_ranks)

    # Fetch active playable subtitles
    subs_stmt = (
        select(VideoSubtitle)
        .where(
            and_(
                VideoSubtitle.video_id == video.id,
                VideoSubtitle.is_playable == True,
                VideoSubtitle.webvtt_path.is_not(None),
            )
        )
        .order_by(VideoSubtitle.is_default.desc(), VideoSubtitle.created_at.asc())
    )
    subs_res = await db.execute(subs_stmt)
    subtitles = subs_res.scalars().all()

    if not subtitles:
        return raw_master if raw_master.endswith("\n") else raw_master + "\n"

    # Inject subtitle declarations into master playlist
    subtitle_lines = []
    for sub in subtitles:
        def_flag = "YES" if sub.is_default else "NO"
        forced_flag = "YES" if sub.is_forced else "NO"
        sub_uri = f"subtitles/{sub.id}/index.m3u8"
        safe_label = escape_m3u8_string(sub.label)
        safe_lang = escape_m3u8_string(sub.language_code)
        subtitle_lines.append(
            f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="{safe_label}",'
            f'DEFAULT={def_flag},AUTOSELECT=YES,FORCED={forced_flag},LANGUAGE="{safe_lang}",'
            f'URI="{sub_uri}"'
        )

    master_lines = raw_master.splitlines()
    new_master_lines = []
    inserted_subs = False

    for line in master_lines:
        if line.startswith("#EXT-X-STREAM-INF:") and not inserted_subs:
            new_master_lines.extend(subtitle_lines)
            new_master_lines.append("")
            inserted_subs = True

        if line.startswith("#EXT-X-STREAM-INF:"):
            if 'SUBTITLES="subs"' not in line:
                line = f'{line},SUBTITLES="subs"'
        new_master_lines.append(line)

    return "\n".join(new_master_lines) + "\n"



def _variant_playlist_response(ctx: dict, sid: str, subpath: str, resolved_file: Path) -> Response:
    """Serve a variant playlist, injecting the protected key tag when encrypted."""
    video = ctx["video"]
    active_gen_id = uuid.UUID(ctx["session"]["generation_id"])
    raw = resolved_file.read_text(encoding="utf-8")
    if hls_crypto.generation_is_encrypted(video.id, active_gen_id):
        key_uri = f"/api/v1/playback/s/{sid}/key"
        raw = hls_crypto.inject_key_tag(raw, key_uri)
    _auth_log(sid, str(video.id), subpath, "allow", 200)
    return Response(
        content=raw,
        media_type=_M3U8,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


def _media_file_response(video: Video, resolved_file: Path, internal_redirect: str, sid: str = "", subpath: str = "", playback_kbps: int = 0) -> Response:
    """Content-typed, cache-controlled media response via X-Accel-Redirect or direct file."""
    ext = resolved_file.suffix.lower()
    if ext == ".m3u8":
        media_type = _M3U8
        cache_header = "no-cache, no-store, must-revalidate"
    elif ext == ".ts":
        media_type = "video/mp2t"
        cache_header = "private, no-transform, max-age=86400"
    elif ext == ".vtt":
        media_type = "text/vtt"
        cache_header = "private, no-transform, max-age=86400"
    elif ext in (".ass", ".ssa"):
        media_type = "text/x-ssa; charset=utf-8"
        cache_header = "private, no-transform, max-age=86400"
    elif ext == ".ttf":
        media_type = "font/ttf"
        cache_header = "private, no-transform, max-age=86400, immutable"
    elif ext == ".otf":
        media_type = "font/otf"
        cache_header = "private, no-transform, max-age=86400, immutable"
    elif ext == ".woff2":
        media_type = "font/woff2"
        cache_header = "private, no-transform, max-age=86400, immutable"
    elif ext == ".mp4":
        media_type = "video/mp4"
        cache_header = "private, no-transform, no-cache"
    elif ext in (".key", ".bin"):
        media_type = "application/octet-stream"
        cache_header = "private, no-store, max-age=0"
    elif ext == ".webp":
        media_type = "image/webp"
        cache_header = "private, no-transform, max-age=86400"
    else:
        media_type = "application/octet-stream"
        cache_header = "no-cache"

    headers = {
        "Content-Type": media_type,
        "Cache-Control": cache_header,
        "X-Content-Type-Options": "nosniff",
    }
    if playback_kbps > 0:
        # Nginx applies this to this individual internal-redirect response.
        # It is deliberately described as per response, not an aggregate cap.
        headers["X-Accel-Limit-Rate"] = str(playback_kbps * 125)

    if sid:
        _auth_log(sid, str(video.id), subpath, "allow", 200)

    # Production delivery via Nginx X-Accel-Redirect
    if settings.SERVE_MEDIA_VIA_NGINX:
        headers["X-Accel-Redirect"] = internal_redirect
        return Response(status_code=200, headers=headers)
    # Fallback for direct testing when Nginx reverse proxy is not in front
    return FileResponse(path=str(resolved_file), media_type=media_type, headers=headers)


def _resolve_generation_asset(ctx: dict, subpath: str):
    """Resolve a generation-scoped asset path with traversal protection."""
    video = ctx["video"]
    active_gen_id = uuid.UUID(ctx["session"]["generation_id"])

    if subpath.startswith("subtitles/"):
        rel_sub = subpath[len("subtitles/"):]
        base_dir = Path(settings.STORAGE_VIDEOS_PATH) / str(video.id) / "subtitles"
        resolved = sanitize_and_resolve_path(base_dir, rel_sub)
        internal = f"/internal_media/{video.id}/subtitles/{rel_sub}"
    else:
        base_dir = (
            Path(settings.STORAGE_VIDEOS_PATH)
            / str(video.id)
            / "generations"
            / str(active_gen_id)
        )
        resolved = sanitize_and_resolve_path(base_dir, subpath)
        internal = f"/internal_media/{video.id}/generations/{active_gen_id}/{subpath}"

    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")
    return resolved, internal


async def _serve_subtitle_file(
    db: AsyncSession,
    video: Video,
    subtitle_id: uuid.UUID,
    sid: str = "",
) -> Response:
    """Serve authorized raw subtitle file (ASS/SSA or VTT) via internal redirect."""
    stmt = select(VideoSubtitle).where(
        and_(
            VideoSubtitle.id == subtitle_id,
            VideoSubtitle.video_id == video.id,
        )
    )
    result = await db.execute(stmt)
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subtitle not found")

    if sub.generation_id:
        base_dir = Path(settings.STORAGE_VIDEOS_PATH) / str(video.id) / "generations" / str(sub.generation_id)
        resolved_file = sanitize_and_resolve_path(base_dir, sub.source_path)
        internal_redirect = f"/internal_media/{video.id}/generations/{sub.generation_id}/{sub.source_path}"
    else:
        base_dir = Path(settings.STORAGE_VIDEOS_PATH) / str(video.id) / "subtitles"
        resolved_file = sanitize_and_resolve_path(base_dir, Path(sub.source_path).name)
        internal_redirect = f"/internal_media/{video.id}/subtitles/{Path(sub.source_path).name}"

    if not resolved_file.exists() or not resolved_file.is_file():
        raise HTTPException(status_code=404, detail="Subtitle file missing on disk")

    ext = resolved_file.suffix.lower()
    media_type = "text/x-ssa; charset=utf-8" if ext in (".ass", ".ssa") else "text/vtt; charset=utf-8"
    headers = {
        "Content-Type": media_type,
        "Cache-Control": "private, no-transform, max-age=86400",
        "X-Content-Type-Options": "nosniff",
    }
    if sid:
        _auth_log(sid, str(video.id), f"subtitles/{subtitle_id}/file", "allow", 200)
    if settings.SERVE_MEDIA_VIA_NGINX:
        headers["X-Accel-Redirect"] = internal_redirect
        return Response(status_code=200, headers=headers)
    return FileResponse(path=str(resolved_file), media_type=media_type, headers=headers)


async def _serve_font_asset(
    db: AsyncSession,
    video: Video,
    font_asset_id: uuid.UUID,
    active_gen_id: uuid.UUID,
    sid: str = "",
) -> Response:
    """Serve authorized font binary for true ASS subtitle rendering."""
    stmt = select(VideoFontAsset).where(
        and_(
            VideoFontAsset.id == font_asset_id,
            VideoFontAsset.video_id == video.id,
        )
    )
    result = await db.execute(stmt)
    font_asset = result.scalar_one_or_none()
    if not font_asset:
        raise HTTPException(status_code=404, detail="Font asset not found")

    gen_id = font_asset.generation_id or active_gen_id
    base_dir = Path(settings.STORAGE_VIDEOS_PATH) / str(video.id) / "generations" / str(gen_id)
    resolved_file = sanitize_and_resolve_path(base_dir, font_asset.relative_path)
    internal_redirect = f"/internal_media/{video.id}/generations/{gen_id}/{font_asset.relative_path}"

    if not resolved_file.exists() or not resolved_file.is_file():
        raise HTTPException(status_code=404, detail="Font binary missing on disk")

    ext = resolved_file.suffix.lower()
    media_type = "font/otf" if ext == ".otf" else "font/ttf"
    headers = {
        "Content-Type": media_type,
        "Cache-Control": "private, no-transform, max-age=86400, immutable",
        "X-Content-Type-Options": "nosniff",
    }
    if sid:
        _auth_log(sid, str(video.id), f"fonts/{font_asset_id}", "allow", 200)
    if settings.SERVE_MEDIA_VIA_NGINX:
        headers["X-Accel-Redirect"] = internal_redirect
        return Response(status_code=200, headers=headers)
    return FileResponse(path=str(resolved_file), media_type=media_type, headers=headers)


async def _track_event(
    db: AsyncSession,
    request: Request,
    video: Video,
    body: PlaybackEventRequest,
    viewer_key: str,
) -> dict:
    """Privacy-conscious viewer playback analytics tracking."""
    client_ip = get_real_client_ip(request)
    ip_hash = hashlib.sha256(f"{client_ip}:{settings.SECRET_KEY}".encode()).hexdigest()
    user_agent = request.headers.get("User-Agent", "")[:128]

    # Lock the logical session before its first row exists. Row locks alone cannot
    # serialize concurrent first heartbeats, and each update must see fresh totals.
    lock_key = int.from_bytes(hashlib.sha256(viewer_key.encode()).digest()[:8], "big", signed=True)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    stmt = select(PlaybackSession).where(PlaybackSession.session_id == viewer_key).execution_options(populate_existing=True)
    result = await db.execute(stmt)
    session_rec = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)

    # Extract referrer domain safely
    raw_ref = body.referrer or request.headers.get("referer") or request.headers.get("origin") or ""
    referrer_domain = None
    if raw_ref:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(raw_ref)
            referrer_domain = parsed.netloc or parsed.path.split("/")[0]
            if ":" in referrer_domain:
                referrer_domain = referrer_domain.split(":")[0]
        except Exception:
            referrer_domain = None

    if not session_rec:
        session_rec = PlaybackSession(
            id=uuid.uuid4(),
            video_id=video.id,
            session_id=viewer_key,
            ip_hash=ip_hash,
            country_code=trusted_country_code(request),
            user_agent_short=user_agent,
            started_at=now,
            last_heartbeat_at=now,
            duration_seconds=max(body.duration or 0.0, 0.0),
            bytes_served=max(body.bytes_received or 0, 0),
            is_completed=False,
            quality=(body.quality or "Auto")[:32] if body.quality else None,
            device_type=(body.device_type or "desktop")[:32] if body.device_type else None,
            referrer_domain=referrer_domain[:255] if referrer_domain else None,
        )
        db.add(session_rec)
    else:
        session_rec.last_heartbeat_at = now
        session_rec.duration_seconds += max(body.duration or 0.0, 0.0)
        session_rec.bytes_served += max(body.bytes_received or 0, 0)
        if body.quality:
            session_rec.quality = body.quality[:32]
        if body.device_type:
            session_rec.device_type = body.device_type[:32]
        if not session_rec.country_code:
            session_rec.country_code = trusted_country_code(request)
        if referrer_domain and not session_rec.referrer_domain:
            session_rec.referrer_domain = referrer_domain[:255]

    # Track bandwidth bytes on video record
    if body.bytes_received and body.bytes_received > 0:
        await db.execute(
            update(Video)
            .where(Video.id == video.id)
            .values(bandwidth_bytes=Video.bandwidth_bytes + int(body.bytes_received))
        )

    # View count threshold logic: minimum 10s playback (or 50% for very short video)
    threshold = min(10.0, (video.source_duration_seconds or 10.0) * 0.5)
    if not session_rec.is_completed and session_rec.duration_seconds >= threshold:
        session_rec.is_completed = True
        await db.execute(
            update(Video)
            .where(Video.id == video.id)
            .values(view_count=Video.view_count + 1)
        )

    await db.commit()
    return {"status": "ok", "duration": session_rec.duration_seconds}


# ---------------------------------------------------------------------------
# Session-authorized routes (production path) — declared first so they win
# routing over the legacy `/{token}/{subpath:path}` catch-all.
# ---------------------------------------------------------------------------

@router.get("/s/{sid}/master.m3u8")
async def session_master_playlist(
    sid: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis_conn: aioredis.Redis = Depends(get_redis),
):
    """Session-authorized master playlist, filtered to entitled renditions."""
    ctx = await _authorize_session(request, db, redis_conn, sid, "master.m3u8")
    video: Video = ctx["video"]
    max_rank = int(ctx["session"].get("max_quality_rank") or 0) or None
    global_allowed = await _viewer_quality_policy(db)
    global_denied = {quality_rank(q) for q in {"360p", "480p", "720p", "1080p", "1440p", "2160p"} - global_allowed}
    # _build_master_playlist filters VIP and session max rank; also remove
    # globally hidden renditions before returning the master playlist.
    content = await _build_master_playlist(db, video, video.active_generation_id, max_rank, allow_vip_renditions=_plan_can_access_vip_renditions(ctx.get("principal")))
    content = _filter_master_variants(content, None, {rank for rank in global_denied if rank is not None})
    _auth_log(sid, str(video.id), "master.m3u8", "allow", 200)
    return Response(
        content=content,
        media_type=_M3U8,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.get("/direct-download/{video_id}")
async def admin_direct_download(
    video_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_owner_or_admin),
):
    """Owner/admin source download with the configured Nginx response limit."""
    video = await db.scalar(select(Video).where(Video.id == video_id, Video.trashed_at.is_(None)))
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    require_video_owner(video, current_user)
    source = Path(video.source_path or "")
    if not video.source_path or not source.is_file():
        raise HTTPException(status_code=404, detail="Source file is unavailable")
    from app.api.v1.admin_transfer_settings import get_transfer_policy
    policy = await get_transfer_policy(db)
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    if policy["playback_kbps"] > 0:
        headers["X-Accel-Limit-Rate"] = str(policy["playback_kbps"] * 125)
    filename = Path(video.source_filename or source.name).name
    return FileResponse(path=str(source), media_type="application/octet-stream", filename=filename, headers=headers)


@router.get("/s/{sid}/subtitles/{sub_id}/index.m3u8")
async def session_subtitle_media_playlist(
    sid: str,
    sub_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis_conn: aioredis.Redis = Depends(get_redis),
):
    """Session-authorized WebVTT subtitle media playlist."""
    ctx = await _authorize_session(request, db, redis_conn, sid, f"subtitles/{sub_id}/index.m3u8")
    video: Video = ctx["video"]
    stmt = select(VideoSubtitle).where(
        and_(
            VideoSubtitle.id == sub_id,
            VideoSubtitle.video_id == video.id,
            VideoSubtitle.is_playable == True,
            VideoSubtitle.webvtt_path.is_not(None),
        )
    )
    result = await db.execute(stmt)
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subtitle not found")

    duration = float(video.source_duration_seconds or 10.0)
    playlist_content = SubtitleService.generate_hls_subtitle_playlist(
        duration_seconds=duration,
        vtt_filename=f"../{sub.id}.vtt",
    )
    _auth_log(sid, str(video.id), f"subtitles/{sub_id}/index.m3u8", "allow", 200)
    return Response(
        content=playlist_content,
        media_type=_M3U8,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.get("/s/{sid}/subtitles/{subtitle_id}/file")
async def session_subtitle_file(
    sid: str,
    subtitle_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis_conn: aioredis.Redis = Depends(get_redis),
):
    """Session-authorized raw ASS/SSA/VTT subtitle bytes."""
    ctx = await _authorize_session(request, db, redis_conn, sid, f"subtitles/{subtitle_id}/file")
    return await _serve_subtitle_file(db, ctx["video"], subtitle_id, sid)


@router.get("/s/{sid}/fonts/{font_asset_id}")
async def session_font_asset(
    sid: str,
    font_asset_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis_conn: aioredis.Redis = Depends(get_redis),
):
    """Session-authorized font binary for ASS rendering."""
    ctx = await _authorize_session(request, db, redis_conn, sid, f"fonts/{font_asset_id}")
    active_gen_id = uuid.UUID(ctx["session"]["generation_id"])
    return await _serve_font_asset(db, ctx["video"], font_asset_id, active_gen_id, sid)


@router.get("/s/{sid}/key")
async def session_hls_key(
    sid: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis_conn: aioredis.Redis = Depends(get_redis),
):
    """Session-authorized AES-128 key delivery (encrypted generations only)."""
    ctx = await _authorize_session(request, db, redis_conn, sid, "key")
    video: Video = ctx["video"]
    active_gen_id = uuid.UUID(ctx["session"]["generation_id"])
    key = hls_crypto.get_generation_key(video.id, active_gen_id)
    if not key:
        raise HTTPException(status_code=404, detail="No encryption key for this media")
    _auth_log(sid, str(video.id), "key", "allow", 200)
    return Response(
        content=key,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/s/{sid}/event")
async def session_track_event(
    sid: str,
    body: PlaybackEventRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis_conn: aioredis.Redis = Depends(get_redis),
):
    """Session-authorized playback analytics."""
    ctx = await _authorize_session(request, db, redis_conn, sid, "event")
    video: Video = ctx["video"]
    viewer_key = f"pb_{sid[:12]}_{video.id}"
    return await _track_event(db, request, video, body, viewer_key)


@router.get("/s/{sid}/{subpath:path}")
async def session_serve_media(
    sid: str,
    subpath: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis_conn: aioredis.Redis = Depends(get_redis),
):
    """Session-authorized media catch-all: variants, segments, VTT, poster."""
    ctx = await _authorize_session(request, db, redis_conn, sid, subpath)
    await _enforce_quality(ctx, subpath, sid, db)
    resolved, internal = _resolve_generation_asset(ctx, subpath)

    if subpath.endswith(".m3u8") and "/" in subpath:
        return _variant_playlist_response(ctx, sid, subpath, resolved)
    from app.api.v1.admin_transfer_settings import get_transfer_policy
    transfer_policy = await get_transfer_policy(db)
    return _media_file_response(ctx["video"], resolved, internal, sid, subpath,
                                playback_kbps=transfer_policy["playback_kbps"])


# ---------------------------------------------------------------------------
# Legacy opaque-token routes (transition window only). New watch pages use the
# session routes above; these remain so pre-deploy pages survive until their
# 30-minute token TTL expires.
# ---------------------------------------------------------------------------

@router.get("/{token}/master.m3u8")
async def get_master_playlist(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis_conn: aioredis.Redis = Depends(get_redis),
):
    """Dynamically serve authorized HLS master playlist with soft subtitle integration."""
    token_data = await verify_playback_token(db, redis_conn, token)
    if not token_data:
        logger.warning("Unauthorized master playlist request (invalid or expired token)")
        raise HTTPException(status_code=404, detail="Media not found or expired")
    await _enforce_legacy_geo(request, db, redis_conn, token_data)

    video: Video = token_data["video"]
    global_allowed = await _viewer_quality_policy(db)
    global_denied = {quality_rank(q) for q in {"360p", "480p", "720p", "1080p", "1440p", "2160p"} - global_allowed}
    legacy_plan = str(token_data.get("plan", "")).lower()
    legacy_vip_allowed = token_data.get("authorization_type") == "admin" or legacy_plan in {"vip", "owner", "admin", "platform_admin"}
    content = await _build_master_playlist(
        db, video, token_data["active_generation_id"], allow_vip_renditions=legacy_vip_allowed,
    )
    content = _filter_master_variants(content, None, {rank for rank in global_denied if rank is not None})
    return Response(
        content=content,
        media_type=_M3U8,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )



@router.get("/{token}/subtitles/{sub_id}/index.m3u8")
async def get_subtitle_media_playlist(
    token: str,
    sub_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis_conn: aioredis.Redis = Depends(get_redis),
):
    """Serve dynamic HLS WebVTT subtitle playlist."""
    token_data = await verify_playback_token(db, redis_conn, token)
    if not token_data:
        raise HTTPException(status_code=404, detail="Media not found or expired")
    await _enforce_legacy_geo(request, db, redis_conn, token_data)

    video: Video = token_data["video"]
    stmt = select(VideoSubtitle).where(
        and_(
            VideoSubtitle.id == sub_id,
            VideoSubtitle.video_id == video.id,
            VideoSubtitle.is_playable == True,
            VideoSubtitle.webvtt_path.is_not(None),
        )
    )
    result = await db.execute(stmt)
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subtitle not found")

    duration = float(video.source_duration_seconds or 10.0)
    playlist_content = SubtitleService.generate_hls_subtitle_playlist(
        duration_seconds=duration,
        vtt_filename=f"../{sub.id}.vtt",
    )
    return Response(
        content=playlist_content,
        media_type=_M3U8,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.get("/{token_or_video_id}/subtitles/{subtitle_id}/file")
async def get_subtitle_file(
    token_or_video_id: str,
    subtitle_id: uuid.UUID,
    request: Request,
    token: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    redis_conn: aioredis.Redis = Depends(get_redis),
):
    """
    Serve authorized raw subtitle file (ASS/SSA or VTT).
    Accepts token as first path param, or video_id as path param with ?token=... query/header.
    """
    actual_token = token
    expected_video_id = None
    try:
        expected_video_id = uuid.UUID(token_or_video_id)
        if not actual_token:
            actual_token = request.headers.get("X-Playback-Token") or request.query_params.get("token")
    except ValueError:
        actual_token = token_or_video_id

    if not actual_token:
        raise HTTPException(status_code=401, detail="Playback token required")

    token_data = await verify_playback_token(db, redis_conn, actual_token)
    if not token_data:
        raise HTTPException(status_code=404, detail="Media not found or expired")
    await _enforce_legacy_geo(request, db, redis_conn, token_data)

    video: Video = token_data["video"]
    if expected_video_id and video.id != expected_video_id:
        raise HTTPException(status_code=403, detail="Token mismatch for video")

    return await _serve_subtitle_file(db, video, subtitle_id)


@router.get("/{token_or_video_id}/fonts/{font_asset_id}")
async def get_font_asset(
    token_or_video_id: str,
    font_asset_id: uuid.UUID,
    request: Request,
    token: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    redis_conn: aioredis.Redis = Depends(get_redis),
):
    """
    Serve authorized font binary asset for true ASS subtitle rendering.
    Accepts token as first path param, or video_id as path param with ?token=... query/header.
    Opaque asset ID is resolved from database - arbitrary filesystem traversal is rejected.
    """
    actual_token = token
    expected_video_id = None
    try:
        expected_video_id = uuid.UUID(token_or_video_id)
        if not actual_token:
            actual_token = request.headers.get("X-Playback-Token") or request.query_params.get("token")
    except ValueError:
        actual_token = token_or_video_id

    if not actual_token:
        raise HTTPException(status_code=401, detail="Playback token required")

    token_data = await verify_playback_token(db, redis_conn, actual_token)
    if not token_data:
        raise HTTPException(status_code=404, detail="Media not found or expired")
    await _enforce_legacy_geo(request, db, redis_conn, token_data)

    video: Video = token_data["video"]
    if expected_video_id and video.id != expected_video_id:
        raise HTTPException(status_code=403, detail="Token mismatch for video")

    return await _serve_font_asset(db, video, font_asset_id, token_data["active_generation_id"])


@router.get("/{token}/{subpath:path}")
async def serve_protected_media(
    token: str,
    subpath: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis_conn: aioredis.Redis = Depends(get_redis),
):
    """Authorize and deliver protected media assets via Nginx X-Accel-Redirect."""
    token_data = await verify_playback_token(db, redis_conn, token)
    if not token_data:
        raise HTTPException(status_code=404, detail="Media not found or expired")

    await _enforce_legacy_geo(request, db, redis_conn, token_data)
    video: Video = token_data["video"]
    active_gen_id = token_data["active_generation_id"]
    requested_quality = (subpath or "").split("/", 1)[0]
    global_allowed = await _viewer_quality_policy(db)
    requested_rank = quality_rank(requested_quality)
    if requested_rank is not None and f"{requested_rank}p" not in global_allowed:
        raise HTTPException(status_code=403, detail="This quality is disabled by platform policy")
    rendition = await db.scalar(select(VideoRendition).where(
        VideoRendition.video_id == video.id,
        VideoRendition.quality_label == requested_quality,
        VideoRendition.is_ready.is_(True),
    ))
    legacy_plan = str(token_data.get("plan", "")).lower()
    legacy_vip_allowed = token_data.get("authorization_type") == "admin" or legacy_plan in {"vip", "owner", "admin", "platform_admin"}
    if rendition and rendition.is_vip_only and not legacy_vip_allowed:
        raise HTTPException(status_code=403, detail="VIP entitlement required for this rendition")

    class _LegacyCtx(dict):
        pass

    ctx = _LegacyCtx()
    ctx["video"] = video
    ctx["session"] = {"generation_id": str(active_gen_id)}
    resolved, internal = _resolve_generation_asset(ctx, subpath)
    from app.api.v1.admin_transfer_settings import get_transfer_policy
    transfer_policy = await get_transfer_policy(db)
    return _media_file_response(video, resolved, internal,
                                playback_kbps=transfer_policy["playback_kbps"])



@router.post("/{token}/event")
async def track_playback_event(
    token: str,
    body: PlaybackEventRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis_conn: aioredis.Redis = Depends(get_redis),
):
    """Privacy-conscious viewer playback analytics tracking."""
    token_data = await verify_playback_token(db, redis_conn, token)
    if not token_data:
        raise HTTPException(status_code=404, detail="Session expired")
    await _enforce_legacy_geo(request, db, redis_conn, token_data)

    video: Video = token_data["video"]
    viewer_session_id = token_data.get("viewer_session_id") or "anon"
    return await _track_event(db, request, video, body, f"{viewer_session_id}_{video.id}")


def _plan_can_access_vip_renditions(principal: dict) -> bool:
    return str((principal or {}).get("plan", "")).lower() in {
        "vip", "owner", "admin", "platform_admin",
    }
