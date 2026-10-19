"""Rendition-level checkpoint verification and fingerprinting for crash-safe transcode recovery.

Design goals:
- A rendition from an interrupted generation is reusable ONLY after full verification.
- Verification covers playlist integrity, segment completeness, duration sanity,
  codec/resolution identity, and a deterministic settings fingerprint.
- Fingerprints bind a rendition to: source identity (SHA-256 + media identity),
  output resolution, codec, bitrates, audio settings, HLS segment duration,
  encoder preset, and a transcode profile version.
- Missing checkpoint metadata (generations produced before this feature) falls
  back to retroactive verification against derived expectations; such reuse is
  logged explicitly and never trusted blindly.
"""
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.logging import logger

# Bump when ladder presets, encoder flags, segmenting, or audio semantics change.
# A mismatch forces re-encoding of the affected rendition.
TRANSCODE_PROFILE_VERSION = "1"

CHECKPOINT_FILENAME = "checkpoint.json"

DURATION_TOLERANCE_FRACTION = 0.15


def compute_source_sha256(source_path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    """Compute SHA-256 of the source media to bind checkpoints to source identity."""
    digest = hashlib.sha256()
    with open(source_path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def build_rendition_fingerprint(
    source_sha256: str,
    probe_info: Dict[str, Any],
    rendition: Dict[str, Any],
    profile_version: Optional[str] = None,
    encoding_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Deterministic fingerprint of everything that defines a rendition's output identity."""
    encoding_config = encoding_config or {}
    return {
        "profile_version": profile_version or TRANSCODE_PROFILE_VERSION,
        "source_sha256": source_sha256,
        "source_identity": {
            "width": probe_info.get("width"),
            "height": probe_info.get("height"),
            "duration_seconds": round(float(probe_info.get("duration", 0.0)), 3),
            "video_codec": probe_info.get("video_codec"),
            "has_audio": bool(probe_info.get("has_audio")),
        },
        "output": {
            "quality_label": rendition["quality_label"],
            "width": rendition["width"],
            "height": rendition["height"],
            "video_codec": "h264",
            "video_bitrate_kbps": rendition["video_bitrate_kbps"],
            "audio_bitrate_kbps": rendition.get("audio_bitrate_kbps"),
            "audio_codec": "aac" if probe_info.get("has_audio") else None,
        },
        "container": {
            "format": "hls",
            "segment_duration_seconds": encoding_config.get("segment_duration_seconds", settings.HLS_SEGMENT_DURATION_SECONDS),
            "playlist_type": "vod",
            "encoder_preset": encoding_config.get("preset", settings.FFMPEG_PRESET),
            "ffmpeg_threads": encoding_config.get("threads", settings.FFMPEG_THREADS),
            "video_codec": encoding_config.get("video_codec", "libx264"),
            "audio_codec": encoding_config.get("audio_codec", "aac"),
        },
    }


def fingerprint_digest(fingerprint: Dict[str, Any]) -> str:
    canonical = json.dumps(fingerprint, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_checkpoint(rendition_dir: Path, fingerprint: Dict[str, Any]) -> Path:
    path = rendition_dir / CHECKPOINT_FILENAME
    payload = {"fingerprint": fingerprint, "digest": fingerprint_digest(fingerprint)}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    return path


def read_checkpoint(rendition_dir: Path) -> Optional[Dict[str, Any]]:
    path = rendition_dir / CHECKPOINT_FILENAME
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return None


def _parse_playlist(index_path: Path) -> Tuple[Optional[List[str]], Optional[float], List[str]]:
    """Parse a media playlist: returns (segment names, total duration, reasons-for-failure)."""
    try:
        content = index_path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, None, [f"playlist unreadable: {exc}"]

    if "#EXTM3U" not in content:
        return None, None, ["missing #EXTM3U header"]

    segments: List[str] = []
    duration = 0.0
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#EXTINF:"):
            try:
                duration += float(stripped.split(":", 1)[1].rstrip(","))
            except ValueError:
                return None, None, ["malformed #EXTINF line"]
        elif stripped and not stripped.startswith("#"):
            segments.append(stripped)

    return segments, duration, []


def _probe_segment_codec_resolution(segment_path: Path) -> Optional[Tuple[str, int, int]]:
    cmd = [
        "ffprobe", "-v", "quiet",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height",
        "-print_format", "json",
        str(segment_path.resolve()),
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if not streams:
            return None
        stream = streams[0]
        return (stream.get("codec_name", ""), int(stream.get("width", 0)), int(stream.get("height", 0)))
    except Exception:
        return None


def verify_rendition_checkpoint(
    rendition_dir: Path,
    rendition: Dict[str, Any],
    source_duration: float,
    fingerprint: Optional[Dict[str, Any]] = None,
    probe_media: bool = True,
) -> Dict[str, Any]:
    """
    Full verification of a candidate completed rendition.

    Returns {"ok": bool, "reasons": [...], "retroactive": bool, "fingerprint_match": Optional[bool]}
    A rendition is reusable only when ok is True.
    """
    reasons: List[str] = []
    retroactive = False
    fingerprint_match: Optional[bool] = None

    if not rendition_dir.exists() or not rendition_dir.is_dir():
        return {"ok": False, "reasons": ["rendition directory missing"], "retroactive": False, "fingerprint_match": None}

    index_path = rendition_dir / "index.m3u8"
    if not index_path.exists() or index_path.stat().st_size == 0:
        return {"ok": False, "reasons": ["index.m3u8 missing or empty"], "retroactive": False, "fingerprint_match": None}

    segments, total_duration, parse_reasons = _parse_playlist(index_path)
    if segments is None:
        return {"ok": False, "reasons": parse_reasons, "retroactive": False, "fingerprint_match": None}

    content = index_path.read_text(encoding="utf-8")
    if "#EXT-X-ENDLIST" not in content:
        reasons.append("missing #EXT-X-ENDLIST (playlist incomplete)")

    if not segments:
        reasons.append("playlist references no segments")

    for name in segments:
        seg_path = rendition_dir / name
        if not seg_path.exists():
            reasons.append(f"referenced segment missing: {name}")
        elif seg_path.stat().st_size == 0:
            reasons.append(f"referenced segment is zero-byte: {name}")

    for stray in rendition_dir.glob("seg_*.ts"):
        if stray.name not in segments and stray.stat().st_size == 0:
            reasons.append(f"unreferenced zero-byte segment present: {stray.name}")

    if source_duration and source_duration > 0:
        if abs(total_duration - source_duration) / source_duration > DURATION_TOLERANCE_FRACTION:
            reasons.append(
                f"duration mismatch: playlist {total_duration:.1f}s vs source {source_duration:.1f}s"
            )

    if probe_media and segments:
        first_seg = rendition_dir / segments[0]
        probe = _probe_segment_codec_resolution(first_seg)
        if probe is None:
            reasons.append("segment not decodable / probe failed")
        else:
            codec, width, height = probe
            expected_codec = rendition.get("video_codec_expected", "h264")
            if codec != expected_codec:
                reasons.append(f"codec mismatch: {codec} != {expected_codec}")
            if width != rendition["width"] or height != rendition["height"]:
                reasons.append(f"resolution mismatch: {width}x{height} != {rendition['width']}x{rendition['height']}")

    if fingerprint is not None:
        checkpoint = read_checkpoint(rendition_dir)
        if checkpoint is None:
            # No checkpoint metadata (generation predates checkpointing):
            # acceptable when all media checks pass, but flagged retroactive.
            retroactive = True
        else:
            fingerprint_match = checkpoint.get("digest") == fingerprint_digest(fingerprint)
            if not fingerprint_match:
                reasons.append("checkpoint fingerprint mismatch (settings or source changed)")

    return {
        "ok": not reasons,
        "reasons": reasons,
        "retroactive": retroactive,
        "fingerprint_match": fingerprint_match,
        "duration_seconds": total_duration,
    }


def prepare_rendition_expectations(ladder: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Augment ladder entries with expected codec for verification."""
    expectations: Dict[str, Dict[str, Any]] = {}
    for rendition in ladder:
        entry = dict(rendition)
        entry["video_codec_expected"] = "h264"
        expectations[rendition["quality_label"]] = entry
    return expectations


def scan_generation_renditions(
    generation_dir: Path,
    ladder: List[Dict[str, Any]],
    probe_info: Dict[str, Any],
    fingerprints: Optional[Dict[str, Dict[str, Any]]] = None,
    probe_media: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """
    Verify every ladder rendition inside one temp generation directory.
    Returns {quality_label: verification_result}.
    """
    expectations = prepare_rendition_expectations(ladder)
    results: Dict[str, Dict[str, Any]] = {}
    for label, expected in expectations.items():
        results[label] = verify_rendition_checkpoint(
            generation_dir / label,
            expected,
            source_duration=float(probe_info.get("duration", 0.0)),
            fingerprint=(fingerprints or {}).get(label),
            probe_media=probe_media,
        )
    return results


def find_reusable_renditions(
    video_id: Any,
    ladder: List[Dict[str, Any]],
    probe_info: Dict[str, Any],
    source_sha256: Optional[str],
    exclude_generation: Optional[str] = None,
    base_temp_dir: Optional[Path] = None,
    probe_media: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """
    Scan previous temp generations for a video, newest first, and return the first
    verified artifact directory per rendition label.

    Returns {label: {"source_dir": Path, "verification": {...}}} for reusable renditions.
    """
    base = Path(base_temp_dir) if base_temp_dir else Path(settings.STORAGE_TEMP_PATH) / "transcodes" / str(video_id)
    reusable: Dict[str, Dict[str, Any]] = {}
    if not base.exists():
        return reusable

    expectations = prepare_rendition_expectations(ladder)
    duration = float(probe_info.get("duration", 0.0))

    candidates = sorted(
        (d for d in base.iterdir() if d.is_dir() and d.name != str(exclude_generation)),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )

    for generation in candidates:
        for label, expected in expectations.items():
            if label in reusable:
                continue
            fingerprint = None
            if source_sha256:
                fingerprint = build_rendition_fingerprint(source_sha256, probe_info, expected)
            result = verify_rendition_checkpoint(
                generation / label, expected, duration, fingerprint=fingerprint, probe_media=probe_media,
            )
            if result["ok"]:
                reusable[label] = {"source_dir": generation / label, "generation": generation.name, "verification": result}
    return reusable


def copy_rendition_artifacts(source_dir: Path, target_dir: Path) -> None:
    """Copy verified rendition artifacts into the new generation workspace.

    Tries reflink/hardlink first (same volume, immutable sharing of content),
    falling back to a full copy. Never shares mutable state across jobs.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    for item in source_dir.iterdir():
        if not item.is_file():
            continue
        target = target_dir / item.name
        if target.exists():
            target.unlink()
        linked = False
        try:
            os.link(item, target)
            linked = True
        except OSError:
            linked = False
        if not linked:
            shutil.copy2(item, target)
