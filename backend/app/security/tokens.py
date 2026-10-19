"""Secure Token Generation and Hashing Utilities."""
import hmac
import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple
from app.core.config import settings


def generate_secure_token(length_bytes: int = 32) -> str:
    """Generate a high-entropy URL-safe cryptographic token."""
    return secrets.token_urlsafe(length_bytes)


def hash_token(token: str) -> str:
    """Hash an API/session token with SHA-256 for secure database storage."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_signed_url_token(
    video_id: str,
    user_id: Optional[str] = None,
    quality: Optional[str] = None,
    ttl_seconds: int = settings.SIGNED_URL_TTL_SECONDS,
) -> str:
    """Generate an HMAC-SHA256 signed URL token for protected media streaming."""
    expires_at = int(datetime.now(timezone.utc).timestamp()) + ttl_seconds
    payload = f"{video_id}:{user_id or 'anon'}:{quality or 'all'}:{expires_at}"
    signature = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{expires_at}.{signature}"


def verify_signed_url_token(
    video_id: str,
    token: str,
    user_id: Optional[str] = None,
    quality: Optional[str] = None,
) -> bool:
    """Verify an HMAC-SHA256 signed URL token."""
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return False
        expires_at_str, signature = parts
        expires_at = int(expires_at_str)

        # Expiry check
        if datetime.now(timezone.utc).timestamp() > expires_at:
            return False

        payload = f"{video_id}:{user_id or 'anon'}:{quality or 'all'}:{expires_at}"
        expected = hmac.new(
            settings.SECRET_KEY.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
    except Exception:
        return False
