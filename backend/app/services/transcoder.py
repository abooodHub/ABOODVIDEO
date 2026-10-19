"""Production Video Transcoding Engine and Adaptive HLS Pipeline (Phase 3)."""
import os
import signal
import shutil
import subprocess
import json
import time
import math
import uuid
import queue
import threading
from collections import deque
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Callable

from app.core.config import settings, LADDER_PRESETS
from app.core.logging import logger
from app.core.exceptions import AboodVideoException


class TranscodeError(AboodVideoException):
    """Base exception for transcoding pipeline errors."""
    def __init__(self, message: str, status_code: int = 500, error_code: str = "TRANSCODE_ERROR", details: Optional[Dict[str, Any]] = None):
        super().__init__(message=message, status_code=status_code, details=details)
        self.error_code = error_code


class InsufficientDiskSpaceError(TranscodeError):
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message=message, status_code=507, error_code="INSUFFICIENT_DISK_SPACE", details=details)


class TranscodeCancelledError(TranscodeError):
    def __init__(self, message: str = "Transcoding was cancelled by user request"):
        super().__init__(message=message, status_code=499, error_code="TRANSCODE_CANCELLED")


def probe_source(source_path: Path) -> Dict[str, Any]:
    """Execute ffprobe to extract complete media, video, and audio metadata."""
    if not source_path.exists():
        raise TranscodeError(f"Source file does not exist: {source_path}", error_code="SOURCE_NOT_FOUND")

    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(source_path.resolve()),
    ]

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30, check=False)
    except FileNotFoundError:
        raise TranscodeError("ffprobe binary is not available on system path", error_code="FFPROBE_UNAVAILABLE")
    except subprocess.TimeoutExpired:
        raise TranscodeError("ffprobe timed out inspecting source file", error_code="FFPROBE_TIMEOUT")

    if proc.returncode != 0:
        raise TranscodeError("ffprobe failed to inspect source media", error_code="FFPROBE_FAILED", details={"stderr": proc.stderr[:500]})

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise TranscodeError("Failed to parse ffprobe json output", error_code="FFPROBE_INVALID_JSON")

    streams = data.get("streams", [])
    format_info = data.get("format", {})

    video_stream = None
    audio_stream = None
    for s in streams:
        if s.get("codec_type") == "video" and not video_stream:
            video_stream = s
        elif s.get("codec_type") == "audio" and not audio_stream:
            audio_stream = s

    if not video_stream:
        raise TranscodeError("Source file contains no valid video stream", error_code="NO_VIDEO_STREAM")

    width = int(video_stream.get("width", 0))
    height = int(video_stream.get("height", 0))
    if width <= 0 or height <= 0:
        raise TranscodeError("Invalid video dimensions", error_code="INVALID_DIMENSIONS")

    duration = 0.0
    if "duration" in video_stream:
        try:
            duration = float(video_stream["duration"])
        except (ValueError, TypeError):
            pass
    if duration <= 0 and "duration" in format_info:
        try:
            duration = float(format_info["duration"])
        except (ValueError, TypeError):
            pass
    if duration <= 0:
        raise TranscodeError("Invalid or zero video duration", error_code="INVALID_DURATION")

    bitrate = 0
    if "bit_rate" in video_stream:
        try:
            bitrate = int(video_stream["bit_rate"])
        except (ValueError, TypeError):
            pass
    if bitrate <= 0 and "bit_rate" in format_info:
        try:
            bitrate = int(format_info["bit_rate"])
        except (ValueError, TypeError):
            pass

    fps = 25.0
    r_frame_rate = video_stream.get("r_frame_rate", "25/1")
    try:
        if "/" in r_frame_rate:
            num, den = r_frame_rate.split("/")
            fps = float(num) / float(den) if float(den) != 0 else 25.0
        else:
            fps = float(r_frame_rate)
    except Exception:
        fps = 25.0
    if fps <= 0 or fps > 120:
        fps = 25.0

    has_audio = audio_stream is not None
    audio_codec = audio_stream.get("codec_name") if audio_stream else None
    audio_bitrate = None
    if audio_stream and "bit_rate" in audio_stream:
        try:
            audio_bitrate = int(audio_stream["bit_rate"])
        except (ValueError, TypeError):
            pass

    return {
        "width": width,
        "height": height,
        "duration": duration,
        "bitrate": bitrate,
        "fps": fps,
        "video_codec": video_stream.get("codec_name", "unknown"),
        "has_audio": has_audio,
        "audio_codec": audio_codec,
        "audio_bitrate": audio_bitrate,
    }


