"""Subtitle processing, True ASS softsub preservation, WebVTT conversion, and filter builder."""
import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

from studio.config import FONTS_DIR
from studio.core.process import run_headless


def escape_ffmpeg_filter_path(p: Path) -> str:
    r"""
    Escapes a filesystem path for FFmpeg filtergraph syntax on Windows:
    1. Replaces backslashes with forward slashes
    2. Escapes colons with \:
    3. Escapes single quotes with \'
    """
    path_str = str(p.resolve()).replace("\\", "/")
    path_str = path_str.replace(":", r"\:")
    path_str = path_str.replace("'", r"\'")
    return path_str


def compute_file_sha256(file_path: Path) -> str:
    """Compute standard SHA-256 hash of a file."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(1048576):
            hasher.update(chunk)
    return hasher.hexdigest().lower()


def convert_srt_to_webvtt(srt_path: Path, output_vtt_path: Path) -> Path:
    """Converts an SRT file to clean WebVTT format."""
    output_vtt_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError("ffmpeg غير متوفر للتحويل إلى WebVTT")

    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-i", str(srt_path.resolve()),
        "-c:s", "webvtt",
        str(output_vtt_path.resolve()),
    ]
    res = run_headless(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"فشل تحويل SRT إلى WebVTT: {res.stderr[:300]}")
    return output_vtt_path


# Backwards compatibility alias
convert_to_webvtt = convert_srt_to_webvtt


def process_softsub_file(
    subtitle_input_path: Path,
    target_subs_dir: Path,
    language_code: str = "ar",
) -> Dict[str, Any]:
    """
    Processes subtitle according to True Softsub rules:
    - ASS/SSA: Preserves original file untouched in target_subs_dir (no conversion to WebVTT).
               Preserves all styles, positioning, override tags, vectors, signs, and karaoke.
    - SRT: Preserves original .srt AND generates a .vtt playback derivative.
    - VTT: Uses directly as .vtt.

    Returns:
      {
         "primary_path": relative path to active subtitle,
         "format": "ass" | "ssa" | "srt" | "vtt",
         "sha256": primary file sha256,
         "size_bytes": primary file size,
         "auxiliary_files": list of dicts for secondary files (e.g. original .srt if .vtt is primary)
      }
    """
    subtitle_input_path = Path(subtitle_input_path).resolve()
    target_subs_dir = Path(target_subs_dir).resolve()
    target_subs_dir.mkdir(parents=True, exist_ok=True)

    ext = subtitle_input_path.suffix.lower().lstrip(".")

    if ext in ("ass", "ssa"):
        # True ASS/SSA: Preserve 100% original untouched
        dest_filename = f"{language_code}.{ext}"
        dest_path = target_subs_dir / dest_filename
        shutil.copy2(subtitle_input_path, dest_path)

        sha = compute_file_sha256(dest_path)
        return {
            "primary_rel_path": f"subtitles/{dest_filename}",
            "primary_abs_path": dest_path,
            "format": ext,
            "sha256": sha,
            "size_bytes": dest_path.stat().st_size,
            "auxiliary_files": [],
        }

    elif ext == "srt":
        # SRT: Preserve original .srt AND generate .vtt playback derivative
        orig_filename = f"{language_code}_original.srt"
        orig_path = target_subs_dir / orig_filename
        shutil.copy2(subtitle_input_path, orig_path)

        vtt_filename = f"{language_code}.vtt"
        vtt_path = target_subs_dir / vtt_filename
        convert_srt_to_webvtt(orig_path, vtt_path)

        vtt_sha = compute_file_sha256(vtt_path)
        orig_sha = compute_file_sha256(orig_path)

        return {
            "primary_rel_path": f"subtitles/{vtt_filename}",
            "primary_abs_path": vtt_path,
            "format": "vtt",
            "sha256": vtt_sha,
            "size_bytes": vtt_path.stat().st_size,
            "auxiliary_files": [
                {
                    "path": f"subtitles/{orig_filename}",
                    "abs_path": orig_path,
                    "sha256": orig_sha,
                    "size_bytes": orig_path.stat().st_size,
                    "media_type": "application/x-subrip",
                }
            ],
        }

    else:
        # VTT: Use directly
        vtt_filename = f"{language_code}.vtt"
        dest_path = target_subs_dir / vtt_filename
        shutil.copy2(subtitle_input_path, dest_path)
        sha = compute_file_sha256(dest_path)
        return {
            "primary_rel_path": f"subtitles/{vtt_filename}",
            "primary_abs_path": dest_path,
            "format": "vtt",
            "sha256": sha,
            "size_bytes": dest_path.stat().st_size,
            "auxiliary_files": [],
        }


def build_hardsub_filter(
    subtitle_path: Path,
    fonts_dir: Optional[Path] = None,
    hardsub_style: Optional[dict] = None,
) -> str:
    """
    Constructs the FFmpeg video filter snippet for burning subtitles into video.

    hardsub_style dict (all optional, used only in custom mode):
        source: "original" | "custom"  (default: "original")
        font: str    - font family name  (default: use ASS original)
        size: int    - font size in points (default: use ASS original)
        color: str   - #RRGGBB hex text color
        border_color: str - #RRGGBB hex border/outline color
        border_size: float - outline width (0.0–8.0)
        position: "bottom" | "top" | "center"
    """
    sub_path_esc = escape_ffmpeg_filter_path(subtitle_path)
    target_fonts_dir = fonts_dir or FONTS_DIR

    ext = subtitle_path.suffix.lower()
    style = hardsub_style or {}
    use_custom = style.get("source", "original") == "custom"

    # Base filter options
    opts = f"subtitles='{sub_path_esc}'"

    # Add fontsdir for ASS files
    if ext in (".ass", ".ssa") and target_fonts_dir and target_fonts_dir.exists():
        fonts_dir_esc = escape_ffmpeg_filter_path(target_fonts_dir)
        opts += f":fontsdir='{fonts_dir_esc}'"

    if use_custom and ext in (".ass", ".ssa"):
        # Build libass override style via force_style parameter
        # force_style applies ASS override tags to all events
        style_parts = []

        font = style.get("font", "").strip()
        if font:
            style_parts.append(f"Fontname={font}")

        try:
            size = int(style.get("size", 0))
            if size > 0:
                style_parts.append(f"Fontsize={size}")
        except (ValueError, TypeError):
            pass

        # Convert #RRGGBB → &H00BBGGRR (ASS BGR hex format)
        def hex_to_ass_color(hexstr: str) -> str:
            hexstr = hexstr.strip().lstrip("#")
            if len(hexstr) == 6:
                r, g, b = hexstr[0:2], hexstr[2:4], hexstr[4:6]
                return f"&H00{b}{g}{r}"
            return ""

        color = style.get("color", "").strip()
        if color:
            ass_color = hex_to_ass_color(color)
            if ass_color:
                style_parts.append(f"PrimaryColour={ass_color}")

        border_color = style.get("border_color", "").strip()
        if border_color:
            ass_bc = hex_to_ass_color(border_color)
            if ass_bc:
                style_parts.append(f"OutlineColour={ass_bc}")

        try:
            border_size = float(style.get("border_size", -1))
            if border_size >= 0:
                style_parts.append(f"Outline={border_size}")
        except (ValueError, TypeError):
            pass

        # Position: bottom=2 (default), top=8, center=5 (numpad layout)
        pos_map = {"bottom": 2, "top": 8, "center": 5}
        position = style.get("position", "")
        alignment = pos_map.get(position)
        if alignment:
            style_parts.append(f"Alignment={alignment}")

        if style_parts:
            force_style_val = ",".join(style_parts)
            opts += f":force_style='{force_style_val}'"

    return opts


# Codec name → file extension for embedded stream extraction
_CODEC_TO_EXT: dict = {
    "ass": ".ass",
    "ssa": ".ass",
    "subrip": ".srt",
    "srt": ".srt",
    "webvtt": ".vtt",
    "mov_text": ".srt",   # extract as SRT for compatibility
    "hdmv_pgs_subtitle": ".srt",  # PGS — ffmpeg extracts as text
    "dvd_subtitle": ".srt",
}


def extract_embedded_subtitle(
    video_path: Path,
    stream_index: int,
    output_dir: Path,
    language_code: str = "ar",
) -> Tuple[Path, str]:
    """
    Extracts an embedded subtitle stream from a video file to a local file.

    Args:
        video_path: Source MKV/MP4 file.
        stream_index: Absolute ffprobe stream index of the subtitle track.
        output_dir: Directory where the extracted subtitle will be written.
        language_code: Used for output filename (e.g. "ar" → ar.ass).

    Returns:
        Tuple of (extracted_path: Path, format_ext: str)
        where format_ext is e.g. "ass", "srt", "vtt".

    Raises:
        RuntimeError on ffmpeg failure.
    """
    import json
    from studio.core.ffmpeg_finder import get_ffmpeg_path, get_ffprobe_path

    video_path = Path(video_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    ffprobe_bin = get_ffprobe_path()
    if not ffprobe_bin:
        raise RuntimeError("ffprobe غير متوفر.")

    # Identify codec for this stream
    probe_cmd = [
        ffprobe_bin,
        "-hide_banner",
        "-v", "error",
        "-select_streams", f"s",
        "-show_entries", "stream=index,codec_name",
        "-of", "json",
        str(video_path),
    ]
    probe_res = run_headless(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    codec_name = "subrip"
    try:
        sdata = json.loads(probe_res.stdout)
        for s in sdata.get("streams", []):
            if s.get("index") == stream_index:
                codec_name = s.get("codec_name", "subrip")
                break
    except Exception:
        pass

    ext = _CODEC_TO_EXT.get(codec_name, ".srt")
    out_filename = f"{language_code}_embedded{ext}"
    out_path = output_dir / out_filename

    ffmpeg_bin = get_ffmpeg_path()
    if not ffmpeg_bin:
        raise RuntimeError("ffmpeg غير متوفر.")

    # Map by absolute stream index using -map 0:N
    if ext == ".ass":
        codec_arg = "copy"  # Preserve ASS binary-compatible copy
    elif ext == ".srt":
        codec_arg = "srt"
    elif ext == ".vtt":
        codec_arg = "webvtt"
    else:
        codec_arg = "srt"

    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-i", str(video_path),
        "-map", f"0:{stream_index}",
        "-c:s", codec_arg,
        str(out_path),
    ]
    res = run_headless(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=120)
    if res.returncode != 0 or not out_path.exists():
        raise RuntimeError(f"فشل استخراج الترجمة المضمنة (stream {stream_index}): {res.stderr[:400]}")

    return out_path, ext.lstrip(".")
