"""Read-only import discovery and durable source/destination identity."""
from __future__ import annotations

from collections import Counter
import hashlib
import os
from pathlib import Path
import re

from studio.core.episode_scanner import SUPPORTED_VIDEO_EXTENSIONS, natural_sort_key, parse_episode_number
from studio.core.output_paths import safe_path_component


def season_number(name):
    if re.fullmatch(r"(?:specials?|extras?|ova|الحلقات الخاصة|خاص)", name.strip(), re.I):
        return 0
    match = re.search(r"(?:^|[\s._-])(?:season[\s._-]*|s|الموسم\s*)(\d{1,3})(?:$|[\s._-])", name, re.I)
    return int(match.group(1)) if match else None


def source_context(path, kind):
    source = Path(path).resolve(strict=True)
    if kind == "file":
        if not source.is_file() or source.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
            raise ValueError("اختر ملف فيديو مدعوماً.")
        parent = source.parent
    else:
        if not source.is_dir():
            raise ValueError("مجلد المصدر غير موجود.")
        parent = source
    if kind == "series":
        root = source
    else:
        season = next((p for p in (parent, *parent.parents) if season_number(p.name) is not None), None)
        root = season.parent if season else (parent.parent if kind == "season" else parent)
    return source, root


def discover_import(path, kind, config=None):
    source, root = source_context(path, kind)
    errors, ignored, files = [], 0, []
    if kind == "file":
        files = [source]
    else:
        configured_output = (config or {}).get("converted_output_folder")
        output_root = Path(configured_output).resolve() if configured_output else None
        if output_root is not None and (source == output_root or source.is_relative_to(output_root)):
            raise ValueError("اختر مجلد المصدر الأصلي؛ مجلد الإخراج ليس مصدراً للاستيراد.")
        def error(exc):
            errors.append({"path": str(exc.filename or source), "message": "تعذر قراءة المجلد"})
        for current, dirs, names in os.walk(source, followlinks=False, onerror=error):
            dirs[:] = sorted((d for d in dirs if not (Path(current) / d).is_symlink()
                              and not (Path(current) / d).is_junction()
                              and not d.startswith(".aboodvideo")
                              and (output_root is None or (Path(current) / d).resolve() != output_root)), key=natural_sort_key)
            for name in names:
                item = Path(current) / name
                if item.is_symlink():
                    errors.append({"path": str(item), "message": "تم تجاهل رابط رمزي لحماية نطاق المصدر"})
                elif item.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS:
                    files.append(item)
                else:
                    ignored += 1
    entries, seen = [], set()
    for item in files:
        try:
            resolved = item.resolve(strict=True)
            relative = resolved.relative_to(root)
            identity = str(resolved).casefold()
            if identity in seen:
                continue
            seen.add(identity)
            size = resolved.stat().st_size
            directory_seasons = [(part, season_number(part)) for part in relative.parts[:-1]
                                 if season_number(part) is not None]
            directory_number = directory_seasons[-1][1] if directory_seasons else None
            match = re.search(r"(?<![a-z0-9])s(\d{1,3})[\s._-]*e\d", item.stem, re.I)
            file_season = int(match.group(1)) if match else None
            number = parse_episode_number(item.name)
            detected = file_season if file_season is not None else directory_number
            issues = []
            if number is None:
                issues.append("رقم الحلقة غير محدد؛ يحتاج مراجعة")
            if file_season is not None and directory_number is not None and file_season != directory_number:
                issues.append("رقم الموسم في اسم الملف يختلف عن المجلد")
            name = directory_seasons[-1][0] if directory_seasons else (
                "Specials" if detected == 0 else f"Season {detected:02d}" if detected is not None else
                relative.parts[0] if len(relative.parts) > 1 else "غير محدد")
            entries.append({"source_path": str(resolved), "filename": item.name,
                            "relative_source_path": relative.as_posix(), "series_name": root.name,
                            "season_name": name, "season_number": detected, "episode_number": number,
                            "source_bytes": size, "issues": issues})
        except (OSError, ValueError) as exc:
            errors.append({"path": str(item), "message": f"تعذر قراءة الملف: {type(exc).__name__}"})
    counts = Counter((e["season_number"], str(e["episode_number"])) for e in entries if e["episode_number"] is not None)
    for entry in entries:
        entry["duplicate"] = entry["episode_number"] is not None and counts[(entry["season_number"], str(entry["episode_number"]))] > 1
        if entry["duplicate"]:
            entry["issues"].append("رقم حلقة مكرر داخل الموسم؛ احتُفظ بالملفين للمراجعة")
    def sort_key(entry):
        number = entry["episode_number"]
        return (entry["season_number"] if entry["season_number"] is not None else 100000,
                0 if isinstance(number, (int, float)) else 1,
                float(number) if isinstance(number, (int, float)) else 0,
                natural_sort_key(entry["relative_source_path"]))
    entries.sort(key=sort_key)
    for entry in entries:
        entry["output_directory"] = planned_output(entry, root, config or {})
    seasons = {}
    for entry in entries:
        key = (entry["season_number"], entry["season_name"])
        seasons.setdefault(key, {"name": entry["season_name"], "season_number": entry["season_number"], "episodes": []})["episodes"].append(entry)
    return {"kind": kind, "source_path": str(source), "source_root": str(root), "series_name": root.name,
            "season_name": source.name, "episodes": entries,
            "seasons": [{**value, "episode_count": len(value["episodes"])} for value in seasons.values()],
            "episode_count": len(entries), "valid_episode_count": sum(not e["issues"] for e in entries),
            "duplicate_count": sum(e["duplicate"] for e in entries),
            "ambiguous_count": sum(bool(e["issues"]) for e in entries),
            "season_count": len(seasons), "source_bytes": sum(e["source_bytes"] for e in entries),
            "ignored_file_count": ignored, "errors": errors,
            "output_root": str((config or {}).get("converted_output_folder") or "")}


def planned_output(entry, source_root, config):
    configured = config.get("converted_output_folder")
    if not configured:
        return ""
    root = Path(configured).resolve()
    relative = Path(entry["relative_source_path"])
    series = str(entry["series_name"])
    # Overlap suffixes, e.g. output already points at Series/Season 01.
    components = [series, *relative.parts[:-1]]
    overlap = 0
    for length in range(1, min(len(root.parts), len(components)) + 1):
        if [p.casefold() for p in root.parts[-length:]] == [p.casefold() for p in components[:length]]:
            overlap = length
    parent = root.joinpath(*components[overlap:]).resolve()
    if not parent.is_relative_to(root):
        raise ValueError("مسار إخراج غير آمن")
    number = entry["episode_number"]
    label = f"Episode {number:03d}" if isinstance(number, int) else f"Episode {number}" if number is not None else safe_path_component(relative.stem, "Media")
    if entry["duplicate"] or number is None:
        label += " - " + hashlib.sha256(entry["relative_source_path"].casefold().encode()).hexdigest()[:10]
    candidate = parent / safe_path_component(label, "Media")
    # Never assign a pre-existing output folder to a fresh import.
    suffix = 2
    original = candidate
    while candidate.exists():
        candidate = original.with_name(f"{original.name} ({suffix})")
        suffix += 1
    return str(candidate)