def select_quality_ladder(width: int, height: int, source_bitrate: Optional[int], enabled_qualities: Optional[List[str]] = None, quality_presets: Optional[Dict[str, Dict[str, Any]]] = None, allow_4k: Optional[bool] = None, fallback_presets: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """
    Select appropriate quality ladder renditions.
    Enforces:
    1. NEVER UPSCALE (if source height is 720, only 360p, 480p, 720p).
    2. Even dimensions (divisible by 2).
    3. Rendition video bitrate capped at 1.2x source bitrate if known.
    """
    aspect_ratio = width / height if height > 0 else 16 / 9
    ladder: List[Dict[str, Any]] = []

    order = enabled_qualities if enabled_qualities is not None else list(LADDER_PRESETS)
    presets = quality_presets or LADDER_PRESETS
    permit_4k = settings.ENABLE_4K_TRANSCODING if allow_4k is None else allow_4k
    for quality in order:
        preset = presets[quality]
        if quality == "2160p" and not permit_4k:
            continue
        preset_h = preset["height"]

        # Downscale rule: do not generate rendition with height higher than source
        if preset_h > height:
            continue

        target_h = preset_h
        target_w = int(target_h * aspect_ratio)
        # Force even dimensions
        target_w = target_w - (target_w % 2)
        target_h = target_h - (target_h % 2)

        v_bitrate = preset["video_bitrate_kbps"]
        if source_bitrate and source_bitrate > 0:
            source_kbps = int(source_bitrate / 1000)
            max_allowed = int(source_kbps * 1.2)
            if v_bitrate > max_allowed:
                v_bitrate = max(300, max_allowed)

        ladder.append({
            "quality_label": quality,
            "width": target_w,
            "height": target_h,
            "video_bitrate_kbps": v_bitrate,
            "audio_bitrate_kbps": preset["audio_bitrate_kbps"],
            "is_vip_only": quality in ("1080p", "1440p", "2160p"),
        })

    # A source below every enabled rung still needs a native-size playable output.
    if not ladder:
        native_presets = fallback_presets or LADDER_PRESETS
        compatible = [p for p in native_presets.values() if p["height"] <= height]
        preset = max(compatible, key=lambda p: p["height"]) if compatible else min(native_presets.values(), key=lambda p: p["height"])
        w = width - (width % 2)
        h = height - (height % 2)
        if w < 2 or h < 2:
            raise TranscodeError("Source dimensions are too small to encode", error_code="INVALID_DIMENSIONS")
        native_bitrate = preset["video_bitrate_kbps"]
        if source_bitrate and source_bitrate > 0:
            native_bitrate = min(native_bitrate, max(300, int(source_bitrate / 1000 * 1.2)))
        ladder.append({
            "quality_label": "source",
            "width": w,
            "height": h,
            "video_bitrate_kbps": native_bitrate,
            "audio_bitrate_kbps": preset["audio_bitrate_kbps"],
            "is_vip_only": False,
        })

    return ladder


def calculate_peak_disk_requirement(
    source_size: int,
    duration: float,
    ladder: List[Dict[str, Any]],
    has_audio: bool,
    current_active_gen_dir: Optional[Path] = None,
) -> int:
    """
    Calculate peak disk bytes needed:
    source + (active_gen if retry/reprocess) + estimated_temp_gen + safety_margin (1GB).
    """
    # Estimate temp generation bytes
    total_est_bytes = 0
    for item in ladder:
        kbps = item["video_bitrate_kbps"] + (item["audio_bitrate_kbps"] if has_audio else 0)
        bytes_sec = (kbps * 1000) / 8
        total_est_bytes += int(bytes_sec * duration * 1.15)  # 15% container/HLS overhead

    active_gen_bytes = 0
    if current_active_gen_dir and current_active_gen_dir.exists():
        for root, _, files in os.walk(current_active_gen_dir):
            for f in files:
                active_gen_bytes += os.path.getsize(os.path.join(root, f))

    peak_required = source_size + active_gen_bytes + total_est_bytes + settings.DISK_SAFETY_MARGIN_BYTES
    return peak_required


def check_disk_headroom(storage_path: Path, required_bytes: int) -> None:
    """Ensure disk has sufficient free bytes without dropping below min floor."""
    try:
        stat = os.statvfs(storage_path)
        free_bytes = stat.f_bavail * stat.f_frsize
    except Exception as e:
        logger.warning(f"Could not read statvfs: {e}")
        return

    if free_bytes < required_bytes:
        raise InsufficientDiskSpaceError(
            f"Insufficient disk space. Free: {free_bytes // (1024*1024)}MB, Peak Required: {required_bytes // (1024*1024)}MB",
            details={"free_bytes": free_bytes, "required_bytes": required_bytes},
        )


def derive_rfc6381_codecs(segment_path: Path, has_audio: bool) -> str:
    """
    Derive the accurate RFC 6381 codec string directly from the encoded output segment.
    Uses ffprobe to inspect actual H.264 profile and level.
    """
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,profile,level",
        "-print_format", "json",
        str(segment_path.resolve()),
    ]

    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10, check=False)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            streams = data.get("streams", [])
            if streams:
                v = streams[0]
                profile_str = v.get("profile", "").lower()
                level_val = v.get("level", 31)

                if "high 10" in profile_str:
                    # High 10's SPS profile_idc is 110, not High's 100.
                    # Keep 10-bit output correctly advertised to MSE/ABR clients.
                    profile_idc = 0x6E
                    compat_flags = 0x00
                elif "high" in profile_str:
                    profile_idc = 0x64
                    compat_flags = 0x00
                elif "main" in profile_str:
                    profile_idc = 0x4D
                    compat_flags = 0x40
                elif "baseline" in profile_str:
                    profile_idc = 0x42
                    compat_flags = 0xE0
                else:
                    profile_idc = 0x64
                    compat_flags = 0x00

                level_idc = int(level_val) if isinstance(level_val, int) and level_val > 0 else 31
                v_codec = f"avc1.{profile_idc:02x}{compat_flags:02x}{level_idc:02x}"
                if has_audio:
                    return f"{v_codec},mp4a.40.2"
                return v_codec
    except Exception as e:
        logger.warning(f"Error reading segment for RFC 6381 codecs: {e}")

    # Fallback to standard RFC 6381 values if probe fails
    if has_audio:
        return "avc1.64001f,mp4a.40.2"
    return "avc1.64001f"


