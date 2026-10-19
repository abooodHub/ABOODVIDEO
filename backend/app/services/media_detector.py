"""Deterministic Media Auto-Detection Service.

Parses video filenames and folder context to identify Series, Season, and Episode metadata.
Does not use LLMs for basic parsing. Returns deterministic confidence scores (HIGH, MEDIUM, LOW).
"""
import re
from typing import Optional, Dict, Any, List, Tuple


# Common video extensions to strip
EXTENSIONS = [".mkv", ".mp4", ".webm", ".avi", ".ts", ".m4v", ".mov"]

# Release groups and tech tags to clean
TAG_PATTERNS = [
    r"\[[^\]]+\]",  # [SILVER], [4K], [HEVC], etc.
    r"\((?:19|20)\d{2}\)",  # (2024), (2021)
    r"\b(?:2160p|1440p|1080p|720p|480p|360p|4K|UHD|HD|FHD)\b",
    r"\b(?:x264|x265|h264|h265|hevc|avc|10bit|8bit|aac|ac3|dts|web-dl|bluray|bdrip|remux)\b",
]

# Season & Episode patterns
# 1. S01E02 / s1e2
REGEX_S_E = re.compile(r"[sS](\d{1,2})[eE](\d{1,4})", re.IGNORECASE)
# 2. Season 1 Episode 2 / Season 01 Ep 02
REGEX_SEASON_EPISODE_WORDS = re.compile(
    r"[sS]eason\s*(\d{1,2})\s*(?:[eE]pisode|[eE]p)?\s*(\d{1,4})", re.IGNORECASE
)
# 3. 01x02
REGEX_X = re.compile(r"\b(\d{1,2})[xX](\d{1,4})\b")
# 4. S1 01 (e.g. Spirit Cage Incarnation S1 01)
REGEX_S_SPACE_E = re.compile(r"\b[sS](\d{1,2})\s+(\d{1,4})\b")
# 5. Episode 85 / Ep 85 / E085 / E85
REGEX_EP_ONLY = re.compile(r"\b(?:[eE]pisode|[eE]p|[eE])\s*(\d{1,4})\b", re.IGNORECASE)
# 6. - 085 or _ 085 (e.g. Swallowed Star - 085)
REGEX_DASH_EP = re.compile(r"[-_]\s*(\d{2,4})\b")
# 7. Quality detection
REGEX_QUALITY = re.compile(r"\b(2160p|1440p|1080p|720p|480p|360p|4[kK])\b", re.IGNORECASE)


def clean_title_string(s: str) -> str:
    """Clean separators like dots, underscores, extra spaces into clean title text."""
    s = s.replace(".", " ").replace("_", " ")
    return " ".join(s.split()).strip()


def parse_filename_metadata(
    filename: str,
    folder_context: Optional[Dict[str, Any]] = None,
    existing_series_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Parse a media filename into recognized Series, Season, Episode, and Quality metadata.
    Returns:
    {
        "series_name": str or None,
        "season_number": int or None,
        "episode_number": int or None,
        "quality": str or None,
        "suggested_title": str,
        "confidence": "HIGH" | "MEDIUM" | "LOW",
        "reason": str,
    }
    """
    stem = filename
    for ext in EXTENSIONS:
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break

    # Extract detected quality before cleaning tags
    quality_match = REGEX_QUALITY.search(stem)
    detected_quality = None
    if quality_match:
        raw_q = quality_match.group(1)
        if raw_q.upper() == "4K":
            detected_quality = "2160p"
        else:
            detected_quality = raw_q.lower()

    # Check tags for clean processing
    clean_stem = stem
    for pat in TAG_PATTERNS:
        clean_stem = re.sub(pat, " ", clean_stem, flags=re.IGNORECASE)
    clean_stem = " ".join(clean_stem.split())

    season_num: Optional[int] = None
    episode_num: Optional[int] = None
    series_candidate: Optional[str] = None
    pattern_found = False

    # 1. Check S01E02
    m = REGEX_S_E.search(stem)
    if m:
        season_num = int(m.group(1))
        episode_num = int(m.group(2))
        series_candidate = clean_stem[: m.start()]
        pattern_found = True

    # 2. Check Season 1 Episode 2
    if not pattern_found:
        m = REGEX_SEASON_EPISODE_WORDS.search(stem)
        if m:
            season_num = int(m.group(1))
            episode_num = int(m.group(2))
            series_candidate = clean_stem[: m.start()]
            pattern_found = True

    # 3. Check 01x02
    if not pattern_found:
        m = REGEX_X.search(stem)
        if m:
            season_num = int(m.group(1))
            episode_num = int(m.group(2))
            series_candidate = clean_stem[: m.start()]
            pattern_found = True

    # 4. Check S1 01
    if not pattern_found:
        m = REGEX_S_SPACE_E.search(stem)
        if m:
            season_num = int(m.group(1))
            episode_num = int(m.group(2))
            series_candidate = clean_stem[: m.start()]
            pattern_found = True

    # 5. Check Episode 85 / E085
    if not pattern_found:
        m = REGEX_EP_ONLY.search(stem)
        if m:
            episode_num = int(m.group(1))
            series_candidate = clean_stem[: m.start()]
            pattern_found = True

    # 6. Check Swallowed Star - 085
    if not pattern_found:
        m = REGEX_DASH_EP.search(stem)
        if m:
            episode_num = int(m.group(1))
            series_candidate = clean_stem[: m.start()]
            pattern_found = True

    # Resolve Context (Current folder or parent)
    series_from_context = None
    season_from_context = None
    if folder_context:
        if folder_context.get("folder_type") == "season":
            season_from_context = folder_context.get("season_number")
            series_from_context = folder_context.get("series_name")
        elif folder_context.get("folder_type") == "series":
            series_from_context = folder_context.get("series_name") or folder_context.get("name")
        elif "موسم" in (folder_context.get("name") or ""):
            # Guess season from folder name "الموسم 1"
            m_s = re.search(r"(\d+)", folder_context.get("name") or "")
            if m_s:
                season_from_context = int(m_s.group(1))

    # Clean extracted series candidate
    final_series = None
    if series_from_context:
        final_series = series_from_context
    elif series_candidate:
        cand = clean_title_string(series_candidate).strip(" -_")
        if cand:
            final_series = cand

    final_season = season_num if season_num is not None else season_from_context
    final_episode = episode_num

    # Determine confidence
    confidence = "LOW"
    reason = "No media pattern recognized"

    if final_episode is not None:
        if final_series and final_season is not None:
            confidence = "HIGH"
            reason = f"Recognized Series '{final_series}', Season {final_season}, Episode {final_episode}"
        elif final_series or final_season is not None:
            confidence = "MEDIUM"
            reason = f"Recognized Episode {final_episode} with partial series/season context"
        else:
            confidence = "MEDIUM"
            reason = f"Recognized Episode {final_episode}"

    # Suggested display title
    suggested_title = stem
    if final_episode is not None:
        suggested_title = f"الحلقة {final_episode:02d}"

    return {
        "series_name": final_series,
        "season_number": final_season,
        "episode_number": final_episode,
        "quality": detected_quality,
        "suggested_title": suggested_title,
        "confidence": confidence,
        "reason": reason,
    }
