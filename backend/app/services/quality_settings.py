"""Validated, DB-backed rendition policy shared by admin and transcode worker."""
from app.core.config import LADDER_PRESETS, settings
from app.models.setting import SystemSetting

SETTING_KEY = "enabled_transcode_qualities"
QUALITY_ORDER = tuple(LADDER_PRESETS)
DEFAULT_QUALITY_ORDER = tuple(q for q in QUALITY_ORDER if q in {"480p", "720p", "1080p", "2160p"})


def normalize_qualities(value: str | None) -> list[str]:
    requested = set(value.split(",")) if value else set(DEFAULT_QUALITY_ORDER)
    if not requested or requested - set(QUALITY_ORDER):
        raise ValueError("Invalid rendition selection")
    if not settings.ENABLE_4K_TRANSCODING:
        requested.discard("2160p")
    if not requested:
        raise ValueError("At least one permitted rendition must remain enabled")
    return [quality for quality in QUALITY_ORDER if quality in requested]


def get_enabled_qualities(session) -> list[str]:
    row = session.get(SystemSetting, SETTING_KEY)
    return normalize_qualities(row.value if row else None)


async def get_enabled_qualities_async(session) -> list[str]:
    row = await session.get(SystemSetting, SETTING_KEY)
    return normalize_qualities(row.value if row else None)
