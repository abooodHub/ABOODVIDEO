"""Safe, no-transcode import of checksummed HLS or browser-targeted MP4 packages.

Package format (zip): manifest.json with format=abv-direct-import-v1,
media_type=hls|mp4, title, entrypoint and an exact files[] list containing
path, size and sha256 for every payload file. No client-supplied storage paths.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from app.core.config import settings


class DirectImportError(ValueError):
    pass


_SHARE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{22}$")


def validate_share_id(value: Any) -> str | None:
    """Accept the token_urlsafe(16) format used by existing share links."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _SHARE_ID_PATTERN.fullmatch(value):
        raise ValueError("share_id must be a 22-character URL-safe share identifier")
    return value


@dataclass(frozen=True)
class ImportInspection:
    media_type: str
    title: str
    entrypoint: str
    size_bytes: int
    sha256: str
    duration: float
    width: int
    height: int
    video_codec: str
    audio_codec: str | None
    variants: tuple[dict[str, Any], ...]


_SAFE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_RESOLUTION = re.compile(r"(?:^|,)RESOLUTION=(\d+)x(\d+)(?:,|$)")
_BANDWIDTH = re.compile(r"(?:^|,)BANDWIDTH=(\d+)(?:,|$)")


def _relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not _SAFE_PATH.fullmatch(value) or "\\" in value:
        raise DirectImportError("unsafe package path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise DirectImportError("unsafe package path")
    return path


def _digest_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _read_manifest(package: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest_path = package / "manifest.json"
    if not manifest_path.is_file() or manifest_path.stat().st_size > 1024 * 1024:
        raise DirectImportError("manifest.json missing or too large")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DirectImportError("invalid manifest JSON") from exc
    if manifest.get("format") != "abv-direct-import-v1":
        raise DirectImportError("unsupported direct-import manifest")
    if manifest.get("media_type") not in ("hls", "mp4"):
        raise DirectImportError("media_type must be hls or mp4")
    title = manifest.get("title")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 255:
        raise DirectImportError("title is required and limited to 255 characters")
    entrypoint = str(_relative_path(manifest.get("entrypoint", "")))
    records: dict[str, dict[str, Any]] = {}
    files = manifest.get("files")
    if not isinstance(files, list) or not files or len(files) > 2000:
        raise DirectImportError("files must contain 1..2000 checked assets")
    for record in files:
        if not isinstance(record, dict):
            raise DirectImportError("invalid file record")
        rel = str(_relative_path(record.get("path", "")))
        size, sha = record.get("size"), record.get("sha256")
        if not isinstance(size, int) or size < 0 or not isinstance(sha, str) or not _SHA256.fullmatch(sha):
            raise DirectImportError("file record requires nonnegative size and SHA-256")
        if rel.casefold() in (p.casefold() for p in records):
            raise DirectImportError("duplicate case-insensitive package path")
        records[rel] = {"size": size, "sha256": sha}
    if entrypoint not in records:
        raise DirectImportError("entrypoint is not listed in manifest files")
    return manifest, records


def extract_and_verify(zip_path: Path, destination: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Extract one bounded archive into a fresh directory and verify every byte."""
    if destination.exists():
        raise DirectImportError("destination must be a new directory")
    manifest: dict[str, Any] | None = None
    records: dict[str, dict[str, Any]] | None = None
    destination.mkdir(parents=True)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            infos = archive.infolist()
            if len(infos) > 2001:
                raise DirectImportError("too many archive entries")
            seen: set[str] = set()
            payload_total = 0
            for info in infos:
                raw_name = info.filename.rstrip("/") if info.is_dir() else info.filename
                if not raw_name:
                    continue
                rel = str(_relative_path(raw_name))
                folded = rel.casefold()
                if folded in seen:
                    raise DirectImportError("duplicate archive path")
                seen.add(folded)
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise DirectImportError("symbolic links are not allowed")
                if info.is_dir():
                    (destination / Path(*PurePosixPath(rel).parts)).mkdir(parents=True, exist_ok=True)
                    continue
                payload_total += info.file_size
                if payload_total > settings.DIRECT_IMPORT_MAX_BYTES:
                    raise DirectImportError("archive payload exceeds direct-import byte limit")
                if info.file_size > settings.DIRECT_IMPORT_MAX_BYTES or (info.file_size > 1024 and info.compress_size == 0):
                    raise DirectImportError("archive member size or compression ratio rejected")
                if info.compress_size and info.file_size / info.compress_size > 150:
                    raise DirectImportError("archive compression ratio rejected")
                target = destination / Path(*PurePosixPath(rel).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                written = 0
                with archive.open(info, "r") as source, target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        if written > info.file_size or written > settings.DIRECT_IMPORT_MAX_BYTES:
                            raise DirectImportError("archive member expanded beyond declared size")
                        digest.update(chunk)
                        output.write(chunk)
                if written != info.file_size:
                    raise DirectImportError("archive member size mismatch")
                if rel == "manifest.json":
                    if written > 1024 * 1024:
                        raise DirectImportError("manifest.json too large")
                    try:
                        manifest = json.loads(target.read_text(encoding="utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise DirectImportError("invalid manifest JSON") from exc
                else:
                    if records is not None:
                        expected = records.get(rel)
                        if not expected or expected["size"] != written or expected["sha256"] != digest.hexdigest():
                            raise DirectImportError(f"asset checksum/size mismatch: {rel}")
            if manifest is None:
                raise DirectImportError("manifest.json missing")
            manifest, records = _read_manifest(destination)
            actual = {p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()} - {"manifest.json"}
            if actual != set(records):
                raise DirectImportError("archive files do not exactly match manifest files")
            for rel, expected in records.items():
                size, digest = _digest_file(destination / Path(*PurePosixPath(rel).parts))
                if size != expected["size"] or digest != expected["sha256"]:
                    raise DirectImportError(f"asset checksum/size mismatch: {rel}")
            return manifest, records
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _playlist_refs_and_variants(text: str, parent: PurePosixPath) -> tuple[list[str], list[dict[str, Any]], float, int]:
    refs: list[str] = []
    variants: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    duration = 0.0
    segments = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = line.partition(":")[2]
            res, bw = _RESOLUTION.search(attrs), _BANDWIDTH.search(attrs)
            if not res or not bw:
                raise DirectImportError("HLS variants require BANDWIDTH and RESOLUTION metadata")
            codecs = re.search(r'(?:^|,)CODECS="([A-Za-z0-9.,_-]{1,128})"', attrs)
            pending = {"width": int(res.group(1)), "height": int(res.group(2)), "bandwidth": int(bw.group(1)), "codecs": codecs.group(1) if codecs else ""}
            continue
        if line.startswith("#EXTINF:"):
            try:
                duration += float(line.partition(":")[2].partition(",")[0])
            except ValueError as exc:
                raise DirectImportError("invalid HLS EXTINF duration") from exc
            continue
        if line.startswith("#"):
            for uri in re.findall(r'URI="([^"]+)"', line):
                refs.append(_resolve_playlist_ref(uri, parent))
            continue
        rel = _resolve_playlist_ref(line, parent)
        refs.append(rel)
        if pending is not None:
            variants.append({**pending, "playlist": rel})
            pending = None
        elif line.lower().endswith(".m3u8"):
            variants.append({"playlist": rel, "width": 0, "height": 0, "bandwidth": 0, "codecs": ""})
        else:
            segments += 1
    if pending is not None:
        raise DirectImportError("HLS master is missing a variant playlist URI")
    return refs, variants, duration, segments


def _resolve_playlist_ref(uri: str, parent: PurePosixPath) -> str:
    parsed = urlsplit(uri)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or not uri or uri.startswith("/"):
        raise DirectImportError("HLS references must be relative local files")
    path = parent
    for part in PurePosixPath(parsed.path).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not path.parts:
                raise DirectImportError("HLS reference escapes package root")
            path = path.parent
        else:
            if not _SAFE_PATH.fullmatch(part) or "/" in part:
                raise DirectImportError("unsafe HLS reference")
            path = path / part
    return str(path)


def inspect_hls(root: Path, manifest: dict[str, Any], records: dict[str, dict[str, Any]]) -> ImportInspection:
    entry = str(_relative_path(manifest["entrypoint"]))
    if not entry.lower().endswith(".m3u8"):
        raise DirectImportError("HLS entrypoint must be an m3u8 playlist")
    master = (root / Path(*PurePosixPath(entry).parts)).read_text(encoding="utf-8-sig")
    refs, variants, _, _ = _playlist_refs_and_variants(master, PurePosixPath(entry).parent)
    if not variants:
        raise DirectImportError("HLS master must reference at least one rendition")
    all_refs = set(refs)
    total_duration = 0.0
    total_segments = 0
    queue = [rel for rel in refs if rel.lower().endswith(".m3u8")]
    seen_playlists = {entry}
    while queue:
        rel = queue.pop(0)
        if rel in seen_playlists:
            continue
        seen_playlists.add(rel)
        if len(seen_playlists) > 100 or rel not in records:
            raise DirectImportError("HLS playlist graph is too large or references an unlisted playlist")
        playlist_text = (root / Path(*PurePosixPath(rel).parts)).read_text(encoding="utf-8-sig")
        if "#EXTM3U" not in playlist_text[:100]:
            raise DirectImportError("invalid HLS playlist")
        child_refs, child_variants, duration, segments = _playlist_refs_and_variants(playlist_text, PurePosixPath(rel).parent)
        all_refs.update(child_refs)
        queue.extend(ref for ref in child_refs if ref.lower().endswith(".m3u8"))
        if child_variants:
            queue.extend(item["playlist"] for item in child_variants if item["playlist"].lower().endswith(".m3u8"))
        if rel != entry:
            if segments < 1 or duration <= 0 or duration > settings.MAX_VIDEO_DURATION_SECONDS:
                if not child_variants:
                    raise DirectImportError("HLS media playlist requires finite duration and at least one segment")
            total_duration = max(total_duration, duration)
            total_segments += segments
    if total_segments > settings.MAX_SEGMENTS:
        raise DirectImportError("HLS segment count exceeds configured limit")
    if not all_refs.issubset(records):
        raise DirectImportError("HLS playlist references an unlisted asset")
    if not all((root / Path(*PurePosixPath(rel).parts)).is_file() for rel in all_refs):
        raise DirectImportError("HLS playlist references a missing asset")
    if total_duration <= 0:
        raise DirectImportError("HLS duration could not be determined")
    if (all_refs | {entry}) != set(records):
        raise DirectImportError("HLS package contains unreferenced media assets")
    size = sum(r["size"] for r in records.values())
    aggregate = hashlib.sha256("".join(f"{p}:{records[p]['sha256']}\n" for p in sorted(records)).encode()).hexdigest()
    first = variants[0]
    codec = first.get("codecs", "")
    return ImportInspection("hls", manifest["title"].strip(), entry, size, aggregate, total_duration, first["width"], first["height"], "h264" if "avc1" in codec.lower() else "unknown", "aac" if "mp4a" in codec.lower() else None, tuple(variants))


def inspect_mp4(root: Path, manifest: dict[str, Any], records: dict[str, dict[str, Any]]) -> ImportInspection:
    entry = str(_relative_path(manifest["entrypoint"]))
    if Path(entry).suffix.lower() != ".mp4":
        raise DirectImportError("direct MP4 import accepts .mp4 files only; MKV is not claimed as browser-compatible")
    if len(records) != 1:
        raise DirectImportError("MP4 package must contain exactly one media file")
    path = root / Path(*PurePosixPath(entry).parts)
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise DirectImportError("ffprobe is required for codec/container validation; no transcoding will be attempted")
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=format_name,duration:stream=codec_type,codec_name,width,height,bit_rate", "-of", "json", str(path)],
            check=True, capture_output=True, text=True, timeout=15,
        )
        probe = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise DirectImportError("MP4 metadata probe failed") from exc
    fmt = (probe.get("format", {}).get("format_name") or "").split(",")
    if "mp4" not in fmt and "mov" not in fmt:
        raise DirectImportError("container is not MP4/ISO-BMFF")
    videos = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
    audios = [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]
    if len(videos) != 1 or len(audios) > 1 or videos[0].get("codec_name") != "h264" or (audios and audios[0].get("codec_name") != "aac"):
        raise DirectImportError("browser target supports one H.264 video stream and zero or one AAC audio stream; transcode is disabled")
    video = videos[0]
    width, height = int(video.get("width", 0)), int(video.get("height", 0))
    try:
        duration = float(probe.get("format", {}).get("duration", "0"))
    except (TypeError, ValueError) as exc:
        raise DirectImportError("MP4 duration is invalid") from exc
    if not math.isfinite(duration) or duration <= 0 or duration > settings.MAX_VIDEO_DURATION_SECONDS:
        raise DirectImportError("MP4 duration is outside configured limits")
    if not (0 < width <= settings.MAX_VIDEO_WIDTH and 0 < height <= settings.MAX_VIDEO_HEIGHT):
        raise DirectImportError("MP4 resolution is outside configured limits")
    record = records[entry]
    return ImportInspection("mp4", manifest["title"].strip(), entry, record["size"], record["sha256"], duration, width, height, "h264", "aac" if audios else None, ())


def inspect_package(root: Path) -> tuple[ImportInspection, dict[str, Any], dict[str, dict[str, Any]]]:
    manifest, records = _read_manifest(root)
    if sum(item["size"] for item in records.values()) > settings.DIRECT_IMPORT_MAX_BYTES:
        raise DirectImportError("direct-import byte limit exceeded")
    if manifest["media_type"] == "hls":
        inspection = inspect_hls(root, manifest, records)
    else:
        inspection = inspect_mp4(root, manifest, records)
    return inspection, manifest, records


def publish_import(source: Path, target: Path) -> None:
    """Publish a fully verified fresh tree atomically on the same volume."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise DirectImportError("target generation already exists")
    shutil.copytree(source, target, symlinks=False, ignore=shutil.ignore_patterns("manifest.json"))
    for path in target.rglob("*"):
        if path.is_file():
            path.chmod(0o640)
    for path in sorted((p for p in target.rglob("*") if p.is_dir()), reverse=True):
        path.chmod(0o750)
    target.chmod(0o750)
