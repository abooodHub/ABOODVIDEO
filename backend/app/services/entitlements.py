"""Small local entitlement policy derived from the existing account model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from enum import StrEnum

from app.core.config import settings
from app.models.user import User, UserRole
from app.security.playback_session import quality_rank


@dataclass(frozen=True)
class PlaybackEntitlement:
    plan: str
    max_quality: str
    download_allowed: bool
    restricted_content: bool


class Entitlement(StrEnum):
    """Stable capability keys exposed to application services, never the UI."""

    PLAYBACK = "playback.watch"
    DOWNLOAD = "media.download"
    RESTRICTED_CONTENT = "content.restricted"


def _bounded_quality(requested: str, fallback: str) -> str:
    requested_rank = quality_rank(requested) or 0
    if requested_rank and requested_rank <= 2160:
        return requested
    return fallback


def playback_entitlement_for_user(user: Optional[User]) -> PlaybackEntitlement:
    """Resolve current user plan and caps; unknown/inactive identities fail closed."""
    if user is None or not user.is_active or user.role == UserRole.BANNED:
        return PlaybackEntitlement("FREE", settings.FREE_MAX_QUALITY, False, False)
    if user.role in (UserRole.OWNER, UserRole.ADMIN):
        return PlaybackEntitlement("PLATFORM_ADMIN", "2160p", True, True)

    now = datetime.now(timezone.utc)
    vip_active = user.role == UserRole.VIP or (
        user.vip_until is not None
        and (user.vip_until if user.vip_until.tzinfo else user.vip_until.replace(tzinfo=timezone.utc)) > now
    )
    if vip_active:
        return PlaybackEntitlement(
            "VIP",
            _bounded_quality(settings.VIP_MAX_QUALITY, "720p"),
            False,
            True,
        )
    return PlaybackEntitlement("FREE", settings.FREE_MAX_QUALITY, False, False)


def has_entitlement(user: Optional[User], capability: Entitlement | str) -> bool:
    """Fail-closed general capability check over the canonical plan projection."""
    try:
        required = Entitlement(capability)
    except (ValueError, TypeError):
        return False
    if user is None or not user.is_active or user.role == UserRole.BANNED:
        return False
    current = playback_entitlement_for_user(user)
    return {
        Entitlement.PLAYBACK: True,
        Entitlement.DOWNLOAD: current.download_allowed,
        Entitlement.RESTRICTED_CONTENT: current.restricted_content,
    }[required]
