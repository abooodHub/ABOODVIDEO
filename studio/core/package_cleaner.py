"""Safe local package cleaner for ABOODVIDEO Studio.

Strictly enforces:
1. Episode state == READY and server_status == READY.
2. Path containment strictly within managed Studio work root (TEMP_DIR).
3. Symlink rejection (no symlink escapes to outside directories).
4. Critical system & source protection (never deletes source videos, subtitles, fonts, db, or project files).
5. Idempotent execution (safe to call multiple times without error).
"""
import os
import json
import hashlib
import re
import shutil
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List, Set

from studio.config import TEMP_DIR, FONTS_DIR, PROJECT_ROOT
from studio.core.batch_store import EpisodeState

logger = logging.getLogger(__name__)

_FINAL_QUALITY_DIRS = {"2160p", "1080p", "720p", "480p", "360p"}
_KNOWN_HLS_INTERMEDIATES = (
    re.compile(r"seg_\d+\.ts\Z", re.IGNORECASE),
    re.compile(r"index\d+\.vtt\Z", re.IGNORECASE),
    re.compile(r"index_vtt\.m3u8\Z", re.IGNORECASE),
)


class PackageCleanupSecurityError(Exception):
    """Raised when a directory fails strict safety containment or escape validation."""
    pass


class PackageCleanupStateError(Exception):
    """Raised when an episode is not in a valid state for package cleanup."""
    pass


def _safe_package_relative(root: Path, value: str) -> Optional[str]:
    """Normalize a package-relative path without following it outside the root."""
    normalized = str(value or "").replace("\\", "/")
    candidate = Path(normalized)
    if not normalized or candidate.is_absolute() or ".." in candidate.parts or ":" in normalized:
        return None
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return candidate.as_posix()


