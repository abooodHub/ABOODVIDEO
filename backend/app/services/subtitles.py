"""Subtitle processing, conversion, and storage management service."""
import os
import re
import uuid
import shutil
from pathlib import Path
from typing import Optional, Tuple
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("subtitles")

SUPPORTED_SUBTITLE_FORMATS = {"srt", "vtt", "ass", "ssa"}
PLAYABLE_SUBTITLE_FORMATS = SUPPORTED_SUBTITLE_FORMATS


class SubtitleError(Exception):
    def __init__(self, message: str, error_code: str = "SUBTITLE_ERROR"):
        super().__init__(message)
        self.error_code = error_code


def decode_subtitle_bytes(raw_bytes: bytes) -> str:
    """Robustly decode subtitle file bytes supporting UTF-8, UTF-8-BOM, CP-1256 (Arabic), and ISO-8859-6."""
    encodings = ["utf-8-sig", "utf-8", "cp1256", "iso-8859-6", "latin1"]
    for enc in encodings:
        try:
            return raw_bytes.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def convert_srt_to_webvtt(srt_text: str | bytes) -> str:
    """Convert SRT text content (or bytes) to valid WebVTT format."""
    if isinstance(srt_text, bytes):
        srt_text = decode_subtitle_bytes(srt_text)

    # Normalize line breaks
    text = srt_text.replace("\r\n", "\n").replace("\r", "\n").strip()

    # Pattern to match SRT timestamp lines: 00:00:20,000 --> 00:00:24,400
    # In WebVTT, millisecond separator is a period '.' instead of comma ','
    timecode_pattern = re.compile(
        r"(\d{2}:\d{2}:\d{2}),(\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2}),(\d{3})"
    )

    converted_lines = []
    converted_lines.append("WEBVTT\n")

    for line in text.split("\n"):
        match = timecode_pattern.search(line)
        if match:
            # Replace comma with period in timestamp line
            new_line = timecode_pattern.sub(r"\1.\2 --> \3.\4", line)
            converted_lines.append(new_line)
        else:
            converted_lines.append(line)

    return "\n".join(converted_lines) + "\n"


def validate_webvtt_content(vtt_text: str | bytes) -> str:
    """Validate that text is valid WebVTT and normalize headers."""
    if isinstance(vtt_text, bytes):
        vtt_text = decode_subtitle_bytes(vtt_text)
    normalized = vtt_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized.startswith("WEBVTT"):
        normalized = "WEBVTT\n\n" + normalized
    return normalized + "\n"


class SubtitleService:
    decode_subtitle_bytes = staticmethod(decode_subtitle_bytes)
    convert_srt_to_webvtt = staticmethod(convert_srt_to_webvtt)
    validate_webvtt_content = staticmethod(validate_webvtt_content)

    @staticmethod
    def get_subtitles_dir(video_id: uuid.UUID) -> Path:
        """Get canonical directory for video subtitles."""
        p = Path(settings.STORAGE_VIDEOS_PATH) / str(video_id) / "subtitles"
        p.mkdir(parents=True, exist_ok=True)
        return p.resolve()

    @staticmethod
    def store_subtitle(
        video_id: uuid.UUID,
        subtitle_id: uuid.UUID,
        filename: str,
        content_bytes: bytes,
    ) -> Tuple[str, Optional[str], str, bool]:
        """Store subtitle file with canonical containment and convert SRT to WebVTT.

        Returns: (source_path_rel, webvtt_path_rel, detected_format, is_playable)
        """
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in SUPPORTED_SUBTITLE_FORMATS:
            raise SubtitleError(
                f"Unsupported subtitle format '{ext}'. Allowed: {', '.join(SUPPORTED_SUBTITLE_FORMATS)}",
                error_code="UNSUPPORTED_FORMAT",
            )

        if len(content_bytes) > 10 * 1024 * 1024:  # 10MB limit
            raise SubtitleError("Subtitle file exceeds 10MB limit", error_code="FILE_TOO_LARGE")

        subs_dir = SubtitleService.get_subtitles_dir(video_id)

        # 1. Save original source file
        source_filename = f"{subtitle_id}_source.{ext}"
        source_file = (subs_dir / source_filename).resolve()
        if not source_file.is_relative_to(subs_dir):
            raise SubtitleError("Path traversal rejected", error_code="PATH_TRAVERSAL")

        source_file.write_bytes(content_bytes)
        source_path_rel = f"videos/{video_id}/subtitles/{source_filename}"

        # 2. Process for playback if playable format
        webvtt_path_rel = None
        is_playable = ext in PLAYABLE_SUBTITLE_FORMATS

        if ext in ("srt", "vtt"):
            text_content = decode_subtitle_bytes(content_bytes)
            if ext == "srt":
                vtt_content = convert_srt_to_webvtt(text_content)
            else:  # vtt
                vtt_content = validate_webvtt_content(text_content)

            vtt_filename = f"{subtitle_id}.vtt"
            vtt_file = (subs_dir / vtt_filename).resolve()
            if not vtt_file.is_relative_to(subs_dir):
                raise SubtitleError("Path traversal rejected", error_code="PATH_TRAVERSAL")

            vtt_file.write_text(vtt_content, encoding="utf-8")
            webvtt_path_rel = f"videos/{video_id}/subtitles/{vtt_filename}"

        return source_path_rel, webvtt_path_rel, ext, is_playable

    @staticmethod
    def generate_hls_subtitle_playlist(duration_seconds: float, vtt_filename: str) -> str:
        """Generate a valid HLS WebVTT subtitle playlist."""
        dur = max(duration_seconds, 1.0)
        return (
            "#EXTM3U\n"
            "#EXT-X-VERSION:6\n"
            f"#EXT-X-TARGETDURATION:{int(dur) + 1}\n"
            "#EXT-X-MEDIA-SEQUENCE:0\n"
            "#EXT-X-PLAYLIST-TYPE:VOD\n"
            f"#EXTINF:{dur:.6f},\n"
            f"{vtt_filename}\n"
            "#EXT-X-ENDLIST\n"
        )

    @staticmethod
    def delete_subtitle_files(video_id: uuid.UUID, subtitle_id: uuid.UUID) -> None:
        """Safely remove subtitle files belonging to a specific subtitle record."""
        subs_dir = SubtitleService.get_subtitles_dir(video_id)
        for pattern in (f"{subtitle_id}.*", f"{subtitle_id}_source.*"):
            for f in subs_dir.glob(pattern):
                resolved = f.resolve()
                if resolved.is_relative_to(subs_dir) and resolved.is_file():
                    try:
                        resolved.unlink()
                    except Exception as e:
                        logger.warning(f"Could not delete subtitle file {resolved}: {e}")
