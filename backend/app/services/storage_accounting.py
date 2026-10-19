"""Read-only accounting and orphan discovery for platform-managed storage."""

import os
from pathlib import Path
from typing import Iterable, Mapping, Optional

from app.core.config import settings


def _roots() -> list[tuple[str, Path]]:
    base = Path(settings.STORAGE_BASE_PATH)
    return [
        ("video_storage", Path(settings.STORAGE_VIDEOS_PATH)),
        ("upload_staging", Path(settings.STORAGE_UPLOADS_PATH)),
        ("temporary_processing", Path(settings.STORAGE_TEMP_PATH)),
        ("studio_package_staging", Path(settings.STORAGE_PACKAGES_STAGING_PATH)),
        ("source_import", base / "sources"),
    ]


def _root_category(path: Path, roots: Iterable[tuple[str, Path]]) -> str:
    matches = [(name, root) for name, root in roots if path == root or root in path.parents]
    if not matches:
        return "other_managed_storage"
    return max(matches, key=lambda pair: len(pair[1].parts))[0]


def scan_managed_storage(video_states: Optional[Mapping[str, str]] = None) -> dict:
    """Walk file metadata only and classify video bytes using current DB state.

    `video_states` maps video UUID strings to `published`, `trash`, or
    `unpublished`; missing IDs are reported as orphan storage candidates.
    Symlinks are never followed and no file contents are read.
    """
    roots = _roots()
    base = Path(settings.STORAGE_BASE_PATH)
    totals: dict[str, int] = {
        name: 0 for name in (
            "published_media", "trash_media", "unpublished_video_storage",
            "orphan_video_storage", "upload_staging", "studio_package_staging",
            "temporary_processing", "source_import", "other_managed_storage",
        )
    }
    file_count = 0
    errors: list[str] = []
    orphan_video_directories: set[str] = set()
    if not base.exists():
        return {
            "scan_complete": False,
            "file_count": 0,
            "categories": totals,
            "managed_total_bytes": 0,
            "orphan_video_directories": [],
            "errors": ["storage_base_missing"],
        }

    states = video_states or {}
    stack = [base]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            path = Path(entry.path)
                            root_kind = _root_category(path, roots)
                            if root_kind == "video_storage":
                                relative = path.relative_to(Path(settings.STORAGE_VIDEOS_PATH))
                                video_id = relative.parts[0] if relative.parts else ""
                                state = states.get(video_id)
                                if state == "published":
                                    category = "published_media"
                                elif state == "trash":
                                    category = "trash_media"
                                elif state == "unpublished":
                                    category = "unpublished_video_storage"
                                else:
                                    category = "orphan_video_storage"
                                    if video_id:
                                        orphan_video_directories.add(video_id)
                            else:
                                category = root_kind
                            totals[category] += entry.stat(follow_symlinks=False).st_size
                            file_count += 1
                    except OSError:
                        if len(errors) < 50:
                            errors.append("entry_stat_failed")
        except OSError:
            if len(errors) < 50:
                errors.append("directory_scan_failed")

    return {
        "scan_complete": not errors,
        "file_count": file_count,
        "categories": totals,
        "managed_total_bytes": sum(totals.values()),
        "orphan_video_directories": sorted(orphan_video_directories)[:100],
        "orphan_video_directory_count": len(orphan_video_directories),
        "errors": errors,
    }


def find_orphan_temporary_resources(
    *,
    active_transcodes: set[tuple[str, str]],
    active_upload_ids: set[str],
    active_package_sessions: set[tuple[str, str]],
    max_items: int = 100,
) -> dict:
    """Identify stale known TEMP roots without deleting or changing anything."""
    if not 1 <= max_items <= 1000:
        raise ValueError("max_items must be between 1 and 1000")
    candidates: list[dict[str, str]] = []
    errors: list[str] = []

    def add(kind: str, identity: str) -> None:
        if len(candidates) < max_items:
            candidates.append({"kind": kind, "identity": identity[:160]})

    roots = (
        ("upload", Path(settings.STORAGE_TEMP_PATH) / "uploads"),
        ("transcode", Path(settings.STORAGE_TEMP_PATH) / "transcodes"),
        ("package", Path(settings.STORAGE_PACKAGES_STAGING_PATH)),
    )
    for kind, root in roots:
        if not root.exists():
            continue
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                            continue
                        first = entry.name
                        if kind == "upload":
                            if first not in active_upload_ids:
                                add(kind, first)
                            continue
                        child = Path(entry.path)
                        with os.scandir(child) as nested_entries:
                            for nested in nested_entries:
                                if nested.is_symlink() or not nested.is_dir(follow_symlinks=False):
                                    continue
                                identity = f"{first}/{nested.name}"
                                active = (first, nested.name) in (
                                    active_transcodes if kind == "transcode" else active_package_sessions
                                )
                                if not active:
                                    add(kind, identity)
                    except OSError:
                        if len(errors) < 50:
                            errors.append("temporary_entry_scan_failed")
        except OSError:
            if len(errors) < 50:
                errors.append("temporary_directory_scan_failed")
    return {
        "scan_complete": not errors,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "errors": errors,
        "automatic_cleanup": False,
    }


def find_missing_managed_objects(
    references: Iterable[Mapping[str, str]],
    *,
    max_items: int = 100,
) -> dict:
    """Report database-referenced managed objects that are absent from disk.

    References are read-only metadata supplied by the caller. Paths outside
    the configured managed storage root are ignored, and symlink resolution
    must remain within that root before existence is checked.
    """
    if not 1 <= max_items <= 1000:
        raise ValueError("max_items must be between 1 and 1000")

    base = Path(settings.STORAGE_BASE_PATH).resolve()
    missing: list[dict[str, str]] = []
    missing_count = 0
    errors: list[str] = []

    for reference in references:
        raw_path = reference.get("path")
        if not raw_path:
            continue
        try:
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = base / candidate
            resolved = candidate.resolve(strict=False)
            if resolved != base and base not in resolved.parents:
                continue
            if resolved.exists():
                continue
            missing_count += 1
            if len(missing) < max_items:
                missing.append({
                    "kind": str(reference.get("kind", "managed_object"))[:80],
                    "identity": str(reference.get("identity", ""))[:160],
                    "path": resolved.relative_to(base).as_posix()[:500],
                })
        except (OSError, RuntimeError, ValueError):
            if len(errors) < 50:
                errors.append("managed_reference_check_failed")

    return {
        "scan_complete": not errors,
        "missing_count": missing_count,
        "missing_objects": missing,
        "errors": errors,
        "automatic_cleanup": False,
    }
