"""Validated server encoder controls, captured when a transcode job is created."""
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from app.core.config import LADDER_PRESETS, settings
from app.models.setting import SystemSetting
from app.services.quality_settings import SETTING_KEY, normalize_qualities

ENCODER_SETTING_KEY = "server_encoder_profile"
ENCODER_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")


class EncoderPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preset: Literal["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]
    video_bitrate_percent: int = Field(default=100, ge=50, le=150, strict=True)


class EncoderSelection(EncoderPolicy):
    """A validated per-job choice; never writes the global settings."""
    enabled_qualities: list[Literal["360p", "480p", "720p", "1080p", "1440p", "2160p"]] = Field(min_length=1, max_length=6)

    @field_validator("enabled_qualities")
    @classmethod
    def permitted_qualities(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Duplicate rendition")
        if "2160p" in value and not settings.ENABLE_4K_TRANSCODING:
            raise ValueError("4K transcoding is disabled by deployment policy")
        return value


async def get_encoder_policy(db) -> EncoderPolicy:
    row = await db.get(SystemSetting, ENCODER_SETTING_KEY)
    return EncoderPolicy.model_validate_json(row.value) if row else EncoderPolicy(preset=settings.FFMPEG_PRESET)


def encoder_ladder_presets(policy: EncoderPolicy) -> dict:
    presets = deepcopy(LADDER_PRESETS)
    for preset in presets.values():
        preset["video_bitrate_kbps"] = max(300, round(preset["video_bitrate_kbps"] * policy.video_bitrate_percent / 100))
    return presets


async def read_transcode_controls(db):
    # One SQL statement observes the two policies from one committed DB snapshot.
    rows = (await db.execute(select(SystemSetting).where(SystemSetting.key.in_([SETTING_KEY, ENCODER_SETTING_KEY])))).scalars().all()
    values = {row.key: row.value for row in rows}
    policy = EncoderPolicy.model_validate_json(values[ENCODER_SETTING_KEY]) if ENCODER_SETTING_KEY in values else EncoderPolicy(preset=settings.FFMPEG_PRESET)
    return normalize_qualities(values.get(SETTING_KEY)), policy
