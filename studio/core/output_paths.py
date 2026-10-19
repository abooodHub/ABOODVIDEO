"""Safe, deterministic paths for retained Studio output packages."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
import json
from typing import Any, Dict, Optional

from studio.config import TEMP_DIR, validate_converted_output_folder


_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))
}


def claim_output_directory(directory: Path, episode_id: str, source_path: Path) -> None:
    """Atomic ownership record; retries may reuse only their own destination."""
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / ".studio-output-owner.json"
    identity = {"episode_id": episode_id, "source_path": str(source_path.resolve())}
    try:
        with marker.open("x", encoding="utf-8") as stream:
            json.dump(identity, stream, ensure_ascii=False)
    except FileExistsError:
        if json.loads(marker.read_text(encoding="utf-8")) != identity:
            raise ValueError("مجلد الإخراج محجوز لمهمة أخرى؛ لم يتم استبدال أي ملف.")


def safe_path_component(value: Any, fallback: str) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(value or "")).strip().rstrip(". ")
    text = re.sub(r"\s+", " ", text)
    if not text or text.split(".", 1)[0].casefold() in _WINDOWS_RESERVED:
        text = fallback
    return text[:120].rstrip(". ") or fallback


def _output_root(config: Dict[str, Any], *, allow_test_fallback: bool = False) -> Path:
    value = config.get("converted_output_folder", "")
    if value:
        return validate_converted_output_folder(value)
    if allow_test_fallback:
        return TEMP_DIR.resolve()
    raise ValueError("اختر مجلد Converted Output من الإعدادات قبل بدء المعالجة.")


def batch_output_directory(
    config: Dict[str, Any],
    *,
    source_path: Path,
    season_folder: Path,
    episode_number: Any,
    existing_work_dir: Optional[str] = None,
    preferred_work_dir: Optional[str] = None,
    allow_test_fallback: bool = False,
) -> Path:
    # Imports persist the canonical output path in the episode settings. Prefer
    # it over a legacy work_dir: older Studio builds stored episodes from
    # different seasons under the same Episode NNN folder.
    if preferred_work_dir:
        return Path(preferred_work_dir).resolve()
    if existing_work_dir:
        return Path(existing_work_dir).resolve()
    if allow_test_fallback and not config.get("converted_output_folder"):
        root = Path(season_folder).resolve() / ".aboodvideo-test-output"
        root.mkdir(parents=True, exist_ok=True)
    else:
        root = _output_root(config, allow_test_fallback=False)
    source = Path(source_path).resolve()
    # Season imports always keep the series/season/episode package structure.
    series = safe_path_component(Path(season_folder).resolve().parent.name, "Series")
    season = safe_path_component(Path(season_folder).resolve().name, "Season")
    try:
        number = Decimal(str(episode_number))
        if not number.is_finite():
            raise ValueError("Non-finite episode number")
        whole, _, fraction = format(number.normalize(), "f").partition(".")
        ep_label = f"Episode {int(whole):03d}" + (f".{fraction}" if fraction else "")
    except (TypeError, ValueError, InvalidOperation):
        ep_label = f"Episode {safe_path_component(episode_number, source.stem)}"
    return root / series / season / ep_label


def fresh_batch_output_directory(
    config: Dict[str, Any],
    *,
    source_path: Path,
    season_folder: Path,
    episode_number: Any,
    preferred_work_dir: Optional[str] = None,
) -> Path:
    """Allocate a non-destructive destination for a replacement package.

    Existing package folders are retained. A replacement is written to the
    normal series/season/episode layout, with a numbered sibling when that
    destination already contains files.
    """
    candidate = batch_output_directory(
        config,
        source_path=source_path,
        season_folder=season_folder,
        episode_number=episode_number,
        preferred_work_dir=preferred_work_dir,
    )
    if not candidate.exists() or (candidate.is_dir() and not any(candidate.iterdir())):
        return candidate

    suffix = 2
    while True:
        versioned = candidate.with_name(f"{candidate.name} ({suffix})")
        if not versioned.exists() or (versioned.is_dir() and not any(versioned.iterdir())):
            return versioned
        suffix += 1


def single_output_directory(config: Dict[str, Any], *, source_path: Path, title: str, job_id: str) -> Path:
    root = _output_root(config)
    source = Path(source_path).resolve()
    base = safe_path_component(source.stem if config.get("output_organization") == "original" else title, "Media")
    candidate = root / base
    if candidate.exists() and (not candidate.is_dir() or any(candidate.iterdir())):
        candidate = root / f"{base} ({job_id[:8]})"
    return candidate
