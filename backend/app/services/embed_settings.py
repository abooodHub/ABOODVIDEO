"""Validated, DB-backed Embed & CDN policies for external sites."""
import json
import os
import re
import time
from typing import Optional, Dict, Any, List
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.setting import SystemSetting
from app.core.config import settings

# Setting keys
KEY_ALLOWED_DOMAINS = "allowed_embed_domains"
KEY_EMBED_ONLY_MODE = "embed_only_mode"
KEY_REDIRECT_URL = "embed_external_redirect_url"
KEY_DEFAULT_COLOR = "default_player_color"
KEY_DEFAULT_WATERMARK = "default_player_watermark"
KEY_DEFAULT_SKIP = "default_skip_intro_seconds"
KEY_PAUSE_BANNER = "default_pause_banner_json"

# In-memory cache
_CACHE: Dict[str, Any] = {}
_CACHE_EXPIRY: float = 0.0
CACHE_TTL_SECONDS = 30.0


def invalidate_embed_settings_cache():
    """Clear memory cache after admin updates settings."""
    global _CACHE, _CACHE_EXPIRY
    _CACHE.clear()
    _CACHE_EXPIRY = 0.0


def parse_domains_string(raw: Optional[str]) -> List[str]:
    """Parse comma/newline separated domains into cleaned lowercase list."""
    if raw is None:
        # Deployment configuration bootstraps the same policy only until a DB
        # setting is saved. It is not an independent CSP override.
        raw = os.environ.get("ABOODVIDEO_EMBED_ORIGINS", "localhost 127.0.0.1")
    domains = []
    # Split by comma or whitespace/newlines
    for part in re.split(r"[,\s\n\r]+", raw.strip()):
        d = part.strip().lower()
        # Remove any leading http:// or https:// or trailing slashes/ports
        d = re.sub(r"^https?://", "", d)
        d = d.split("/")[0].split(":")[0]
        if d and not re.fullmatch(r"(?:\*\.)?[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", d):
            raise ValueError("Invalid embed domain")
        if d and d not in domains:
            domains.append(d)
    

    return domains


