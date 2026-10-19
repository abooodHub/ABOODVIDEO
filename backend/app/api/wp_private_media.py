import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.config import settings
from app.core.database import get_db
from app.models.video import Video, VideoStatus, VideoVisibility

router = APIRouter(tags=["WordPress private media bridge"])
TOKEN_DOMAIN = b"okdonghua/private-media/v1."
KEY_DOMAIN = b"okdonghua/private-media/signing-key/v1"
SAFE_PART = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*$")


def _key(kid: str) -> bytes:
    ring_json = os.getenv("WP_PRIVATE_MEDIA_HMAC_KEYS_JSON", "")
    if ring_json:
        try:
            ring = json.loads(ring_json)
        except (TypeError, json.JSONDecodeError):
            raise HTTPException(503, "Private delivery unavailable")
        if not isinstance(ring, dict):
            raise HTTPException(503, "Private delivery unavailable")
        entry = ring.get(kid)
        if not isinstance(entry, dict) or entry.get("revoked") is True:
            raise HTTPException(404, "Media unavailable")
        until = entry.get("valid_until")
        if until is not None and (type(until) is not int or int(time.time()) >= until):
            raise HTTPException(404, "Media unavailable")
        raw = entry.get("key", "")
    else:
        if kid != os.getenv("WP_PRIVATE_MEDIA_KEY_ID"):
            raise HTTPException(404, "Media unavailable")
        raw = os.getenv("WP_PRIVATE_MEDIA_HMAC_KEY", "")
    if not isinstance(raw, str) or not re.fullmatch(r"[a-fA-F0-9]{64,128}", raw):
        raise HTTPException(503, "Private delivery unavailable")
    return bytes.fromhex(raw)


def _verify(token: str, resource: str) -> dict:
    if len(token) > 2048 or not SAFE_PART.fullmatch(resource.split("/")[-1] or ""):
        raise HTTPException(404, "Media unavailable")
    m = re.fullmatch(r"([A-Za-z0-9_-]+)\.([a-f0-9]{64})", token)
    if not m:
        raise HTTPException(404, "Media unavailable")
    payload = m.group(1)
    try:
        padded = payload + "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(padded.encode())
        claims = json.loads(raw)
    except Exception:
        raise HTTPException(404, "Media unavailable")
    expected_fields = ["v", "kid", "ep", "sid", "res", "scope", "sub", "iat", "exp"]
    if not isinstance(claims, dict) or list(claims) != expected_fields:
        raise HTTPException(404, "Media unavailable")
    canonical = json.dumps(claims, separators=(",", ":"), ensure_ascii=False)
    canonical_payload = base64.urlsafe_b64encode(canonical.encode()).decode().rstrip("=")
    now = int(time.time())
    kid = claims.get("kid")
    if canonical_payload != payload or claims.get("v") != 1 or not isinstance(kid, str) or not re.fullmatch(r"[a-z0-9_-]{1,32}", kid):
        raise HTTPException(404, "Media unavailable")
    key = _key(kid)
    expected = hmac.new(key, TOKEN_DOMAIN + payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, m.group(2)):
        raise HTTPException(404, "Media unavailable")
    if not all(isinstance(claims.get(k), int) for k in ("ep", "iat", "exp")) or claims["iat"] < 1 or claims["exp"] <= claims["iat"] or claims["exp"] - claims["iat"] > 300 or claims["iat"] > now + 15 or now >= claims["exp"]:
        raise HTTPException(404, "Media unavailable")
    if not re.fullmatch(r"[a-f0-9]{32}", str(claims.get("sid", ""))) or not re.fullmatch(r"[a-f0-9]{64}", str(claims.get("sub", ""))):
        raise HTTPException(404, "Media unavailable")
    if not isinstance(claims.get("res"), str) or not claims["res"].startswith(f'e{claims["ep"]}/{claims["sid"]}/') or claims.get("scope") not in ("file", "package"):
        raise HTTPException(404, "Media unavailable")
    parts = resource.split("/")
    if len(parts) < 4 or parts[0] != f'e{claims["ep"]}' or parts[1] != claims["sid"] or not re.fullmatch(r"[0-9a-fA-F-]{36}", parts[2]) or any(p in (".", "..") or not SAFE_PART.fullmatch(p) for p in parts):
        raise HTTPException(404, "Media unavailable")
    package_root = claims["res"].rsplit("/", 1)[0]
    if claims["scope"] == "file" and resource != claims["res"] or claims["scope"] == "package" and not (resource == claims["res"] or resource.startswith(package_root + "/")):
        raise HTTPException(404, "Media unavailable")
    return claims


