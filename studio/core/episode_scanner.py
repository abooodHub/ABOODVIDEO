"""Episode Scanner, Deterministic Subtitle & Font Matcher for ABOODVIDEO Studio Season Batches.

Scans local season directories, extracts video files (.mkv, .mp4, .mov, .ts, .webm, .avi),
determines natural sorting, matches external subtitles (.ass, .ssa, .srt, .vtt) deterministically,
detects embedded subtitle tracks, audits ASS fonts against Windows/Studio/Season/MKV sources,
and marks episodes as NEEDS_REVIEW if ambiguous or missing fonts.
Never uses an LLM.
Never creates Series/Season entities automatically.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Union

from studio.core.batch_store import EpisodeState
from studio.core.fonts import (
    analyze_ass_fonts,
    extract_mkv_font_attachments,
)
from studio.core.process import run_headless
from studio.core.probe import probe_video
from studio.core.probe import LADDER_SPECS
from studio.config import TEMP_DIR
from studio.core.subtitle_metadata import normalize_subtitle


def get_season_working_fonts_dir(folder_path: Union[str, Path]) -> Path:
    """Return the season-scoped managed copy area for exact subtitle font reuse."""
    canonical = str(Path(folder_path).resolve()).casefold().encode("utf-8")
    return TEMP_DIR / f"season_fonts_{hashlib.sha256(canonical).hexdigest()[:12]}"


def _extract_embedded_ass(video_path: Path, stream_index: int, output_path: Path) -> Optional[Path]:
    from studio.core.ffmpeg_finder import get_ffmpeg_path
    ffmpeg_bin = get_ffmpeg_path()
    if not ffmpeg_bin:
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video_path),
           "-map", f"0:{stream_index}", "-c:s", "ass", str(output_path)]
    try:
        result = run_headless(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=45, check=False)
        return output_path if result.returncode == 0 and output_path.exists() else None
    except Exception:
        return None


def _prepare_season_font_reuse(video_files: List[Path], all_subtitles: List[Path], season_dir: Path) -> Path:
    """Add verified font binaries to a pool; ASS audits match their actual family names."""
    import shutil as _shutil
    working = get_season_working_fonts_dir(season_dir)
    working.mkdir(parents=True, exist_ok=True)
    source_fonts = find_season_fonts_dir(season_dir)
    if source_fonts:
        for font_path in source_fonts.iterdir():
            if font_path.is_file() and font_path.suffix.lower() in (".ttf", ".otf", ".ttc"):
                destination = working / f"font-{hashlib.sha256(font_path.read_bytes()).hexdigest()[:16]}{font_path.suffix.lower()}"
                if not destination.exists():
                    _shutil.copy2(font_path, destination)

    provenance: Dict[str, Dict[str, Any]] = {}
    provenance_file = working / ".font_pool_provenance.json"
    if provenance_file.is_file():
        try:
            provenance = {item["sha256"]: item for item in json.loads(provenance_file.read_text(encoding="utf-8")).get("fonts", [])}
        except (OSError, ValueError, KeyError, TypeError):
            pass
    for video in video_files:
        identity = hashlib.sha256(str(video.resolve()).encode("utf-8")).hexdigest()[:16]
        per_video = working / ".attachments" / identity
        records = extract_mkv_font_attachments(video, output_dir=per_video) if video.suffix.lower() == ".mkv" else []
        for record in records:
            try:
                attached_path = Path(record["saved_path"])
                sha = hashlib.sha256(attached_path.read_bytes()).hexdigest()
                # A filename is insufficient evidence that an attachment is a valid font.
                if not record.get("names"):
                    continue
                target = working / f"font-{sha[:16]}{attached_path.suffix.lower()}"
                if not target.exists():
                    _shutil.copy2(attached_path, target)
                provenance[sha] = {
                    "sha256": sha,
                    "source_episode": video.name,
                    "source_path": str(video.resolve()),
                    "families": list(record.get("names", [])),
                }
            except (KeyError, OSError):
                continue

    provenance_file.write_text(
        json.dumps({"fonts": list(provenance.values())}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return working

logger = logging.getLogger("aboodvideo.studio.scanner")

SUPPORTED_VIDEO_EXTENSIONS: Set[str] = {
    ".mkv",
    ".mp4",
    ".mov",
    ".ts",
    ".webm",
    ".avi",
}

SUPPORTED_SUBTITLE_EXTENSIONS: Set[str] = {
    ".ass",
    ".ssa",
    ".srt",
    ".vtt",
}


def natural_sort_key(s: str) -> List[tuple[int, Union[Decimal, str]]]:
    """Generates a natural sort key splitting numeric and non-numeric chunks.
    Uses (0, Decimal(val)) for numbers and (1, str(val)) for text so Python's
    comparison is always type-safe and never compares str with int.
    Correctly orders:
      1.mkv, 2.mkv, 10.mkv -> 1, 2, 10
      Episode 01.mkv, Episode 2.mkv, Episode 10.mkv
      S01E01.mkv, S01E02.mkv, S01E10.mkv
    """
    parts: List[tuple[int, Union[int, str]]] = []
    # Keep decimal episode tokens intact: splitting "6.5" into 6, ".", 5
    # sorts it after episode 6.9 and can put it before plain episode 6 files.
    for chunk in re.split(r"(\d+(?:\.\d+)?)", s):
        if not chunk:
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?", chunk):
            parts.append((0, Decimal(chunk)))
        else:
            parts.append((1, chunk.lower()))
    return parts


def parse_episode_number(filename: str) -> Optional[Union[int, float, str]]:
    """Extracts likely episode number from filename for display and matching.
    Supports S01E03, Episode 02, Ep 10, 01.mkv, etc.
    Does NOT create any Series or Season entities.
    """
    stem = Path(filename).stem
    # Release groups, checksums and technical tags are never episode numbers.
    cleaned = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", stem)

    special = re.search(r"(?<![A-Z0-9])(OVA|SP)[\s._-]*(\d+(?:\.\d+)?)(?!\d)", stem, re.IGNORECASE)
    if special:
        value = _number_value(special.group(2))
        suffix = str(int(value)) if float(value).is_integer() else format(value, "g")
        return f"{special.group(1).upper()}{suffix}"

    # Pattern 1: S01E03 / s1e3
    number = r"(\d+(?:\.\d+)?)"
    m = re.search(r"s\d+[\s._-]*e" + number, stem, re.IGNORECASE)
    if m:
        return _number_value(m.group(1))

    # Pattern 2: Episode 02 / Ep.05 / Ep_12 / E08
    m = re.search(r"(?<![a-z0-9])(?:episode|ep|e|الحلقة)[\s._-]*" + number + r"(?!\d|[a-z])", cleaned, re.IGNORECASE)
    if m:
        return _number_value(m.group(1))

    # Pattern 3: Leading numbers: '01.mkv', '02 - Title.mkv'
    m = re.search(r"^\s*" + number + r"(?=$|[\s._-])", cleaned)
    if m:
        value = _number_value(m.group(1))
        if value not in (360, 480, 576, 720, 1080, 1440, 2160, 264, 265) and not 1900 <= value <= 2099:
            return value

    # Pattern 4: Trailing numbers: 'Title_01', 'Show - 24'
    candidates = re.findall(r"(?:^|[\s._-])(\d+(?:\.\d+)?)(?=$|[\s._-])", cleaned)
    values = [_number_value(value) for value in candidates]
    values = [value for value in values if value not in (360, 480, 576, 720, 1080, 1440, 2160, 264, 265)
              and not 1900 <= value <= 2099 and value < 10000]
    if len(values) == 1:
        return values[0]

    return None


def _number_value(value: str) -> Union[int, float]:
    parsed = Decimal(value)
    return int(parsed) if parsed == parsed.to_integral_value() else float(parsed)


def _episode_label(number: Optional[Union[int, float, str]], fallback: int) -> str:
    if number is None:
        return f"{fallback:02d}"
    if isinstance(number, str):
        return number
    if float(number).is_integer():
        return f"{int(number):02d}"
    whole, fraction = format(number, "g").split(".", 1)
    return f"{int(whole):02d}.{fraction}"


def find_season_subtitles(folder_path: Path) -> List[Path]:
    """Finds all subtitle files in season directory and direct subdirectories like subs/ or subtitles/."""
    subs: List[Path] = []
    if not folder_path.exists() or not folder_path.is_dir():
        return subs

    import os
    for current, dirs, names in os.walk(folder_path, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
        subs.extend(Path(current) / name for name in names
                    if Path(name).suffix.lower() in SUPPORTED_SUBTITLE_EXTENSIONS
                    and not (Path(current) / name).is_symlink())
    return subs


def find_season_fonts_dir(folder_path: Path) -> Optional[Path]:
    """Checks for a fonts/ subdirectory within the season folder."""
    fonts_dir = folder_path / "fonts"
    if fonts_dir.exists() and fonts_dir.is_dir():
        return fonts_dir
    return None


def probe_embedded_subtitles(video_path: Path) -> List[Dict[str, Any]]:
    """Probes video file for embedded subtitle streams using ffprobe."""
    from studio.core.ffmpeg_finder import get_ffprobe_path
    ffprobe_bin = get_ffprobe_path()
    if not ffprobe_bin:
        return []

    cmd = [
        ffprobe_bin,
        "-v", "error",
        "-select_streams", "s",
        "-show_entries", "stream=index,codec_name:stream_tags=language,title:stream_disposition=forced,default",
        "-of", "json",
        str(video_path),
    ]
    try:
        proc = run_headless(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
        if proc.returncode != 0:
            return []
        data = json.loads(proc.stdout)
        results = []
        for s in data.get("streams", []):
            tags = s.get("tags", {})
            disposition = s.get("disposition", {}) or {}
            normalized = normalize_subtitle(
                language=tags.get("language", "und"),
                fmt=s.get("codec_name", "unknown"),
                title=tags.get("title", ""),
                forced=disposition.get("forced", 0),
                default=disposition.get("default", 0),
            )
            results.append({
                "index": s.get("index"),
                "codec": s.get("codec_name", "unknown").lower(),
                "language": normalized["language_code"],
                **normalized,
            })
        return results
    except Exception:
        return []


def match_subtitles_for_video(video_path: Path, all_subtitles: List[Path]) -> List[Path]:
    """Finds candidate external subtitle files for a video using deterministic matching rules:
    
    1. Exact stem match: '01.mkv' <-> '01.ass'
    2. Language-tagged stem match: '01.mkv' <-> '01.ar.ass', '01.en.ass'
    3. Episode number match: 'Episode 01.mkv' <-> 'Episode 01.ass', '01.mkv' <-> 'Episode 01.ass'
    
    Never uses an LLM.
    Returns list of candidate subtitle paths.
    """
    v_stem = video_path.stem.lower()
    v_ep_num = parse_episode_number(video_path.name)

    candidates: List[Path] = []
    seen_paths = set()

    for sub in all_subtitles:
        video_season = re.search(r"s(\d+)[\s._-]*e\d", video_path.stem, re.I)
        sub_season = re.search(r"s(\d+)[\s._-]*e\d", sub.stem, re.I)
        if video_season and sub_season and int(video_season.group(1)) != int(sub_season.group(1)):
            continue
        sub_stem = sub.stem.lower()
        sub_path_str = str(sub.resolve())

        # Exact stem match
        if sub_stem == v_stem:
            if sub_path_str not in seen_paths:
                candidates.append(sub)
                seen_paths.add(sub_path_str)
            continue

        # Language tagged stem match: stem starts with "video_stem." e.g. "01.ar" for "01"
        if sub_stem.startswith(v_stem + "."):
            if sub_path_str not in seen_paths:
                candidates.append(sub)
                seen_paths.add(sub_path_str)
            continue

        # Episode number match: both have identical parsed episode number
        if v_ep_num is not None:
            sub_ep_num = parse_episode_number(sub.name)
            if sub_ep_num is not None and sub_ep_num == v_ep_num:
                if sub_path_str not in seen_paths:
                    candidates.append(sub)
                    seen_paths.add(sub_path_str)

    return candidates


def audit_subtitle_and_fonts(
    video_path: Path,
    all_subtitles: List[Path],
    season_fonts_dir: Optional[Path] = None,
    embedded_subs: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Performs full deterministic subtitle matching and ASS font audit for a single video.
    
    Returns:
        Dict containing subtitle_source, subtitle_path, subtitle_format, subtitle_stream_index,
        subtitle_label, font_status, missing_fonts, subtitle_candidates, initial_state, and error.
    """
    candidate_subs = match_subtitles_for_video(video_path, all_subtitles)
    if embedded_subs is None:
        embedded_subs = probe_embedded_subtitles(video_path)

    # Build candidates list for user inspection / switching
    candidates_data: List[Dict[str, Any]] = []
    for sub in candidate_subs:
        fmt = sub.suffix.lower().lstrip(".")
        normalized = normalize_subtitle(fmt=fmt, path=sub)
        candidates_data.append({
            "source": "external",
            "path": str(sub.resolve()),
            "filename": sub.name,
            "format": fmt,
            "stream_index": None,
            **normalized,
        })

    for s in embedded_subs:
        candidates_data.append({
            "source": "embedded",
            "path": "",
            "filename": "",
            "format": s["codec"],
            "stream_index": s["index"],
            **{k: v for k, v in s.items() if k in ("language_code", "language_name", "title", "forced", "default", "label")},
        })

    candidates_data.append({
        "source": "none",
        "path": "",
        "filename": "",
        "format": "",
        "stream_index": None,
        "label": "بدون ترجمة",
        "language_code": "und",
        "language_name": "غير معروف",
        "title": "",
        "forced": False,
        "default": False,
    })

    # Default outcomes
    subtitle_source = "none"
    subtitle_path = ""
    subtitle_format = ""
    subtitle_stream_index = None
    subtitle_label = "بدون ترجمة"
    font_status = "—"
    missing_fonts: List[str] = []
    font_audit_result: Dict[str, Any] = {}
    state = EpisodeState.PENDING.value
    error = None

    # Decision logic
    if len(candidate_subs) > 1:
        # Ambiguous external subtitle match! Mark NEEDS_REVIEW, do NOT guess
        subtitle_source = "needs_review"
        subtitle_label = "تحتاج مراجعة ⚠️"
        font_status = "تحتاج مراجعة"
        state = EpisodeState.NEEDS_REVIEW.value
        cand_names = [s.name for s in candidate_subs]
        error = f"ملفات ترجمة متعددة متطابقة للحلقة ({len(candidate_subs)}): {', '.join(cand_names)}"

    elif len(candidate_subs) == 1:
        # Deterministic single external match
        matched_sub = candidate_subs[0]
        fmt = matched_sub.suffix.lower().lstrip(".")
        subtitle_source = "external"
        subtitle_path = str(matched_sub.resolve())
        subtitle_format = fmt
        subtitle_metadata = normalize_subtitle(fmt=fmt, path=matched_sub, default=True)
        subtitle_label = subtitle_metadata["label"]

        if fmt in ("ass", "ssa"):
            try:
                font_audit = analyze_ass_fonts(
                    ass_path=matched_sub,
                    mkv_path=None,
                    season_fonts_dir=season_fonts_dir,
                )
                font_audit_result = font_audit
                total = font_audit.get("total_fonts", 0)
                avail = font_audit.get("available_count", 0)
                missing = font_audit.get("missing_count", 0)
                missing_fonts = font_audit.get("missing_font_names", [])

                if total == 0:
                    font_status = "0/0 ✓"
                elif missing == 0:
                    font_status = f"{avail}/{total} ✓"
                else:
                    font_status = f"{missing}/{total} مفقود"
                    state = EpisodeState.NEEDS_REVIEW.value
                    error = f"خطوط مفقودة ({font_status}): {', '.join(missing_fonts[:5])}"
            except Exception as e:
                logger.warning("Failed to analyze ASS fonts for %s: %s", matched_sub.name, e)
                font_status = "تحتاج مراجعة"
                state = EpisodeState.NEEDS_REVIEW.value
                error = f"خطأ أثناء فحص خطوط ASS: {str(e)}"
        else:
            # SRT or VTT: No font dependency
            font_status = "—"

    elif len(embedded_subs) > 0:
        # No external match, but embedded subtitle exists
        first_emb = next((s for s in embedded_subs if s["codec"] in ("ass", "ssa") and
                          (s.get("language") or "").casefold() in ("ar", "ara", "arabic")), embedded_subs[0])
        subtitle_source = "embedded"
        subtitle_stream_index = first_emb["index"]
        subtitle_format = first_emb["codec"]
        subtitle_metadata = normalize_subtitle(
            language=first_emb.get("language"),
            fmt=first_emb.get("codec"),
            title=first_emb.get("title"),
            forced=first_emb.get("forced"),
            default=first_emb.get("default"),
        )
        subtitle_label = subtitle_metadata["label"]
        if first_emb["codec"] in ("ass", "ssa"):
            temporary_ass = TEMP_DIR / "scanner_ass" / f"{hashlib.sha256(str(video_path).encode()).hexdigest()[:12]}_{first_emb['index']}.ass"
            extracted_ass = _extract_embedded_ass(video_path, int(first_emb["index"]), temporary_ass)
            if extracted_ass:
                try:
                    audit = analyze_ass_fonts(extracted_ass, mkv_path=None, season_fonts_dir=season_fonts_dir)
                    font_audit_result = audit
                    missing_fonts = audit.get("missing_font_names", [])
                    if audit.get("missing_count", 0):
                        font_status = f"{audit['missing_count']}/{audit['total_fonts']} مفقود"
                        state = EpisodeState.NEEDS_REVIEW.value
                        error = f"خطوط مفقودة ({font_status}): {', '.join(missing_fonts[:5])}"
                    else:
                        font_status = f"{audit.get('available_count', 0)}/{audit.get('total_fonts', 0)} ✓"
                except Exception as exc:
                    font_status = "تحتاج مراجعة"
                    state = EpisodeState.NEEDS_REVIEW.value
                    error = f"خطأ أثناء فحص خطوط ASS المضمنة: {exc}"
            else:
                font_status = "تعذر فحص ASS"
                state = EpisodeState.NEEDS_REVIEW.value
                error = "تعذر استخراج ترجمة ASS المضمنة لفحص خطوطها"
        else:
            font_status = "—"

    else:
        # No external and no embedded subtitle
        subtitle_source = "none"
        subtitle_label = "بدون ترجمة"
        font_status = "—"

    if subtitle_source == "none":
        subtitle_metadata = normalize_subtitle(fmt="unknown")
        subtitle_metadata["label"] = "بدون ترجمة"
    elif subtitle_source == "needs_review":
        subtitle_metadata = normalize_subtitle(fmt=subtitle_format or "unknown")
        subtitle_metadata["label"] = subtitle_label
    return {
        "subtitle_source": subtitle_source,
        "subtitle_path": subtitle_path,
        "subtitle_format": subtitle_format,
        "subtitle_stream_index": subtitle_stream_index,
        "subtitle_label": subtitle_label,
        "subtitle_metadata": subtitle_metadata,
        "font_status": font_status,
        "missing_fonts": missing_fonts,
        "font_audit": font_audit_result,
        "subtitle_candidates": candidates_data,
        "state": state,
        "error": error,
    }