def classify_final_package_files(package_dir: Any) -> Dict[str, List[str]]:
    """Dry-run inventory: final references, known intermediates, and unknowns.

    Only known FFmpeg HLS intermediate names directly inside a rendition folder are
    eligible for cleanup. Unknown files and symlinks are always preserved.
    """
    root = Path(package_dir).resolve()
    manifest_path = root / "manifest.json"
    if not root.is_dir() or not manifest_path.is_file():
        raise ValueError("A verified package directory with manifest.json is required.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    final: Set[str] = {"manifest.json"}
    if (root / "studio_package_identity.json").is_file():
        final.add("studio_package_identity.json")
    for item in manifest.get("files", []):
        rel = _safe_package_relative(root, item.get("path"))
        if rel is None:
            raise ValueError("Package manifest contains an unsafe file path.")
        final.add(rel)
    for subtitle in manifest.get("subtitles", []):
        rel = _safe_package_relative(root, subtitle.get("path"))
        if rel:
            final.add(rel)
    for font in manifest.get("fonts", []):
        rel = _safe_package_relative(root, font.get("path"))
        if rel:
            final.add(rel)
    # A playlist can reference files not correctly declared by the manifest. Keep
    # those files final so classification can never delete playback dependencies.
    for rendition in manifest.get("renditions", []):
        playlist_rel = _safe_package_relative(root, rendition.get("playlist_path"))
        if not playlist_rel:
            raise ValueError("Package manifest contains an unsafe playlist path.")
        final.add(playlist_rel)
        playlist = root / playlist_rel
        if not playlist.is_file():
            raise ValueError(f"Package playlist is missing: {playlist_rel}")
        for line in playlist.read_text(encoding="utf-8", errors="replace").splitlines():
            uri = line.strip()
            if not uri or uri.startswith("#"):
                continue
            if ":" in uri or uri.startswith("/") or "\\" in uri:
                raise ValueError("Package playlist contains an unsafe URI.")
            rel = _safe_package_relative(root, (Path(playlist_rel).parent / uri).as_posix())
            if rel is None:
                raise ValueError("Package playlist contains a path traversal.")
            final.add(rel)

    result: Dict[str, List[str]] = {
        "FINAL_REFERENCED": [],
        "SAFE_INTERMEDIATE_DELETE": [],
        "UNKNOWN_PRESERVE": [],
    }
    for path in root.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in final:
            result["FINAL_REFERENCED"].append(rel)
            continue
        parts = Path(rel).parts
        known_name = any(pattern.fullmatch(path.name) for pattern in _KNOWN_HLS_INTERMEDIATES)
        if (not path.is_symlink() and path.is_file() and len(parts) == 2
                and parts[0] in _FINAL_QUALITY_DIRS and known_name):
            result["SAFE_INTERMEDIATE_DELETE"].append(rel)
        elif (not path.is_symlink() and path.is_file() and len(parts) == 2
              and parts[0] == "subtitles" and path.stem == "embedded_raw"
              and path.suffix.lower() in {".ass", ".ssa", ".srt", ".vtt"}):
            # BatchRunner extracts embedded subtitles under this exact temporary
            # basename, then process_softsub_file writes the declared playback file.
            result["SAFE_INTERMEDIATE_DELETE"].append(rel)
        else:
            result["UNKNOWN_PRESERVE"].append(rel)
    for values in result.values():
        values.sort()
    return result


def cleanup_final_package_intermediates(package_dir: Any, *, apply: bool = False) -> Dict[str, Any]:
    """Classify safely; delete only allowlisted unreferenced HLS intermediates.

    `apply=False` is the side-effect-free dry run used by audits/tests.
    """
    root = Path(package_dir).resolve()
    inventory = classify_final_package_files(root)
    deleted: List[str] = []
    freed_bytes = 0
    if apply:
        for rel in inventory["SAFE_INTERMEDIATE_DELETE"]:
            target = root / rel
            if target.is_symlink() or not target.is_file():
                continue
            resolved = target.resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            freed_bytes += target.stat().st_size
            target.unlink()
            deleted.append(rel)
    return {
        **inventory,
        "deleted": deleted,
        "freed_bytes": freed_bytes,
        "applied": bool(apply),
    }


def build_final_package_inventory(package_dir: Any, *, verify_hashes: bool = False, cancel_event=None, progress_callback=None) -> Dict[str, Any]:
    """Build the transport inventory from manifest-declared files only."""
    root = Path(package_dir).resolve()
    classified = classify_final_package_files(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    declared = {item["path"].replace("\\", "/"): item for item in manifest.get("files", [])}
    entries: List[Dict[str, Any]] = []
    checked_bytes = 0
    total_bytes = sum(int(item.get("size_bytes", 0)) for item in declared.values())
    for rel, item in sorted(declared.items()):
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("تم إلغاء التحقق المحلي للحزمة.")
        safe_rel = _safe_package_relative(root, rel)
        if safe_rel is None:
            raise ValueError("Package manifest contains an unsafe file path.")
        path = root / safe_rel
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Declared package file is missing or unsafe: {safe_rel}")
        size = path.stat().st_size
        expected_size = int(item.get("size_bytes", -1))
        if size != expected_size or size <= 0:
            raise ValueError(f"Declared package file size does not match: {safe_rel}")
        digest = str(item.get("sha256", "")).lower()
        if verify_hashes:
            hasher = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    if cancel_event and cancel_event.is_set():
                        raise RuntimeError("تم إلغاء التحقق المحلي للحزمة.")
                    hasher.update(chunk)
                    checked_bytes += len(chunk)
                    if progress_callback:
                        progress_callback({"stage":"hashing_package", "preparation_progress":checked_bytes * 100 / max(1,total_bytes)})
            if hasher.hexdigest() != digest:
                raise ValueError(f"Declared package file checksum does not match: {safe_rel}")
        entries.append({"path": safe_rel, "size_bytes": size, "sha256": digest})
    metadata_paths = ["manifest.json"]
    if (root / "studio_package_identity.json").is_file():
        metadata_paths.append("studio_package_identity.json")
    for rel in metadata_paths:
        if (root / rel).is_symlink() or not (root / rel).is_file():
            raise ValueError(f"Final package metadata is missing or unsafe: {rel}")
    final_paths = set(declared) | set(metadata_paths)
    # Classifier's playlist walk protects all playback references. Require every
    # such reference to also be a declared file (except the two metadata files).
    referenced = set(classified["FINAL_REFERENCED"])
    missing_declarations = referenced - final_paths
    if missing_declarations:
        raise ValueError("Package contains playback dependencies missing from manifest: " + ", ".join(sorted(missing_declarations)[:5]))
    return {
        "files": entries,
        "metadata_files": metadata_paths,
        "file_count": len(entries),
        "declared_bytes": sum(entry["size_bytes"] for entry in entries),
        "final_referenced": classified["FINAL_REFERENCED"],
        "unknown_preserve": classified["UNKNOWN_PRESERVE"],
    }


def validate_cleanup_path(
    work_dir: Any,
    episode: Optional[Dict[str, Any]] = None,
    managed_root: Optional[Path] = None,
) -> Path:
    """
    Validates that a work_dir is strictly safe to delete:
    - Must be a non-empty path.
    - Must resolve strictly inside managed_root (defaults to TEMP_DIR).
    - Cannot be the managed_root itself.
    - Must not be or contain symlinks targeting outside managed_root.
    - Must not match or contain source_path, season folder, subtitles, fonts, or database.
    """
    if not work_dir:
        raise PackageCleanupSecurityError("Package work_dir is empty or not provided.")

    p_raw = Path(work_dir)
    root = (managed_root or TEMP_DIR).resolve()

    # 1. Symlink check on the work_dir itself
    if p_raw.is_symlink():
        raise PackageCleanupSecurityError(f"Symlink escape rejected: work_dir '{work_dir}' is a symlink.")

    # 2. Component-level symlink checks on raw path
    curr = p_raw
    while curr != curr.parent:
        if curr.is_symlink():
            target = curr.resolve()
            try:
                if not target.is_relative_to(root) or target == root:
                    raise PackageCleanupSecurityError(
                        f"Symlink escape rejected: component '{curr}' targets outside managed root: {target}"
                    )
            except AttributeError:
                # Python < 3.9 fallback
                try:
                    target.relative_to(root)
                except ValueError:
                    raise PackageCleanupSecurityError(
                        f"Symlink escape rejected: component '{curr}' targets outside managed root: {target}"
                    )
        if curr == root or curr.resolve() == root:
            break
        curr = curr.parent

    # 3. Canonical resolution
    resolved_work = p_raw.resolve()

    # 4. Containment within managed root
    try:
        rel = resolved_work.relative_to(root)
    except ValueError:
        raise PackageCleanupSecurityError(
            f"Path traversal rejected: '{resolved_work}' is outside managed Studio root '{root}'."
        )

    # 5. Cannot be the managed root itself (must have sub-parts)
    if not rel.parts:
        raise PackageCleanupSecurityError(
            f"Containment violation: attempted to delete the entire managed root '{root}'."
        )

    # 6. Critical project protection
    proj_resolved = PROJECT_ROOT.resolve()
    try:
        if resolved_work.relative_to(proj_resolved):
            raise PackageCleanupSecurityError(
                f"Containment violation: work_dir '{resolved_work}' is inside the project repository."
            )
    except ValueError:
        pass  # Safe: not inside project root

    if proj_resolved == resolved_work:
        raise PackageCleanupSecurityError("Containment violation: work_dir matches project root.")

    # 7. Studio Fonts protection
    fonts_resolved = FONTS_DIR.resolve()
    if resolved_work == fonts_resolved:
        raise PackageCleanupSecurityError("Containment violation: work_dir matches studio fonts directory.")
    try:
        fonts_resolved.relative_to(resolved_work)
        raise PackageCleanupSecurityError("Containment violation: work_dir contains studio fonts directory.")
    except ValueError:
        pass

    # 8. Episode-specific source and subtitle protection
    if episode:
        src_path_str = episode.get("source_path")
        if src_path_str:
            src_path = Path(src_path_str).resolve()
            if resolved_work == src_path:
                raise PackageCleanupSecurityError("Containment violation: work_dir matches source video file.")
            try:
                src_path.relative_to(resolved_work)
                raise PackageCleanupSecurityError(
                    f"Containment violation: source video '{src_path}' is inside work_dir."
                )
            except ValueError:
                pass

            # Source season directory protection
            src_dir = src_path.parent
            if resolved_work == src_dir:
                raise PackageCleanupSecurityError("Containment violation: work_dir matches season source folder.")
            try:
                src_dir.relative_to(resolved_work)
                raise PackageCleanupSecurityError(
                    f"Containment violation: season folder '{src_dir}' is inside work_dir."
                )
            except ValueError:
                pass

        sub_path_str = episode.get("subtitle_path")
        if sub_path_str:
            sub_path = Path(sub_path_str).resolve()
            if resolved_work == sub_path:
                raise PackageCleanupSecurityError("Containment violation: work_dir matches external subtitle file.")
            try:
                sub_path.relative_to(resolved_work)
                raise PackageCleanupSecurityError(
                    f"Containment violation: subtitle file '{sub_path}' is inside work_dir."
                )
            except ValueError:
                pass

    return resolved_work


def calculate_directory_size(dir_path: Path) -> int:
    """Calculates total size in bytes of all regular files in a directory."""
    if not dir_path.exists():
        return 0

    total = 0
    for root, dirs, files in os.walk(dir_path):
        for f in files:
            fp = Path(root) / f
            try:
                if not fp.is_symlink():
                    total += fp.stat().st_size
            except OSError:
                pass
    return total


def clean_package_directory(
    work_dir: Any,
    episode: Dict[str, Any],
    is_active: bool = False,
    managed_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Safely cleans generated package directory:
    - Verifies episode state == READY and server_status == READY.
    - Verifies path containment and symlink safety.
    - Calculates freed bytes before deletion.
    - Recursively removes generated package files.
    - Idempotent: returns safely if already cleaned or directory does not exist.
    """
    # 1. State Verification
    state = episode.get("state")
    server_status = episode.get("server_status")

    if state != EpisodeState.READY.value:
        raise PackageCleanupStateError(
            f"لا يمكن حذف الحزمة: حالة الحلقة هي '{state}'، ويجب أن تكون 'READY'."
        )

    if server_status != "READY":
        raise PackageCleanupStateError(
            f"لا يمكن حذف الحزمة: حالة السيرفر هي '{server_status}'، ويجب أن تكون 'READY'."
        )

    if is_active:
        raise PackageCleanupStateError(
            "لا يمكن حذف الحزمة: الحلقة قيد المعالجة أو الرفع حالياً."
        )

    # 2. Strict Path Validation
    resolved_work = validate_cleanup_path(
        work_dir=work_dir,
        episode=episode,
        managed_root=managed_root,
    )

    root = (managed_root or TEMP_DIR).resolve()

    # 3. Check for symlink escapes within the directory tree
    if resolved_work.exists():
        for dirpath, dirnames, filenames in os.walk(resolved_work):
            for item in dirnames + filenames:
                item_p = Path(dirpath) / item
                if item_p.is_symlink():
                    target = item_p.resolve()
                    try:
                        if not target.is_relative_to(root) or target == root:
                            raise PackageCleanupSecurityError(
                                f"Symlink escape rejected: file '{item_p}' links outside managed root to '{target}'."
                            )
                    except AttributeError:
                        try:
                            target.relative_to(root)
                        except ValueError:
                            raise PackageCleanupSecurityError(
                                f"Symlink escape rejected: file '{item_p}' links outside managed root to '{target}'."
                            )

    # 4. Check if directory exists
    if not resolved_work.exists():
        return {
            "freed_bytes": 0,
            "already_cleaned": True,
            "path": str(resolved_work),
        }

    # 5. Measure freed bytes
    freed_bytes = calculate_directory_size(resolved_work)

    # 6. Delete directory safely
    def _onerror_readonly(func, path, exc_info):
        try:
            os.chmod(path, 0o777)
            func(path)
        except Exception:
            pass

    shutil.rmtree(resolved_work, onerror=_onerror_readonly)

    return {
        "freed_bytes": freed_bytes,
        "already_cleaned": False,
        "path": str(resolved_work),
    }