def _cors_headers(request: Request) -> dict:
    origin = os.getenv("WP_PRIVATE_MEDIA_ALLOWED_ORIGIN", "")
    headers = {"Cache-Control": "private, no-store, max-age=0", "Pragma": "no-cache", "Vary": "Origin", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff", "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS", "Access-Control-Allow-Headers": "Range, Content-Type", "Access-Control-Expose-Headers": "Accept-Ranges, Content-Range, Content-Length", "Access-Control-Max-Age": "0"}
    if request.headers.get("origin") == origin:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Access-Control-Allow-Credentials"] = "true"
    headers["Vary"] = "Origin, Cookie"
    return headers


@router.api_route("/private-media/{token}/{resource:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def wordpress_private_media(token: str, resource: str, request: Request, db: AsyncSession = Depends(get_db)):
    # Every object and Range validates both the signed resource and login-session proof.
    claims = _verify(token, resource)
    origin = os.getenv("WP_PRIVATE_MEDIA_ALLOWED_ORIGIN", "")
    if request.headers.get("origin") not in (None, origin):
        raise HTTPException(403, "Origin denied")
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_cors_headers(request))
    credential = request.cookies.get("abv_media_session", "")
    if not re.fullmatch(r"[a-f0-9]{64}", credential):
        raise HTTPException(404, "Media unavailable")
    subject = hmac.new(_key(claims["kid"]), ("session|" + credential).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(subject, claims["sub"]):
        raise HTTPException(404, "Media unavailable")
    parts = resource.split("/")
    try:
        video_id = uuid.UUID(parts[2])
    except ValueError:
        raise HTTPException(404, "Media unavailable")
    video = await db.scalar(select(Video).where(Video.id == video_id))
    if not video or video.status != VideoStatus.READY or video.visibility != VideoVisibility.PRIVATE or not video.active_generation_id:
        raise HTTPException(404, "Media unavailable")
    base = Path(settings.STORAGE_VIDEOS_PATH) / str(video.id) / "generations" / str(video.active_generation_id)
    rel = "/".join(parts[3:])
    if rel == "key":
        from app.services import hls_crypto
        generation_key = hls_crypto.get_generation_key(video.id, video.active_generation_id)
        if not generation_key:
            raise HTTPException(404, "Media unavailable")
        headers = _cors_headers(request)
        headers["Content-Type"] = "application/octet-stream"
        headers["X-Content-Type-Options"] = "nosniff"
        return Response(content=generation_key, media_type="application/octet-stream", headers=headers)
    resolved = (base / rel).resolve()
    if not resolved.is_relative_to(base.resolve()) or not resolved.is_file():
        raise HTTPException(404, "Media unavailable")
    headers = _cors_headers(request)
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=headers)
    if resolved.suffix.lower() == ".m3u8":
        try:
            text = resolved.read_text(encoding="utf-8")
        except OSError:
            raise HTTPException(404, "Media unavailable")
        parent = "/".join(parts[3:-1])
        base_resource = "/".join(parts[:3])
        def protected(child: str) -> str:
            from urllib.parse import urlsplit
            parsed = urlsplit(child)
            if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or child.startswith("/"):
                raise HTTPException(404, "Media unavailable")
            stack = list(parts[3:-1])
            for item in parsed.path.split("/"):
                if item in ("", "."):
                    continue
                if item == "..":
                    if not stack:
                        raise HTTPException(404, "Media unavailable")
                    stack.pop()
                else:
                    if not SAFE_PART.fullmatch(item):
                        raise HTTPException(404, "Media unavailable")
                    stack.append(item)
            child_resource = base_resource + "/" + "/".join(stack)
            _verify(token, child_resource)
            return "/private-media/" + token + "/" + quote(child_resource, safe="/-_.")
        out = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                out.append(line)
            elif stripped.startswith("#"):
                out.append(re.sub(r'URI="([^"]+)"', lambda m: 'URI="' + protected(m.group(1)) + '"', line))
            else:
                out.append(protected(stripped))
        headers["Content-Type"] = "application/vnd.apple.mpegurl"
        return Response("\n".join(out) + "\n", headers=headers)
    media_type = {".ts": "video/mp2t", ".mp4": "video/mp4", ".m4s": "video/iso.segment", ".vtt": "text/vtt"}.get(resolved.suffix.lower(), "application/octet-stream")
    return FileResponse(resolved, media_type=media_type, headers=headers)