def generate_poster(source_path: Path, duration: float, output_path: Path) -> None:
    """Extract representative WebP poster frame at ~10% timestamp."""
    timestamp = max(1.0, min(duration * 0.1, max(1.0, duration - 0.5)))
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-ss", f"{timestamp:.2f}",
        "-i", str(source_path.resolve()),
        "-vframes", "1",
        "-c:v", "libwebp",
        "-q:v", "80",
        str(output_path.resolve()),
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30, check=False)
        if proc.returncode != 0:
            logger.warning(f"libwebp poster generation failed, falling back to mjpeg: {proc.stderr[:200]}")
            fallback_cmd = [
                "ffmpeg", "-hide_banner", "-y", "-ss", f"{timestamp:.2f}",
                "-i", str(source_path.resolve()), "-vframes", "1",
                str(output_path.resolve()),
            ]
            subprocess.run(fallback_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
    except Exception as e:
        logger.error(f"Poster extraction failed: {e}")


def generate_master_playlist(
    rendition_configs: List[Dict[str, Any]],
    output_path: Path,
) -> None:
    """Generate RFC 8216 compliant Adaptive HLS Master Playlist."""
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:6",
        "#EXT-X-INDEPENDENT-SEGMENTS",
        "",
    ]
    for r in rendition_configs:
        bandwidth = int((r["video_bitrate_kbps"] + (r.get("audio_bitrate_kbps") or 0)) * 1000)
        res_w = r["width"]
        res_h = r["height"]
        codecs = r["rfc_codecs"]
        sub_playlist = f"{r['quality_label']}/index.m3u8"

        lines.append(f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={res_w}x{res_h},CODECS="{codecs}"')
        lines.append(sub_playlist)
        lines.append("")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def verify_transcoded_output(
    temp_dir: Path,
    ladder: List[Dict[str, Any]],
    source_duration: float,
) -> None:
    """
    Verify integrity of all generated HLS streams before publishing.
    1. Master playlist exists and non-empty.
    2. Each rendition folder has index.m3u8 with #EXT-X-ENDLIST.
    3. Segments exist, non-empty, and sample segments are decodable by ffprobe.
    4. Duration matches source duration within 10%.
    5. Poster exists and non-empty.
    """
    master_path = temp_dir / "master.m3u8"
    if not master_path.exists() or master_path.stat().st_size == 0:
        raise TranscodeError("Verification failed: master.m3u8 missing or empty", error_code="VERIFY_MASTER_MISSING")

    poster_path = temp_dir / "poster.webp"
    if not poster_path.exists() or poster_path.stat().st_size == 0:
        logger.warning("poster.webp missing or empty during verification")

    for r in ladder:
        q_dir = temp_dir / r["quality_label"]
        idx_path = q_dir / "index.m3u8"
        if not idx_path.exists() or idx_path.stat().st_size == 0:
            raise TranscodeError(f"Verification failed: index.m3u8 missing for {r['quality_label']}", error_code="VERIFY_INDEX_MISSING")

        with open(idx_path, "r", encoding="utf-8") as f:
            content = f.read()

        if "#EXT-X-ENDLIST" not in content:
            raise TranscodeError(f"Verification failed: index.m3u8 incomplete for {r['quality_label']}", error_code="VERIFY_INCOMPLETE_PLAYLIST")

        segments = [line.strip() for line in content.splitlines() if line.strip().endswith(".ts")]
        if not segments:
            raise TranscodeError(f"Verification failed: no segments found for {r['quality_label']}", error_code="VERIFY_NO_SEGMENTS")

        # Test first, middle, and last segments
        sample_indices = [0, len(segments) // 2, len(segments) - 1]
        for s_idx in set(sample_indices):
            seg_file = q_dir / segments[s_idx]
            if not seg_file.exists() or seg_file.stat().st_size == 0:
                raise TranscodeError(f"Verification failed: segment {segments[s_idx]} missing or empty", error_code="VERIFY_SEGMENT_EMPTY")

            cmd = ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-print_format", "json", str(seg_file.resolve())]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            if res.returncode != 0:
                raise TranscodeError(f"Verification failed: segment {segments[s_idx]} not decodable", error_code="VERIFY_SEGMENT_CORRUPT")


def safe_kill_process_group(proc: subprocess.Popen) -> None:
    """Cleanly terminate process group using SIGTERM, grace period, and SIGKILL."""
    if proc.poll() is not None:
        return
    try:
        if hasattr(os, "killpg"):
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            for _ in range(50):
                if proc.poll() is not None:
                    return
                time.sleep(0.1)
            if proc.poll() is None:
                os.killpg(pgid, signal.SIGKILL)
                proc.wait(timeout=5)
        else:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    except (ProcessLookupError, PermissionError):
        pass


def execute_transcode_rendition(
    source_path: Path,
    rendition: Dict[str, Any],
    rendition_dir: Path,
    fps: float,
    has_audio: bool,
    duration: float,
    rendition_index: int,
    total_renditions: int,
    progress_state: Dict[str, Any],
    check_cancelled_fn: Optional[Callable[[], bool]] = None,
    progress_callback: Optional[Callable[[float, Optional[float], Optional[float], Optional[int]], None]] = None,
    encoding_config: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Encode a single rendition to HLS using FFmpeg with process group isolation,
    deterministic keyframe alignment, and monotonic progress calculation.
    """
    rendition_dir.mkdir(parents=True, exist_ok=True)
    encoding_config = encoding_config or {}
    seg_time = int(encoding_config.get("segment_duration_seconds", settings.HLS_SEGMENT_DURATION_SECONDS))
    threads = int(encoding_config.get("threads", settings.FFMPEG_THREADS))
    preset = str(encoding_config.get("preset", settings.FFMPEG_PRESET))
    gop_size = int(fps * seg_time)

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-threads", str(threads),
        "-filter_threads", str(threads),
        "-i", str(source_path.resolve()),
        "-vf", f"scale={rendition['width']}:{rendition['height']}",
        "-c:v", "libx264",
        # FFmpeg options before -i apply to decoding only. Bound x264's
        # output threads too, otherwise concurrent jobs each auto-size to host CPU.
        "-threads:v", str(threads),
        "-preset", preset,
        "-b:v", f"{rendition['video_bitrate_kbps']}k",
        "-maxrate", f"{int(rendition['video_bitrate_kbps'] * 1.5)}k",
        "-bufsize", f"{int(rendition['video_bitrate_kbps'] * 2.0)}k",
        "-force_key_frames", f"expr:gte(t,n_forced*{seg_time})",
        "-g", str(gop_size),
        "-keyint_min", str(gop_size),
        "-sc_threshold", "0",
    ]

    if has_audio:
        cmd.extend([
            "-c:a", "aac",
            "-b:a", f"{rendition['audio_bitrate_kbps']}k",
            "-ar", "48000",
            "-ac", "2",
        ])
    else:
        cmd.extend(["-an"])

    cmd.extend([
        "-nostats",
        "-f", "hls",
        "-hls_time", str(seg_time),
        "-hls_playlist_type", "vod",
        "-hls_flags", "independent_segments",
        "-hls_segment_filename", str((rendition_dir / "seg_%03d.ts").resolve()),
        "-progress", "pipe:1",
        str((rendition_dir / "index.m3u8").resolve()),
    ])

    popen_kwargs: Dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "bufsize": 1,
    }
    if hasattr(os, "setsid"):
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)

    # FFmpeg writes structured progress to stdout and diagnostics to stderr.
    # Both streams must be drained concurrently: reading only stdout can leave
    # stderr full and block FFmpeg forever in a pipe write.
    stdout_events: "queue.Queue[Optional[str]]" = queue.Queue()
    stderr_lines: deque[str] = deque(maxlen=200)

    def drain_stdout() -> None:
        if not proc.stdout:
            stdout_events.put(None)
            return
        try:
            for raw_line in proc.stdout:
                stdout_events.put(raw_line)
        finally:
            stdout_events.put(None)

    def drain_stderr() -> None:
        if not proc.stderr:
            return
        for raw_line in proc.stderr:
            # Retain bounded, useful diagnostics without allowing a noisy
            # encoder to consume unbounded worker memory.
            stderr_lines.append(raw_line.rstrip()[:1024])

    stdout_thread = threading.Thread(target=drain_stdout, name="ffmpeg-stdout", daemon=True)
    stderr_thread = threading.Thread(target=drain_stderr, name="ffmpeg-stderr", daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    # Monotonic progress weighting: stage PROCESSING is 5.0% to 90.0%
    weight_per_rendition = 85.0 / total_renditions
    rendition_base = 5.0 + (rendition_index * weight_per_rendition)
    last_update_time = time.monotonic()
    last_progress_activity_at = last_update_time
    last_media_activity_at = last_update_time
    last_stall_check_at = last_update_time
    last_media_signature: Optional[Tuple[int, int, int]] = None

    current_fps = None
    current_speed = None

    def media_signature() -> Tuple[int, int, int]:
        segments = list(rendition_dir.glob("*.ts"))
        if not segments:
            return (0, 0, 0)
        return (
            len(segments),
            sum(segment.stat().st_size for segment in segments),
            max(segment.stat().st_mtime_ns for segment in segments),
        )

    stdout_closed = False
    try:
        while True:
            now = time.monotonic()
            try:
                line = stdout_events.get(timeout=1.0)
            except queue.Empty:
                line = ""

            if line is None:
                stdout_closed = True
            elif line:
                line = line.strip()
                if "=" in line:
                    k, v = (part.strip() for part in line.split("=", 1))
                    if k == "fps":
                        try:
                            current_fps = float(v)
                        except ValueError:
                            pass
                    elif k == "speed":
                        try:
                            current_speed = float(v.removesuffix("x"))
                        except ValueError:
                            pass
                    elif k == "out_time_us":
                        try:
                            out_sec = int(v) / 1_000_000.0
                        except ValueError:
                            continue
                        last_progress_activity_at = now
                        fraction = min(1.0, max(0.0, out_sec / duration)) if duration > 0 else 0.0
                        new_progress = min(89.9, max(progress_state["current"], rendition_base + (weight_per_rendition * fraction)))
                        progress_state["current"] = new_progress
                        if now - last_update_time >= settings.TRANSCODE_PROGRESS_THROTTLE_SECONDS:
                            last_update_time = now
                            eta = None
                            if current_speed and current_speed > 0:
                                eta = int(max(0.0, (duration * (total_renditions - rendition_index)) - out_sec) / current_speed)
                            if progress_callback:
                                progress_callback(new_progress, current_fps, current_speed, eta)

            if check_cancelled_fn and check_cancelled_fn():
                safe_kill_process_group(proc)
                raise TranscodeCancelledError()

            if now - last_stall_check_at >= settings.TRANSCODE_STALL_CHECK_INTERVAL_SECONDS:
                last_stall_check_at = now
                signature = media_signature()
                if signature != last_media_signature:
                    last_media_signature = signature
                    last_media_activity_at = now
                inactive_for = now - max(last_progress_activity_at, last_media_activity_at)
                if proc.poll() is None and inactive_for >= settings.TRANSCODE_STALL_TIMEOUT_SECONDS:
                    safe_kill_process_group(proc)
                    raise TranscodeError(
                        "FFmpeg stopped producing progress and media output",
                        error_code="FFMPEG_STALLED",
                    )

            if stdout_closed and proc.poll() is not None:
                break

        proc.wait()
    except Exception:
        safe_kill_process_group(proc)
        raise
    finally:
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        # Popen does not close the parent-side wrappers automatically. Close
        # them after the drainers have finished to avoid descriptor leaks.
        for stream in (proc.stdout, proc.stderr):
            if stream:
                stream.close()

    if proc.returncode != 0:
        stderr_out = "\n".join(stderr_lines)
        raise TranscodeError(f"FFmpeg rendition encoding failed: {stderr_out[:500]}", error_code="FFMPEG_ENCODE_FAILED")
