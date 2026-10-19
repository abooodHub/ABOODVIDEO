"""Build and validate immutable server-transcode plans at job creation."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from app.core.config import LADDER_PRESETS, settings
from app.services.encoder_settings import EncoderPolicy, EncoderSelection, read_transcode_controls, encoder_ladder_presets
from app.services.quality_settings import QUALITY_ORDER

SNAPSHOT_VERSION = 1


async def build_transcode_configuration_snapshot(db, selection: EncoderSelection | None = None) -> dict[str, Any]:
    """Capture all mutable policy that can affect output for a new job."""
    if selection is None:
        enabled, policy = await read_transcode_controls(db)
    else:
        enabled = [quality for quality in QUALITY_ORDER if quality in selection.enabled_qualities]
        policy = EncoderPolicy(preset=selection.preset, video_bitrate_percent=selection.video_bitrate_percent)
    all_presets = encoder_ladder_presets(policy)
    presets = {label: deepcopy(all_presets[label]) for label in enabled}
    return {
        "schema_version": SNAPSHOT_VERSION,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "enabled_qualities": list(enabled),
        "quality_presets": presets,
        "source_fallback_presets": all_presets,
        "encoder": {
            "video_codec": "libx264",
            "audio_codec": "aac",
            "preset": policy.preset,
            "video_bitrate_percent": policy.video_bitrate_percent,
            "threads": settings.FFMPEG_THREADS,
        },
        "hls": {"segment_duration_seconds": settings.HLS_SEGMENT_DURATION_SECONDS},
        "policy": {
            "allow_4k_transcoding": settings.ENABLE_4K_TRANSCODING,
            "upscaling": False,
            "source_bitrate_cap_multiplier": 1.2,
        },
    }


def validate_transcode_configuration_snapshot(snapshot: Any) -> dict[str, Any]:
    """Reject malformed or unsupported persisted plans rather than silently drift."""
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != SNAPSHOT_VERSION:
        raise ValueError("Missing or unsupported transcode configuration snapshot")
    enabled = snapshot.get("enabled_qualities")
    presets = snapshot.get("quality_presets")
    encoder = snapshot.get("encoder")
    hls = snapshot.get("hls")
    policy = snapshot.get("policy")
    if (not isinstance(enabled, list) or not enabled or len(set(enabled)) != len(enabled)
            or any(label not in LADDER_PRESETS for label in enabled)
            or not isinstance(presets, dict) or set(presets) != set(enabled)
            or not isinstance(encoder, dict) or encoder.get("video_codec") != "libx264"
            or encoder.get("audio_codec") != "aac"
            or not isinstance(encoder.get("preset"), str) or not encoder["preset"]
            or not isinstance(encoder.get("threads"), int) or encoder["threads"] < 1
            or not isinstance(hls, dict) or not isinstance(hls.get("segment_duration_seconds"), int)
            or hls["segment_duration_seconds"] < 1
            or not isinstance(policy, dict) or policy.get("upscaling") is not False):
        raise ValueError("Invalid transcode configuration snapshot")
    for label in enabled:
        preset = presets[label]
        if (not isinstance(preset, dict)
                or not isinstance(preset.get("height"), int)
                or not isinstance(preset.get("video_bitrate_kbps"), int)
                or not isinstance(preset.get("audio_bitrate_kbps"), int)):
            raise ValueError(f"Invalid quality preset in transcode snapshot: {label}")
    fallback = snapshot.get("source_fallback_presets")
    if fallback is not None:
        if not isinstance(fallback, dict) or set(fallback) != set(LADDER_PRESETS):
            raise ValueError("Invalid source fallback presets")
        for preset in fallback.values():
            if not isinstance(preset, dict) or any(type(preset.get(key)) is not int or preset[key] <= 0 for key in ("height", "video_bitrate_kbps", "audio_bitrate_kbps")):
                raise ValueError("Invalid source fallback preset")
    return snapshot


def build_legacy_transcode_configuration_snapshot() -> dict[str, Any]:
    """Compatibility plan for jobs created before snapshot migration 0015."""
    enabled = [label for label in LADDER_PRESETS
               if label != "2160p" or settings.ENABLE_4K_TRANSCODING]
    return {
        "schema_version": SNAPSHOT_VERSION,
        "captured_at": None,
        "enabled_qualities": enabled,
        "quality_presets": {label: deepcopy(LADDER_PRESETS[label]) for label in enabled},
        "encoder": {"video_codec": "libx264", "audio_codec": "aac",
                    "preset": settings.FFMPEG_PRESET, "threads": settings.FFMPEG_THREADS},
        "hls": {"segment_duration_seconds": settings.HLS_SEGMENT_DURATION_SECONDS},
        "policy": {"allow_4k_transcoding": settings.ENABLE_4K_TRANSCODING,
                   "upscaling": False, "source_bitrate_cap_multiplier": 1.2},
    }
