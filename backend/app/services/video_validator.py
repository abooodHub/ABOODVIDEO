"""Video Validation Service utilizing ffprobe exclusively (Phase 2)."""
import json
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional
from app.core.logging import logger
from app.core.exceptions import AboodVideoException


class InvalidVideoException(AboodVideoException):
    """Raised when an uploaded file is not a valid video or fails ffprobe inspection."""
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message=message, status_code=422, details=details)


def validate_video_file(file_path: Path) -> Dict[str, Any]:
    """
    Execute ffprobe to strictly inspect and validate the video file.
    Does NOT use shell=True. Rejects audio-only, text, images, or corrupted streams.
    """
    if not file_path.exists():
        raise InvalidVideoException("Uploaded file does not exist on disk")

    if file_path.stat().st_size == 0:
        raise InvalidVideoException("Uploaded file is empty (0 bytes)")

    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(file_path.resolve()),
    ]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,  # 30s timeout for probing
            check=False,
        )
    except FileNotFoundError:
        logger.error("ffprobe is not installed on the system path")
        raise InvalidVideoException("Server error: ffprobe is unavailable for validation")
    except subprocess.TimeoutExpired:
        raise InvalidVideoException("ffprobe inspection timed out: file may be corrupted")

    if proc.returncode != 0:
        raise InvalidVideoException(
            "ffprobe failed to parse file: invalid or corrupted media container",
            details={"ffprobe_stderr": proc.stderr[:500]},
        )

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise InvalidVideoException("Failed to parse ffprobe inspection output")

    streams = data.get("streams", [])
    format_info = data.get("format", {})

    # 1. Identify at least one video stream
    video_stream = None
    for s in streams:
        if s.get("codec_type") == "video":
            video_stream = s
            break

    if not video_stream:
        raise InvalidVideoException(
            "Validation rejected: No valid video stream found in media container"
        )

    # 2. Extract dimensions
    width = video_stream.get("width")
    height = video_stream.get("height")
    if not width or not height or width <= 0 or height <= 0:
        raise InvalidVideoException("Invalid video stream dimensions")

    # 3. Extract duration
    duration = None
    if "duration" in video_stream:
        try:
            duration = float(video_stream["duration"])
        except (ValueError, TypeError):
            pass

    if not duration and "duration" in format_info:
        try:
            duration = float(format_info["duration"])
        except (ValueError, TypeError):
            pass

    if duration is None or duration <= 0:
        raise InvalidVideoException("Invalid or missing video duration")

    # 4. Extract bitrate and codec
    codec_name = video_stream.get("codec_name", "unknown")
    bitrate = None
    if "bit_rate" in format_info:
        try:
            bitrate = int(format_info["bit_rate"])
        except (ValueError, TypeError):
            pass

    return {
        "valid": True,
        "duration_seconds": round(duration, 3),
        "resolution_width": int(width),
        "resolution_height": int(height),
        "bitrate": bitrate,
        "codec": codec_name,
        "format_name": format_info.get("format_name", "unknown"),
        "size_bytes": file_path.stat().st_size,
    }
