"""UTF-8 subtitle metadata normalization for Studio and prepared packages."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional


_LANGUAGES = {
    "ar": ("ar", "العربية"),
    "ara": ("ar", "العربية"),
    "arabic": ("ar", "العربية"),
    "العربية": ("ar", "العربية"),
    "en": ("en", "English"),
    "eng": ("en", "English"),
    "english": ("en", "English"),
}


def normalize_language(value: Any) -> tuple[str, str]:
    raw = str(value or "").strip().casefold().lstrip("@")
    return _LANGUAGES.get(raw, ("und", "غير معروف"))


def infer_language_from_path(path_value: Any) -> str:
    if not path_value:
        return "und"
    stem = Path(str(path_value)).stem.casefold()
    tokens = [token for token in re.split(r"[.\s_\-\[\]()]+", stem) if token]
    for token in reversed(tokens):
        if token in _LANGUAGES:
            return token
    return "und"


def normalize_subtitle(
    *,
    language: Any = None,
    fmt: Any = None,
    title: Any = None,
    forced: Any = False,
    default: Any = False,
    path: Any = None,
) -> Dict[str, Any]:
    code, name = normalize_language(language or infer_language_from_path(path))
    format_name = str(fmt or (Path(str(path)).suffix.lstrip(".") if path else "unknown")).upper()
    clean_title = re.sub(r"\s+", " ", str(title or "")).strip()
    return {
        "language_code": code,
        "language_name": name,
        "format": format_name,
        "title": clean_title,
        "forced": bool(forced),
        "default": bool(default),
        "path": str(path or ""),
        "label": format_subtitle_label(name, format_name, bool(default), bool(forced)),
    }


def format_subtitle_label(language_name: str, fmt: str, default: bool = False, forced: bool = False) -> str:
    parts = [language_name or "غير معروف", (fmt or "UNKNOWN").upper()]
    if default:
        parts.append("افتراضية")
    if forced:
        parts.append("Forced")
    return " · ".join(parts)
