"""Immutable per-job bitrate profiles and approximate output sizing."""
from __future__ import annotations

from typing import Any, Dict, Iterable

RATE_PROFILES: Dict[str, Dict[str, Dict[str, int]]] = {
    "economy": {
        "2160p": {"video_bitrate_bps": 16_000_000, "audio_bitrate_bps": 192_000},
        "1080p": {"video_bitrate_bps": 3_200_000, "audio_bitrate_bps": 128_000},
        "720p": {"video_bitrate_bps": 1_800_000, "audio_bitrate_bps": 96_000},
        "480p": {"video_bitrate_bps": 800_000, "audio_bitrate_bps": 80_000},
        "360p": {"video_bitrate_bps": 700_000, "audio_bitrate_bps": 96_000},
    },
    "balanced": {
        "2160p": {"video_bitrate_bps": 16_000_000, "audio_bitrate_bps": 192_000},
        "1080p": {"video_bitrate_bps": 4_500_000, "audio_bitrate_bps": 160_000},
        "720p": {"video_bitrate_bps": 2_500_000, "audio_bitrate_bps": 128_000},
        "480p": {"video_bitrate_bps": 1_200_000, "audio_bitrate_bps": 128_000},
        "360p": {"video_bitrate_bps": 700_000, "audio_bitrate_bps": 96_000},
    },
    "quality": {
        "2160p": {"video_bitrate_bps": 16_000_000, "audio_bitrate_bps": 192_000},
        "1080p": {"video_bitrate_bps": 6_000_000, "audio_bitrate_bps": 192_000},
        "720p": {"video_bitrate_bps": 3_500_000, "audio_bitrate_bps": 160_000},
        "480p": {"video_bitrate_bps": 1_800_000, "audio_bitrate_bps": 128_000},
        "360p": {"video_bitrate_bps": 700_000, "audio_bitrate_bps": 96_000},
    },
}

NVENC_PRESETS = {"economy": "p3", "balanced": "p5", "quality": "p7"}
ENCODER_PRESETS = {
    "nvenc": NVENC_PRESETS,
    "qsv": {"economy": "veryfast", "balanced": "medium", "quality": "slower"},
    "amf": {"economy": "speed", "balanced": "balanced", "quality": "quality"},
    "cpu": {"economy": "veryfast", "balanced": "medium", "quality": "slow"},
}


def normalize_preset_name(value: Any) -> str:
    name = str(value or "balanced").strip().casefold()
    if name == "fast":
        name = "economy"
    return name if name in RATE_PROFILES else "balanced"


def preset_identity(value: Any, include_360p: bool) -> Dict[str, Any]:
    name = normalize_preset_name(value)
    return {
        "preset_name": name,
        "video_bitrate_bps_per_rendition": {
            quality: values["video_bitrate_bps"]
            for quality, values in RATE_PROFILES[name].items()
            if quality != "360p" or include_360p
        },
        "audio_bitrate_bps_per_rendition": {
            quality: values["audio_bitrate_bps"]
            for quality, values in RATE_PROFILES[name].items()
            if quality != "360p" or include_360p
        },
        "nvenc_preset": NVENC_PRESETS[name],
        "rate_control_mode": "vbr",
        "cq": 22,
        "four_k_strategy": "source_copy_if_compatible_else_transcode",
        "include_360p": bool(include_360p),
    }


def selected_qualities(source_width: int, source_height: int, include_360p: bool) -> list[str]:
    """Choose downscaled renditions and retain a 4K-wide cinematic source rung."""
    width, height = int(source_width or 0), int(source_height or 0)
    if height <= 0:
        return []
    qualities = []
    for quality, target_height in (("2160p", 2160), ("1080p", 1080), ("720p", 720), ("480p", 480), ("360p", 360)):
        if quality == "360p" and not include_360p:
            continue
        # A 3840x1634 scope source is still native 4K by width. Keep its
        # 2160p ladder entry; the encoder preserves aspect ratio and does not upscale.
        if (width >= 3840 if quality == "2160p" else height >= target_height):
            qualities.append(quality)
    return qualities


def expected_output_size(
    duration_seconds: float,
    preset: Any,
    include_360p: bool,
    allowed_qualities: Iterable[str],
    *,
    four_k_copy: bool = False,
    source_file_bytes: int = 0,
    source_video_bitrate_bps: int = 0,
    source_audio_bitrate_bps: int = 0,
) -> Dict[str, Any]:
    """Return approximate per-rendition and total bytes; VBR output is variable."""
    duration = max(0.0, float(duration_seconds or 0))
    name = normalize_preset_name(preset)
    included = set(allowed_qualities)
    if not include_360p:
        included.discard("360p")
    breakdown: Dict[str, int] = {}
    for quality in included:
        if quality not in RATE_PROFILES[name]:
            continue
        rates = RATE_PROFILES[name][quality]
        if quality == "2160p" and four_k_copy:
            source_rate = int(source_video_bitrate_bps or 0) + int(source_audio_bitrate_bps or 0)
            amount = int(duration * source_rate / 8) if source_rate > 0 else int(source_file_bytes or 0)
        else:
            amount = int(duration * (rates["video_bitrate_bps"] + rates["audio_bitrate_bps"]) / 8)
        breakdown[quality] = max(0, amount)
    return {"estimated_output_bytes": sum(breakdown.values()), "per_rendition_bytes": breakdown, "is_estimate": True}
