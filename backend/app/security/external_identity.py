"""Optional compact HMAC identity assertion contract for external consumers.

The verifier accepts only short-lived HS256 assertions with explicit issuer,
audience, subject, issue/expiry timestamps, and one-time JTI. It creates no
local account and never treats WordPress as the platform identity database.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from typing import Any, Mapping


class ExternalIdentityError(ValueError):
    """Safe validation failure for an untrusted external identity assertion."""


_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PLANS = {"FREE", "MEMBER", "VIP"}
_QUALITIES = {"360p", "480p", "720p", "1080p", "1440p", "2160p"}


def _decode(segment: str) -> bytes:
    if not _SEGMENT_RE.fullmatch(segment):
        raise ExternalIdentityError("Malformed identity assertion")
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (ValueError, TypeError) as exc:
        raise ExternalIdentityError("Malformed identity assertion") from exc


async def verify_external_identity(
    token: str,
    *,
    secret: str,
    issuer: str,
    audience: str,
    replay_store: Any,
    now: int | None = None,
    max_lifetime_seconds: int = 600,
) -> dict[str, Any]:
    """Verify claims, signature, lifetime, audience and one-time replay key."""
    if len(secret) < 32 or not issuer or not audience or replay_store is None:
        raise ExternalIdentityError("External identity verification is not configured")
    if not isinstance(token, str) or len(token) > 8192:
        raise ExternalIdentityError("Malformed identity assertion")
    pieces = token.split(".")
    if len(pieces) != 3:
        raise ExternalIdentityError("Malformed identity assertion")

    header_raw, claims_raw, signature_raw = pieces
    signing_input = f"{header_raw}.{claims_raw}".encode("ascii")
    expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _decode(signature_raw)):
        raise ExternalIdentityError("Invalid identity assertion signature")
    try:
        header = json.loads(_decode(header_raw))
        claims = json.loads(_decode(claims_raw))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ExternalIdentityError("Malformed identity assertion") from exc
    if not isinstance(header, dict) or header.get("alg") != "HS256" or header.get("typ") != "JWT":
        raise ExternalIdentityError("Unsupported identity assertion algorithm")
    if not isinstance(claims, dict):
        raise ExternalIdentityError("Malformed identity claims")

    current = int(time.time()) if now is None else int(now)
    try:
        issued = int(claims["iat"])
        expires = int(claims["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ExternalIdentityError("Identity assertion timestamps are required") from exc
    if claims.get("iss") != issuer or claims.get("aud") != audience:
        raise ExternalIdentityError("Identity assertion issuer or audience mismatch")
    if issued > current + 60 or expires <= current or expires <= issued:
        raise ExternalIdentityError("Identity assertion is expired or not yet valid")
    if expires - issued > max_lifetime_seconds:
        raise ExternalIdentityError("Identity assertion lifetime exceeds policy")

    subject = claims.get("sub")
    jti = claims.get("jti")
    plan = claims.get("plan")
    entitlements = claims.get("entitlements", {})
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 256:
        raise ExternalIdentityError("Invalid external identity subject")
    if not isinstance(jti, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", jti):
        raise ExternalIdentityError("Invalid identity assertion identifier")
    if plan not in _PLANS:
        raise ExternalIdentityError("Unsupported external identity plan")
    if not isinstance(entitlements, dict):
        raise ExternalIdentityError("Invalid identity entitlements")
    max_quality = entitlements.get("max_quality", "720p")
    if max_quality not in _QUALITIES:
        raise ExternalIdentityError("Invalid max-quality entitlement")
    safe_entitlements = {
        "max_quality": max_quality,
        "restricted_content": entitlements.get("restricted_content") is True,
        "download": entitlements.get("download") is True,
    }

    accepted = await replay_store.set(
        f"external_identity:jti:{jti}", "1", nx=True, ex=max(1, expires - current)
    )
    if not accepted:
        raise ExternalIdentityError("Identity assertion replay detected")
    return {
        "external_user_id": subject,
        "display_name": str(claims.get("name", ""))[:120],
        "plan": plan,
        "entitlements": safe_entitlements,
        "issued_at": issued,
        "expires_at": expires,
    }
