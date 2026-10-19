"""Package Assembly and Client-Side Verification Engine for ABOODVIDEO Studio."""
import os
import re
import json
import hashlib
from pathlib import Path
from studio import __version__
from typing import Dict, Any, List, Optional, Tuple

from studio.core.subtitles import compute_file_sha256


MEDIA_TYPES = {
    ".m3u8": "application/x-mpegURL",
    ".ts": "video/MP2T",
    ".vtt": "text/vtt",
    ".ass": "text/x-ssa",
    ".ssa": "text/x-ssa",
    ".srt": "application/x-subrip",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".ttc": "font/collection",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def build_and_verify_package(
    package_dir: Path,
    title: str,
    duration_seconds: float,
    renditions: List[Dict[str, Any]],
    subtitles: Optional[List[Dict[str, Any]]] = None,
    fonts: Optional[List[Dict[str, Any]]] = None,
    description: Optional[str] = None,
    source_checksum: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Assembles, verifies, and calculates exact SHA-256 checksums for all package files.
    Generates a compliant manifest.json adhering to ABOODVIDEO PackageManifest schema.
    Supports True ASS softsubs and packaged font assets.
    """
    package_dir = Path(package_dir).resolve()
    if not package_dir.exists():
        raise FileNotFoundError(f"مجلد الحزمة غير موجود: {package_dir}")

    declared_files: List[Dict[str, Any]] = []
    file_rel_paths_set = set()

    # 1. Verify and Collect Renditions & Playlists
    for r in renditions:
        q_label = r["quality_label"]
        pl_rel = r["playlist_path"].replace("\\", "/")
        pl_path = package_dir / pl_rel

        if not pl_path.exists():
            raise ValueError(f"ملف قائمة التشغيل مفقود: {pl_rel}")

        with open(pl_path, "r", encoding="utf-8", errors="ignore") as fp:
            pl_content = fp.read()

        if "#EXTM3U" not in pl_content:
            raise ValueError(f"قائمة التشغيل {pl_rel} غير صالحة (تفتقد #EXTM3U)")
        if "#EXT-X-ENDLIST" not in pl_content:
            raise ValueError(f"قائمة التشغيل {pl_rel} غير مكتملة (تفتقد #EXT-X-ENDLIST)")

        # Collect segment references from playlist
        for line in pl_content.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and line.endswith(".ts"):
                # Safety checks on segment URI: strictly no traversal or schemes
                if any(x in line for x in ["..", "\\", ":", "/", "http", "file"]):
                    raise ValueError(f"مسار مقطع غير آمن داخل {pl_rel}: '{line}'")

                seg_rel = f"{q_label}/{line}"
                seg_path = package_dir / seg_rel
                if not seg_path.exists():
                    raise ValueError(f"المقطع المشار إليه مفقود: {seg_rel}")
                if seg_path.stat().st_size == 0:
                    raise ValueError(f"حجم المقطع فارغ (0 بايت): {seg_rel}")

                if seg_rel not in file_rel_paths_set:
                    file_rel_paths_set.add(seg_rel)
                    declared_files.append({
                        "path": seg_rel,
                        "abs_path": seg_path,
                        "media_type": "video/MP2T",
                    })

        # Add the playlist itself
        if pl_rel not in file_rel_paths_set:
            file_rel_paths_set.add(pl_rel)
            declared_files.append({
                "path": pl_rel,
                "abs_path": pl_path,
                "media_type": "application/x-mpegURL",
            })

    # 2. Collect Subtitles (True ASS / SRT / VTT)
    declared_subtitles = []
    if subtitles:
        for sub in subtitles:
            sub_rel = sub["path"].replace("\\", "/")
            sub_path = package_dir / sub_rel
            if not sub_path.exists():
                raise ValueError(f"ملف الترجمة المعلن مفقود: {sub_rel}")
            if sub_path.stat().st_size == 0:
                raise ValueError(f"ملف الترجمة فارغ: {sub_rel}")

            sub_sha = compute_file_sha256(sub_path)
            ext = sub_path.suffix.lower()
            detected_fmt = sub.get("format") or ext.lstrip(".")

            declared_subtitles.append({
                "language_code": sub.get("language_code", "ar"),
                "language_name": sub.get("language_name", "العربية"),
                "label": sub.get("label", "العربية"),
                "is_default": sub.get("is_default", True),
                "path": sub_rel,
                "sha256": sub_sha,
                "format": detected_fmt,
                "fonts": sub.get("fonts", []),
            })

            if sub_rel not in file_rel_paths_set:
                file_rel_paths_set.add(sub_rel)
                declared_files.append({
                    "path": sub_rel,
                    "abs_path": sub_path,
                    "media_type": MEDIA_TYPES.get(ext, "text/vtt"),
                })

            # Check for any auxiliary files (e.g. preserved original .srt)
            for aux in sub.get("auxiliary_files", []):
                aux_rel = aux["path"].replace("\\", "/")
                aux_path = package_dir / aux_rel
                if aux_path.exists() and aux_rel not in file_rel_paths_set:
                    file_rel_paths_set.add(aux_rel)
                    declared_files.append({
                        "path": aux_rel,
                        "abs_path": aux_path,
                        "media_type": aux.get("media_type") or "application/x-subrip",
                    })

    # 3. Collect Fonts (for True ASS softsubs)
    declared_fonts = []
    if fonts:
        for f_item in fonts:
            f_rel = f_item["path"].replace("\\", "/")
            f_abs = package_dir / f_rel
            if not f_abs.exists() or f_abs.stat().st_size == 0:
                continue

            f_sha = compute_file_sha256(f_abs)
            ext = f_abs.suffix.lower()

            declared_fonts.append({
                "family_name": f_item["family_name"],
                "filename": f_item.get("filename", f_abs.name),
                "path": f_rel,
                "size_bytes": f_abs.stat().st_size,
                "sha256": f_sha,
                "source": f_item.get("source"),
            })

            if f_rel not in file_rel_paths_set:
                file_rel_paths_set.add(f_rel)
                declared_files.append({
                    "path": f_rel,
                    "abs_path": f_abs,
                    "media_type": MEDIA_TYPES.get(ext, "font/ttf"),
                })

    # 4. Collect Poster Thumbnail (if exists)
    poster_path = package_dir / "poster.webp"
    if poster_path.exists() and poster_path.stat().st_size > 0:
        if "poster.webp" not in file_rel_paths_set:
            file_rel_paths_set.add("poster.webp")
            declared_files.append({
                "path": "poster.webp",
                "abs_path": poster_path,
                "media_type": "image/webp",
            })

    # 5. Hash and Measure all files
    final_files_manifest = []
    total_bytes = 0

    for f_entry in declared_files:
        p_abs = f_entry["abs_path"]
        p_rel = f_entry["path"]
        f_size = p_abs.stat().st_size
        f_sha = compute_file_sha256(p_abs)
        total_bytes += f_size

        final_files_manifest.append({
            "path": p_rel,
            "size_bytes": f_size,
            "sha256": f_sha,
            "media_type": f_entry["media_type"],
        })

    # 6. Format Rendition metadata for Manifest
    manifest_renditions = []
    for r in renditions:
        manifest_renditions.append({
            "quality_label": r["quality_label"],
            "width": r["width"],
            "height": r["height"],
            "video_bitrate_bps": r["video_bitrate_bps"],
            "audio_bitrate_bps": r.get("audio_bitrate_bps", 128000),
            "playlist_path": r["playlist_path"].replace("\\", "/"),
            "segment_count": r["segment_count"],
        })

    # 7. Build Complete Manifest Object
    manifest = {
        "manifest_version": "1.0",
        "generator": f"ABOODVIDEO Studio v{__version__}",
        "video": {
            "title": title or "Studio Video",
            "description": description or "",
            "duration_seconds": round(float(duration_seconds), 2),
            "source_checksum": source_checksum,
        },
        "renditions": manifest_renditions,
        "subtitles": declared_subtitles,
        "fonts": declared_fonts,
        "files": final_files_manifest,
    }

    # Save manifest.json to package directory
    manifest_file = package_dir / "manifest.json"
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    return {
        "manifest": manifest,
        "manifest_file": str(manifest_file),
        "total_files": len(final_files_manifest),
        "total_bytes": total_bytes,
        "total_bytes_formatted": f"{total_bytes / (1024 * 1024):.1f} MB",
        "package_dir": str(package_dir),
    }
