"""HLS AES-128 encryption support (Phase 1, optional).

Segments produced by the transcoder are plaintext MPEG-TS. When enabled for a
generation, every segment is encrypted with AES-128-CBC using one key per
generation; the IV defaults to the HLS media sequence number (RFC 8216),
which keeps segment alignment, manual quality switching, resume, and subtitle
behavior unchanged. hls.js and Safari native HLS both support AES-128 HLS.

The key is NEVER stored under the publicly served media tree root naming and
is NEVER delivered through a static route. It lives in a private keys
directory and is streamed only through the session-authorized endpoint:

    GET /api/v1/playback/s/{session_id}/key

Variant playlists are rewritten at serve time to point at that endpoint, so a
copied segment URL yields ciphertext that is not directly playable, and the
key itself requires an authorized playback session.

Full DRM (Widevine / FairPlay / PlayReady) is intentionally out of scope for
Phase 1 and is documented as a future layer in the audit reports.
"""
import json
import os
import secrets
from pathlib import Path
from typing import Optional

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("hls_crypto")

ENCRYPTION_META_FILENAME = ".hls-encryption.json"
KEY_SUBDIR = "keys/hls"


def generation_dir(video_id, generation_id) -> Path:
    return (
        Path(settings.STORAGE_VIDEOS_PATH)
        / str(video_id)
        / "generations"
        / str(generation_id)
    )


def keys_root() -> Path:
    return Path(settings.STORAGE_BASE_PATH) / KEY_SUBDIR


def key_file_path(video_id, generation_id) -> Path:
    return keys_root() / str(video_id) / f"{generation_id}.key"


def get_encryption_metadata(video_id, generation_id) -> Optional[dict]:
    """Return encryption metadata for a generation, or None if plaintext."""
    meta_path = generation_dir(video_id, generation_id) / ENCRYPTION_META_FILENAME
    try:
        if not meta_path.exists():
            return None
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"Invalid HLS encryption metadata for {video_id}/{generation_id}: {exc}")
        return None
    if data.get("method") != "AES-128":
        return None
    return data


def generation_is_encrypted(video_id, generation_id) -> bool:
    return get_encryption_metadata(video_id, generation_id) is not None


def get_generation_key(video_id, generation_id) -> Optional[bytes]:
    """Load the 16-byte AES-128 key for a generation (must be encrypted)."""
    if not generation_is_encrypted(video_id, generation_id):
        return None
    path = key_file_path(video_id, generation_id)
    try:
        key = path.read_bytes()
    except OSError:
        return None
    return key if len(key) == 16 else None


def prepare_generation_key(video_id, generation_id) -> Optional[Path]:
    """Create (if needed) the key file for a generation and return its path.

    Used by the transcoder before packaging when ENABLE_HLS_ENCRYPTION is on.
    """
    path = key_file_path(video_id, generation_id)
    if path.exists():
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(secrets.token_bytes(16))
    except FileExistsError:
        pass
    except OSError as exc:
        logger.error(f"Failed to prepare HLS key {path}: {exc}")
        return None
    return path


def write_encryption_metadata(video_id, generation_id, key_uri_placeholder: str = "key") -> bool:
    """Mark a generation as AES-128 encrypted once segments are encrypted."""
    meta = {
        "method": "AES-128",
        "key_uri": key_uri_placeholder,
        "iv_mode": "sequence",
        "version": 1,
    }
    gdir = generation_dir(video_id, generation_id)
    try:
        gdir.mkdir(parents=True, exist_ok=True)
        (gdir / ENCRYPTION_META_FILENAME).write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logger.error(f"Failed to write encryption metadata for {video_id}/{generation_id}: {exc}")
        return False
    return True


def inject_key_tag(playlist_text: str, key_uri: str) -> str:
    """Rewrite a variant playlist to fetch its key from the protected endpoint.

    Any existing EXT-X-KEY line (e.g. a transcoder placeholder URI) is removed
    and replaced with a single session-scoped key URI placed before the first
    media segment. No IV attribute: players derive the IV from the media
    sequence number, matching the encryptor.
    """
    lines = playlist_text.splitlines()
    out = []
    inserted = False
    for line in lines:
        if line.startswith("#EXT-X-KEY"):
            continue
        if not inserted and line.startswith("#EXTINF"):
            out.append(f'#EXT-X-KEY:METHOD=AES-128,URI="{key_uri}"')
            inserted = True
        out.append(line)
    if not inserted:
        # No segments (e.g. empty placeholder playlist); keep as-is.
        return playlist_text
    return "\n".join(out) + "\n"