def discover_season_videos(folder_path: Union[str, Path]) -> List[Path]:
    """Discover every supported video without following directory symlinks."""
    import os
    root = Path(folder_path).resolve()
    videos = []
    def fail(exc):
        raise exc
    for current, dirs, names in os.walk(root, followlinks=False, onerror=fail):
        dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
        videos.extend(Path(current) / name for name in names
                      if Path(name).suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
                      and not (Path(current) / name).is_symlink())
    return sorted(videos, key=lambda item: natural_sort_key(item.relative_to(root).as_posix()))


def scan_season_folder(
    folder_path: Union[str, Path],
    on_episode_scanned: Optional[Callable[[Dict[str, Any]], None]] = None,
    *, video_files: Optional[List[Path]] = None,
) -> List[Dict[str, Any]]:
    """Scans a local season folder for video files and returns sorted episode candidate records
    with deterministic subtitle matching and font audit.
    
    Args:
        folder_path: Absolute or relative path to the season folder.
        
    Returns:
        List of episode dicts ready for insertion into BatchStore, sorted by natural order.
        
    Raises:
        FileNotFoundError: If the folder does not exist.
        NotADirectoryError: If the path is not a directory.
    """
    p = Path(folder_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"المجلد المحدد غير موجود: {folder_path}")
    if not p.is_dir():
        raise NotADirectoryError(f"المسار ليس مجلداً: {folder_path}")

    video_files = discover_season_videos(p) if video_files is None else video_files

    # Pre-gather all subtitles and fonts in season directory
    all_subtitles = find_season_subtitles(p)
    season_fonts_dir = _prepare_season_font_reuse([], all_subtitles, p)

    episodes: List[Dict[str, Any]] = []
    for idx, fpath in enumerate(video_files, start=1):
        _prepare_season_font_reuse([fpath], all_subtitles, p)
        file_size = fpath.stat().st_size
        ep_num = parse_episode_number(fpath.name)
        display_label = _episode_label(ep_num, idx)
        try:
            media_probe = probe_video(fpath)
            duration_seconds = float(media_probe.get("duration_seconds", 0) or 0)
            source_width = int(media_probe.get("width", 0) or 0)
            source_height = int(media_probe.get("height", 0) or 0)
        except Exception as exc:
            logger.info("Duration probe unavailable for %s: %s", fpath.name, exc)
            media_probe = {}
            duration_seconds = 0.0
            source_width = source_height = 0
        estimate_bps = sum(
            spec["video_bitrate_bps"] + spec["audio_bitrate_bps"]
            for spec in LADDER_SPECS.values()
            if spec["height"] <= source_height or spec["width"] <= source_width
        )
        output_estimate_bytes = int(duration_seconds * estimate_bps / 8)

        # Run subtitle matching and font audit
        audit = audit_subtitle_and_fonts(
            video_path=fpath,
            all_subtitles=all_subtitles,
            season_fonts_dir=season_fonts_dir,
            embedded_subs=media_probe.get("subtitle_tracks") if media_probe else None,
        )

        episode = {
            "order_index": idx,
        "episode_number": ep_num if ep_num is not None else idx,
            "duration_seconds": duration_seconds,
            "source_bytes": file_size,
            "source_width": source_width,
            "source_height": source_height,
            "source_video_codec": media_probe.get("video_codec", ""),
            "source_video_profile": media_probe.get("video_profile", ""),
            "source_video_level": int(media_probe.get("video_level", 0) or 0),
            "source_pixel_format": media_probe.get("pixel_format", ""),
            "source_fps": float(media_probe.get("fps", 0) or 0),
            "source_video_bitrate_bps": int(media_probe.get("video_bitrate_bps", 0) or 0),
            "source_audio_codec": media_probe.get("audio_codec", ""),
            "source_audio_profile": media_probe.get("audio_profile", ""),
            "source_audio_channels": int(media_probe.get("audio_channels", 0) or 0),
            "source_audio_sample_rate": str(media_probe.get("audio_sample_rate", "")),
            "source_audio_bitrate_bps": int(media_probe.get("audio_bitrate_bps", 0) or 0),
            "output_estimate_bytes": output_estimate_bytes,
            "source_path": str(fpath.resolve()),
            "filename": fpath.name,
            "display_label": display_label,
            "state": audit["state"],
            "subtitle_path": audit["subtitle_path"],
            "subtitle_source": audit["subtitle_source"],
            "subtitle_format": audit["subtitle_format"],
            "subtitle_stream_index": audit["subtitle_stream_index"],
            "subtitle_label": audit["subtitle_label"],
            "subtitle_metadata": audit["subtitle_metadata"],
            "font_status": audit["font_status"],
            "missing_fonts": audit["missing_fonts"],
            "font_audit": audit.get("font_audit", {}),
            "subtitle_candidates": audit["subtitle_candidates"],
            "settings_json": "{}",
            "work_dir": None,
            "video_id": None,
            "session_id": None,
            "source_bytes": file_size,
            "package_bytes": 0,
            "uploaded_bytes": 0,
            "error": audit["error"],
        }
        episodes.append(episode)
        if on_episode_scanned is not None:
            on_episode_scanned(episode)

    # Later episodes may carry the exact font missing from an earlier subtitle.
    for episode in episodes:
        if episode.get("missing_fonts"):
            audit = audit_subtitle_and_fonts(Path(episode["source_path"]), all_subtitles, season_fonts_dir)
            episode.update(audit)
            if on_episode_scanned is not None:
                on_episode_scanned(episode)
    return episodes
