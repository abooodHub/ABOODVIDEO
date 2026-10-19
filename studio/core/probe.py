"""Video inspection engine using ffprobe for ABOODVIDEO Studio."""
import os
import json
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional

from studio.core.process import run_headless
from studio.core.subtitle_metadata import normalize_subtitle


LADDER_SPECS = {
    "360p": {"width": 640, "height": 360, "video_bitrate_bps": 700000, "audio_bitrate_bps": 96000, "label": "360p (SD)"},
    "480p": {"width": 854, "height": 480, "video_bitrate_bps": 1200000, "audio_bitrate_bps": 128000, "label": "480p (SD)"},
    "720p": {"width": 1280, "height": 720, "video_bitrate_bps": 2500000, "audio_bitrate_bps": 128000, "label": "720p (HD)"},
    "1080p": {"width": 1920, "height": 1080, "video_bitrate_bps": 4500000, "audio_bitrate_bps": 160000, "label": "1080p (Full HD)"},
    "2160p": {"width": 3840, "height": 2160, "video_bitrate_bps": 16000000, "audio_bitrate_bps": 192000, "label": "4K (Ultra HD)"},
}

QUALITY_ORDER = ["360p", "480p", "720p", "1080p", "2160p"]


def format_duration(seconds: float) -> str:
    """Format duration in seconds to HH:MM:SS or MM:SS."""
    if seconds <= 0:
        return "00:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_bytes(num_bytes: int) -> str:
    """Format bytes into human readable string (KB, MB, GB)."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    elif num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    elif num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{num_bytes / (1024 * 1024 * 1024):.2f} GB"


def probe_video(video_path: Path) -> Dict[str, Any]:
    """
    Executes ffprobe to extract complete media, video, and audio metadata.
    Returns structured dictionary with technical specifications and quality ladder eligibility.
    """
    video_path = Path(video_path).resolve()
    if not video_path.exists():
        raise FileNotFoundError(f"ملف الفيديو غير موجود: {video_path}")

    from studio.core.ffmpeg_finder import get_ffprobe_path

    ffprobe_bin = get_ffprobe_path()
    if not ffprobe_bin:
        raise RuntimeError("أداة ffprobe غير متوفرة في النظام. يرجى التأكد من تثبيت FFmpeg وإضافته لـ PATH.")

    cmd = [
        ffprobe_bin,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(video_path),
    ]

    try:
        # ffprobe's JSON output is UTF-8.  Letting subprocess use Windows' active
        # ANSI code page corrupts non-ASCII stream tags (notably Arabic ASS titles)
        # before json.loads ever sees them.
        proc = run_headless(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError("استغرق فحص ffprobe وقتاً طويلاً وتجاوز المهلة المحددة.")

    if proc.returncode != 0:
        raise RuntimeError(f"فشل ffprobe في فحص الفيديو: {proc.stderr[:300]}")

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise ValueError("تعذر قراءة بيانات ffprobe JSON.")

    streams = data.get("streams", [])
    format_info = data.get("format", {})

    video_stream: Optional[Dict[str, Any]] = None
    audio_streams: List[Dict[str, Any]] = []
    subtitle_streams: List[Dict[str, Any]] = []

    for s in streams:
        c_type = s.get("codec_type")
        if c_type == "video" and not video_stream:
            video_stream = s
        elif c_type == "audio":
            audio_streams.append(s)
        elif c_type == "subtitle":
            subtitle_streams.append(s)

    if not video_stream:
        raise ValueError("ملف الوسائط لا يحتوي على مسار فيديو صالح.")

    width = int(video_stream.get("width", 0))
    height = int(video_stream.get("height", 0))
    if width <= 0 or height <= 0:
        raise ValueError("أبعاد الفيديو غير صالحة.")

    # Duration calculation
    duration = 0.0
    if "duration" in video_stream:
        try:
            duration = float(video_stream["duration"])
        except (ValueError, TypeError):
            pass
    if duration <= 0 and "duration" in format_info:
        try:
            duration = float(format_info["duration"])
        except (ValueError, TypeError):
            pass

    # Bitrate
    bitrate = 0
    if "bit_rate" in video_stream:
        try:
            bitrate = int(video_stream["bit_rate"])
        except (ValueError, TypeError):
            pass
    if bitrate <= 0 and "bit_rate" in format_info:
        try:
            bitrate = int(format_info["bit_rate"])
        except (ValueError, TypeError):
            pass

    # FPS calculation
    fps = 25.0
    r_frame_rate = video_stream.get("r_frame_rate", "25/1")
    try:
        if "/" in r_frame_rate:
            num, den = r_frame_rate.split("/")
            fps = float(num) / float(den) if float(den) != 0 else 25.0
        else:
            fps = float(r_frame_rate)
    except Exception:
        fps = 25.0

    fps_display = f"{fps:.3f}".rstrip("0").rstrip(".")

    # Size
    file_size = video_path.stat().st_size

    # Aspect ratio
    dar = video_stream.get("display_aspect_ratio", "")
    if not dar or dar == "0:1":
        # calculate approximate aspect ratio
        import math
        gcd = math.gcd(width, height) if height > 0 else 1
        ar_w = width // gcd
        ar_h = height // gcd
        if ar_w in (16, 8, 4) and ar_h in (9, 5, 3):
            dar = f"{ar_w}:{ar_h}"
        else:
            dar = f"{width / height:.2f}:1" if height > 0 else "16:9"

    # Audio details
    primary_audio = audio_streams[0] if audio_streams else None
    has_audio = primary_audio is not None
    audio_codec = primary_audio.get("codec_name", "") if primary_audio else None
    audio_channels = int(primary_audio.get("channels", 2)) if primary_audio else 0
    audio_channels_label = "Stereo" if audio_channels == 2 else ("5.1 Surround" if audio_channels == 6 else f"{audio_channels} Channels")
    audio_sample_rate = primary_audio.get("sample_rate", "48000") if primary_audio else "48000"
    try:
        audio_bitrate_bps = int(primary_audio.get("bit_rate") or 0) if primary_audio else 0
    except (TypeError, ValueError):
        audio_bitrate_bps = 0
    try:
        video_bitrate_bps = int(video_stream.get("bit_rate") or 0)
    except (TypeError, ValueError):
        video_bitrate_bps = 0
    if video_bitrate_bps <= 0:
        video_bitrate_bps = max(0, bitrate - audio_bitrate_bps)

    # Audio tracks summary
    parsed_audio_tracks = []
    for idx, a in enumerate(audio_streams):
        tags = a.get("tags", {})
        lang = tags.get("language", "und")
        title = tags.get("title", f"Track {idx + 1}")
        parsed_audio_tracks.append({
            "index": a.get("index", idx),
            "codec": a.get("codec_name", "aac"),
            "channels": int(a.get("channels", 2)),
            "language": lang,
            "title": title,
        })

    # Subtitle tracks summary
    parsed_subtitles = []
    for idx, s in enumerate(subtitle_streams):
        tags = s.get("tags", {})
        lang = tags.get("language", "und")
        title = tags.get("title", "")
        codec = s.get("codec_name", "unknown")
        disposition = s.get("disposition", {}) or {}
        normalized = normalize_subtitle(
            language=lang,
            fmt=codec,
            title=title,
            forced=disposition.get("forced", 0),
            default=disposition.get("default", 0),
        )
        parsed_subtitles.append({
            "index": s.get("index", idx),
            "codec": codec,
            "language": normalized["language_code"],
            **normalized,
        })

    # Quality Ladder Eligibility: DOWN-SCALE ONLY, NEVER UP-SCALE
    quality_options = []
    for q_key in QUALITY_ORDER:
        spec = LADDER_SPECS[q_key]
        # Allow only if rendition height <= source height or rendition width <= source width
        # Preserves aspect ratio for cinematic/cropped sources (e.g. 3840x1634 4K scope) without upscaling
        is_allowed = (spec["height"] <= height) or (spec["width"] <= width)
        # Auto-recommend all allowed
        quality_options.append({
            "key": q_key,
            "label": spec["label"],
            "width": spec["width"],
            "height": spec["height"],
            "video_bitrate_bps": spec["video_bitrate_bps"],
            "audio_bitrate_bps": spec["audio_bitrate_bps"],
            "allowed": is_allowed,
            "is_upscale": not is_allowed,
            "recommended": is_allowed,
        })

    # HDR detection using authoritative color metadata
    color_transfer = video_stream.get("color_transfer", "")
    color_primaries = video_stream.get("color_primaries", "")
    color_space = video_stream.get("color_space", "")

    # True HDR: requires HDR transfer function
    _hdr_transfers = {"smpte2084", "arib-std-b67", "smpte428", "bt2020-10", "bt2020-12"}
    is_hdr = color_transfer.lower() in _hdr_transfers
    if not is_hdr:
        # Also check for common HDR container variants
        is_hdr = any(t in color_transfer.lower() for t in ["pq", "hlg", "hdr"])

    # HLG variant detection
    is_hlg = "arib" in color_transfer.lower() or "arib-std-b67" in color_transfer.lower()

    # SDR 10-bit: has 10-bit depth but no HDR transfer
    pix_fmt = video_stream.get("pix_fmt", "yuv420p")
    is_10bit = any(x in pix_fmt for x in ["p10", "p12", "10le", "12le"])

    if is_hdr:
        hdr_label = "HLG HDR" if is_hlg else "HDR (PQ/HDR10)"
    elif is_10bit:
        hdr_label = "10-bit SDR"
    else:
        hdr_label = "SDR"

    return {
        "file_path": str(video_path),
        "file_name": video_path.name,
        "file_size": file_size,
        "file_size_formatted": format_bytes(file_size),
        "duration_seconds": round(duration, 2),
        "duration_formatted": format_duration(duration),
        "width": width,
        "height": height,
        "resolution": f"{width}x{height}",
        "aspect_ratio": dar,
        "video_codec": video_stream.get("codec_name", "unknown"),
        "video_profile": video_stream.get("profile", ""),
        "video_level": int(video_stream.get("level") or 0),
        "video_bitrate_bps": video_bitrate_bps,
        "pixel_format": video_stream.get("pix_fmt", "yuv420p"),
        "color_transfer": color_transfer,
        "color_primaries": color_primaries,
        "color_space": color_space,
        "is_hdr": is_hdr,
        "is_10bit": is_10bit,
        "hdr_label": hdr_label,
        "fps": round(fps, 3),
        "fps_display": fps_display,
        "bitrate_bps": bitrate,
        "bitrate_formatted": f"{bitrate // 1000} kbps" if bitrate > 0 else "غير محدد",
        "has_audio": has_audio,
        "audio_codec": audio_codec,
        "audio_profile": primary_audio.get("profile", "") if primary_audio else "",
        "audio_bitrate_bps": audio_bitrate_bps,
        "audio_channels": audio_channels,
        "audio_channels_label": audio_channels_label,
        "audio_sample_rate": audio_sample_rate,
        "audio_tracks": parsed_audio_tracks,
        "subtitle_tracks": parsed_subtitles,
        "qualities": quality_options,
    }