def is_embed_host_allowed(host: str, domains: List[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d.removeprefix("*.") or host.endswith("." + d.removeprefix("*.")) for d in domains)


def build_frame_ancestors(domains: List[str]) -> str:
    origins = ["'self'"]
    for domain in domains:
        domain = domain.removeprefix("*.")
        if domain in {"localhost", "127.0.0.1"}:
            origins.extend([f"http://{domain}:*", f"https://{domain}:*"])
        else:
            for scheme in ("http", "https"):
                origins.extend([f"{scheme}://{domain}", f"{scheme}://*.{domain}"])
    return "frame-ancestors " + " ".join(dict.fromkeys(origins))


async def get_all_embed_settings(db: AsyncSession) -> Dict[str, Any]:
    """Retrieve full embed & CDN settings with short caching."""
    global _CACHE, _CACHE_EXPIRY
    now = time.monotonic()
    if _CACHE and now < _CACHE_EXPIRY:
        return dict(_CACHE)

    rows = {}
    try:
        # Fetch all keys
        for key in (
            KEY_ALLOWED_DOMAINS,
            KEY_EMBED_ONLY_MODE,
            KEY_REDIRECT_URL,
            KEY_DEFAULT_COLOR,
            KEY_DEFAULT_WATERMARK,
            KEY_DEFAULT_SKIP,
            KEY_PAUSE_BANNER,
        ):
            row = await db.get(SystemSetting, key)
            if row:
                rows[key] = row.value
    except Exception:
        pass

    # Process defaults
    allowed_domains_list = parse_domains_string(rows.get(KEY_ALLOWED_DOMAINS))
    
    # Parse pause banner
    pause_banner_raw = rows.get(KEY_PAUSE_BANNER)
    pause_banner = {
        "enabled": False,
        "title": "تابعنا على قنواتنا الرسمية",
        "text": "انضم إلينا لمتابعة جميع الحلقات والإصدارات الجديدة بجودة 4K",
        "link": "",
    }
    if pause_banner_raw:
        try:
            parsed = json.loads(pause_banner_raw)
            if isinstance(parsed, dict):
                pause_banner.update(parsed)
        except Exception:
            pass

    color = rows.get(KEY_DEFAULT_COLOR, "#00d2be")
    if not color or not color.startswith("#"):
        color = f"#{color.lstrip('#')}" if color else "#00d2be"

    skip_sec = 85
    try:
        if KEY_DEFAULT_SKIP in rows:
            skip_sec = int(rows[KEY_DEFAULT_SKIP])
    except (ValueError, TypeError):
        pass

    res = {
        "allowed_domains": allowed_domains_list,
        "allowed_domains_raw": ", ".join(allowed_domains_list),
        "embed_only_mode": rows.get(KEY_EMBED_ONLY_MODE, "false").lower() in ("1", "true", "yes"),
        "external_redirect_url": rows.get(KEY_REDIRECT_URL, ""),
        "default_color": color,
        "default_watermark": rows.get(KEY_DEFAULT_WATERMARK, "ABOODVIDEO"),
        "default_skip_intro": skip_sec,
        "pause_banner": pause_banner,
    }

    _CACHE = dict(res)
    _CACHE_EXPIRY = now + CACHE_TTL_SECONDS
    return res


async def save_embed_settings(db: AsyncSession, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Save updated embed & CDN settings to SystemSetting table."""
    # 1. Allowed domains
    if "allowed_domains_raw" in payload:
        domains_list = parse_domains_string(str(payload["allowed_domains_raw"]))
        val = ", ".join(domains_list)
        row = await db.get(SystemSetting, KEY_ALLOWED_DOMAINS)
        if not row:
            db.add(SystemSetting(key=KEY_ALLOWED_DOMAINS, value=val, description="Domains authorized to embed video player"))
        else:
            row.value = val

    # 2. Embed Only Mode
    if "embed_only_mode" in payload:
        mode_val = "true" if payload["embed_only_mode"] else "false"
        row = await db.get(SystemSetting, KEY_EMBED_ONLY_MODE)
        if not row:
            db.add(SystemSetting(key=KEY_EMBED_ONLY_MODE, value=mode_val, description="Disable direct platform browsing and restrict playback to external embeds"))
        else:
            row.value = mode_val

    # 3. External Redirect URL
    if "external_redirect_url" in payload:
        url_val = str(payload["external_redirect_url"]).strip()
        row = await db.get(SystemSetting, KEY_REDIRECT_URL)
        if not row:
            db.add(SystemSetting(key=KEY_REDIRECT_URL, value=url_val, description="Primary partner domain for redirecting direct viewers"))
        else:
            row.value = url_val

    # 4. Default color
    if "default_color" in payload:
        raw_col = str(payload["default_color"]).strip()
        clean = raw_col.lstrip("#")
        if re.fullmatch(r"[0-9a-fA-F]{3}|[0-9a-fA-F]{6}", clean):
            col_val = f"#{clean}"
            row = await db.get(SystemSetting, KEY_DEFAULT_COLOR)
            if not row:
                db.add(SystemSetting(key=KEY_DEFAULT_COLOR, value=col_val, description="Default player accent theme color"))
            else:
                row.value = col_val

    # 5. Default watermark
    if "default_watermark" in payload:
        wm_val = str(payload["default_watermark"]).strip()[:40] or "ABOODVIDEO"
        row = await db.get(SystemSetting, KEY_DEFAULT_WATERMARK)
        if not row:
            db.add(SystemSetting(key=KEY_DEFAULT_WATERMARK, value=wm_val, description="Default watermark text for embedded player"))
        else:
            row.value = wm_val

    # 6. Default skip intro
    if "default_skip_intro" in payload:
        try:
            sk_val = str(int(payload["default_skip_intro"]))
            row = await db.get(SystemSetting, KEY_DEFAULT_SKIP)
            if not row:
                db.add(SystemSetting(key=KEY_DEFAULT_SKIP, value=sk_val, description="Default skip intro button duration in seconds"))
            else:
                row.value = sk_val
        except (ValueError, TypeError):
            pass

    # 7. Pause Banner
    if "pause_banner" in payload and isinstance(payload["pause_banner"], dict):
        pb_val = json.dumps(payload["pause_banner"], ensure_ascii=False)
        row = await db.get(SystemSetting, KEY_PAUSE_BANNER)
        if not row:
            db.add(SystemSetting(key=KEY_PAUSE_BANNER, value=pb_val, description="Default pause promo banner configuration"))
        else:
            row.value = pb_val

    await db.commit()
    invalidate_embed_settings_cache()
    return await get_all_embed_settings(db)
