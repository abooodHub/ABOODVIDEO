"""HLS Multi-Rendition Transcoding Engine for ABOODVIDEO Studio."""
import os
import re
import queue
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable

from studio.core.hardware import get_full_telemetry, resolve_encoder_choice
from studio.core.probe import LADDER_SPECS, QUALITY_ORDER, probe_video
from studio.core.subtitles import build_hardsub_filter
from studio.core.process import run_headless, popen_headless
from studio.core.presets import RATE_PROFILES, normalize_preset_name


FFMPEG_NO_PROGRESS_TIMEOUT_SECONDS = 120


class DiskSafetyPause(RuntimeError):
    """Signals a safe pause before a rendition when free disk is insufficient."""


def can_copy_2160p_video(probe_info: Dict[str, Any], subtitle_mode: str = "softsub") -> bool:
    """Use H.264 source packets for 4K HLS only inside a browser-safe envelope."""
    return bool(
        subtitle_mode != "hardsub"
        and int(probe_info.get("width", 0) or 0) >= 3840
        and int(probe_info.get("height", 0) or 0) >= 2160
        and str(probe_info.get("video_codec", "")).lower() == "h264"
        and str(probe_info.get("pixel_format", "")).lower() == "yuv420p"
        and str(probe_info.get("video_profile", "")).lower() in {"high", "main", "baseline", "constrained baseline"}
        and 0 < int(probe_info.get("video_level", 0) or 0) <= 51
        and 0 < float(probe_info.get("fps", 0) or 0) <= 30
    )


def generate_poster_webp(source_path: Path, duration: float, output_path: Path, process_runner: Optional[Callable[..., Any]] = None) -> Path:
    """Extracts a representative WebP thumbnail image at ~10% timestamp."""
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = max(1.0, min(duration * 0.1, max(1.0, duration - 0.5)))

    from studio.core.ffmpeg_finder import get_ffmpeg_path
    ffmpeg_bin = get_ffmpeg_path()
    if not ffmpeg_bin:
        raise RuntimeError("أداة ffmpeg غير متوفرة في النظام.")
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-ss", f"{timestamp:.2f}",
        "-i", str(source_path.resolve()),
        "-vframes", "1",
        "-c:v", "libwebp",
        "-q:v", "85",
        str(output_path),
    ]

    run_process = process_runner or run_headless
    res = run_process(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30, check=False)
    if res.returncode != 0:
        # Fallback to standard frame
        fallback_cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-ss", f"{timestamp:.2f}",
            "-i", str(source_path.resolve()),
            "-vframes", "1",
            str(output_path),
        ]
        run_process(fallback_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30, check=False)

    return output_path


def transcode_package_renditions(
    source_path: Path,
    output_dir: Path,
    selected_qualities: List[str],
    encoder_choice: str = "auto",
    subtitle_mode: str = "none",
    subtitle_path: Optional[Path] = None,
    fonts_dir: Optional[Path] = None,
    hardsub_style: Optional[Dict[str, Any]] = None,
    hls_segment_duration: int = 6,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    process_launcher: Optional[Callable[..., Any]] = None,
    process_runner: Optional[Callable[..., Any]] = None,
    preset: str = "balanced",
    include_360p: bool = False,
    rate_profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Encodes selected video renditions into compliant HLS directories using either
    NVIDIA NVENC hardware acceleration or CPU x264.
    Emits real-time telemetry (progress %, rendition %, FPS, speed, ETA, hardware stats).
    """
    source_path = Path(source_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Probe source
    probe_info = probe_video(source_path)
    source_w = probe_info["width"]
    source_h = probe_info["height"]
    duration = probe_info["duration_seconds"]
    fps = probe_info["fps"]
    has_audio = probe_info["has_audio"]

    preset = normalize_preset_name(preset)
    rate_specs = RATE_PROFILES[preset]
    if rate_profile:
        video_rates = rate_profile.get("video_bitrate_bps_per_rendition") or {}
        audio_rates = rate_profile.get("audio_bitrate_bps_per_rendition") or {}
        rate_specs = {
            quality: {
                "video_bitrate_bps": int(video_rates.get(quality, rates["video_bitrate_bps"])),
                "audio_bitrate_bps": int(audio_rates.get(quality, rates["audio_bitrate_bps"])),
            }
            for quality, rates in RATE_PROFILES[preset].items()
        }

    # 2. Filter renditions: NEVER UPSCALE
    valid_qualities = []
    for q in selected_qualities:
        if q == "360p" and not include_360p:
            continue
        if q in LADDER_SPECS:
            spec = LADDER_SPECS[q]
            if spec["height"] <= source_h or spec["width"] <= source_w:
                valid_qualities.append(q)

    if not valid_qualities:
        if include_360p and source_h >= LADDER_SPECS["360p"]["height"]:
            valid_qualities = ["360p"]
        else:
            raise ValueError("لا توجد جودة إخراج مدعومة لهذا المصدر والإعداد.")

    # 3. Resolve only encoders that passed the real FFmpeg runtime probe.
    actual_encoder = resolve_encoder_choice(encoder_choice)

    # 4. Generate Poster frame first
    poster_path = output_dir / "poster.webp"
    generate_poster_webp(source_path, duration, poster_path, process_runner=process_runner)

    # 5. Execute Rendition Transcoding in sequence
    total_renditions = len(valid_qualities)
    from studio.core.ffmpeg_finder import get_ffmpeg_path
    ffmpeg_bin = get_ffmpeg_path()
    if not ffmpeg_bin:
        raise RuntimeError("أداة ffmpeg غير متوفرة في النظام.")

    encoded_rendition_results = []
    rendition_methods: Dict[str, Dict[str, Any]] = {}
    start_time_all = time.time()

    for idx, q_key in enumerate(valid_qualities):
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("تم إلغاء عملية المعالجة بواسطة المستخدم.")

        if progress_callback:
            remaining_bytes = sum(
                (rate_specs[q]["video_bitrate_bps"] + rate_specs[q]["audio_bitrate_bps"]) * duration / 8
                for q in valid_qualities[idx:]
            )
            allowed = progress_callback({
                "stage": "before_rendition",
                "current_rendition": q_key,
                "remaining_qualities": valid_qualities[idx:],
                "estimated_remaining_output_bytes": int(remaining_bytes),
                "source_duration_seconds": duration,
            })
            if allowed is False:
                raise DiskSafetyPause("تم الإيقاف الآمن قبل الجودة التالية بسبب انخفاض المساحة المتاحة.")

        spec = {**LADDER_SPECS[q_key], **rate_specs[q_key]}
        target_w = spec["width"]
        target_h = spec["height"]
        v_bitrate_kbps = spec["video_bitrate_bps"] // 1000
        a_bitrate_kbps = spec["audio_bitrate_bps"] // 1000

        rendition_dir = output_dir / q_key
        rendition_dir.mkdir(parents=True, exist_ok=True)

        gop_size = max(1, int(fps * hls_segment_duration))

        copy_4k = q_key == "2160p" and can_copy_2160p_video(probe_info, subtitle_mode)
        use_cuda_video_path = False
        copy_audio = bool(
            copy_4k and has_audio
            and str(probe_info.get("audio_codec", "")).lower() == "aac"
            and int(probe_info.get("audio_channels", 0) or 0) == 2
            and str(probe_info.get("audio_sample_rate", "")) == "48000"
            and str(probe_info.get("audio_profile", "")).lower() in {"lc", "aac lc", ""}
        )
        if copy_4k:
            cmd = [ffmpeg_bin, "-hide_banner", "-y", "-i", str(source_path),
                   "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy"]
            if has_audio and copy_audio:
                cmd.extend(["-c:a", "copy"])
            elif has_audio:
                cmd.extend(["-c:a", "aac", "-b:a", f"{a_bitrate_kbps}k", "-ar", "48000", "-ac", "2"])
            else:
                cmd.append("-an")
            rendition_encoder = "copy"
            rendition_methods[q_key] = {
                "video": "copy",
                "audio": "copy" if copy_audio or not has_audio else "aac-transcode",
                "four_k_video_mode": "COPY",
                "four_k_audio_mode": "COPY" if copy_audio else ("TRANSCODE" if has_audio else "NONE"),
            }
        else:
            source_pixel_format = str(probe_info.get("pixel_format") or "").lower()
            source_is_10bit = bool(probe_info.get("is_10bit")) or "10" in source_pixel_format or "p010" in source_pixel_format
            # Keep 10-bit HEVC decode and scaling on NVIDIA hardware when NVENC
            # is selected. This avoids software-decoding 4K Main 10 frames and
            # uploading each full frame again for encoding.
            use_cuda_video_path = bool(
                actual_encoder.endswith("_nvenc")
                and source_is_10bit
                and subtitle_mode != "hardsub"
            )
            scale_filter = f"scale=w={target_w}:h={target_h}:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2"
            if use_cuda_video_path:
                scale_filter = (
                    f"scale_cuda=w={target_w}:h={target_h}:"
                    "force_original_aspect_ratio=decrease:force_divisible_by=2:format=yuv420p"
                )
                cmd = [ffmpeg_bin, "-hide_banner", "-y", "-hwaccel", "cuda",
                       "-hwaccel_output_format", "cuda", "-i", str(source_path)]
            else:
                cmd = [ffmpeg_bin, "-hide_banner", "-y", "-i", str(source_path)]
            vf_filters = [scale_filter]
            if subtitle_mode == "hardsub" and subtitle_path and Path(subtitle_path).exists():
                vf_filters.append(build_hardsub_filter(Path(subtitle_path), fonts_dir, hardsub_style))
            cmd.extend(["-vf", ",".join(vf_filters)])
            rendition_encoder = actual_encoder
            rendition_methods[q_key] = {"video": actual_encoder, "audio": "aac-transcode" if has_audio else "none"}
            if use_cuda_video_path:
                rendition_methods[q_key].update({"decode": "cuda", "scale": "scale_cuda"})
            if q_key == "2160p":
                rendition_methods[q_key].update({"four_k_video_mode": "TRANSCODE", "four_k_audio_mode": "TRANSCODE" if has_audio else "NONE"})

        if not copy_4k and actual_encoder.endswith("_nvenc"):
            nvenc_preset = {"economy": "p3", "balanced": "p5", "quality": "p7"}[preset]
            cmd.extend([
                "-c:v", actual_encoder,
                "-preset", nvenc_preset,
                "-rc", "vbr",
                "-cq", "22",
                "-b:v", f"{v_bitrate_kbps}k",
                "-maxrate", f"{int(v_bitrate_kbps * 1.5)}k",
                "-bufsize", f"{int(v_bitrate_kbps * 2.0)}k",
            ])
            if not use_cuda_video_path:
                cmd.extend(["-pix_fmt", "yuv420p"])
        elif not copy_4k and actual_encoder.endswith("_qsv"):
            qsv_preset = {"economy": "veryfast", "balanced": "medium", "quality": "slower"}[preset]
            cmd.extend([
                "-c:v", actual_encoder,
                "-preset", qsv_preset,
                "-global_quality", "23",
                "-b:v", f"{v_bitrate_kbps}k",
                "-maxrate", f"{int(v_bitrate_kbps * 1.5)}k",
                "-bufsize", f"{int(v_bitrate_kbps * 2.0)}k",
                "-pix_fmt", "nv12",
            ])
        elif not copy_4k and actual_encoder.endswith("_amf"):
            amf_quality = {"economy": "speed", "balanced": "balanced", "quality": "quality"}[preset]
            cmd.extend([
                "-c:v", actual_encoder,
                "-quality", amf_quality,
                "-b:v", f"{v_bitrate_kbps}k",
                "-maxrate", f"{int(v_bitrate_kbps * 1.5)}k",
                "-bufsize", f"{int(v_bitrate_kbps * 2.0)}k",
                "-pix_fmt", "yuv420p",
            ])
        elif not copy_4k:
            cpu_preset = {"economy": "veryfast", "balanced": "medium", "quality": "slow"}[preset]
            cmd.extend([
                "-c:v", actual_encoder,
                "-preset", cpu_preset,
                "-b:v", f"{v_bitrate_kbps}k",
                "-maxrate", f"{int(v_bitrate_kbps * 1.5)}k",
                "-bufsize", f"{int(v_bitrate_kbps * 2.0)}k",
                "-pix_fmt", "yuv420p",
            ])

        if not copy_4k:
            cmd.extend([
                "-force_key_frames", f"expr:gte(t,n_forced*{hls_segment_duration})",
                "-g", str(gop_size), "-keyint_min", str(gop_size), "-sc_threshold", "0",
            ])

        if has_audio and not copy_4k:
            cmd.extend([
                "-c:a", "aac",
                "-b:a", f"{a_bitrate_kbps}k",
                "-ar", "48000",
                "-ac", "2",
            ])
        elif not has_audio:
            cmd.extend(["-an"])

        playlist_file = rendition_dir / "index.m3u8"
        segment_pattern = rendition_dir / "seg_%03d.ts"

        cmd.extend([
            "-nostats",
            "-f", "hls",
            "-hls_time", str(hls_segment_duration),
            "-hls_playlist_type", "vod",
            "-hls_flags", "independent_segments" if not copy_4k else "temp_file",
            "-hls_segment_filename", str(segment_pattern),
            "-progress", "pipe:1",
            str(playlist_file),
        ])

        # Spawn FFmpeg process
        rendition_started = time.monotonic()
        launch_process = process_launcher or popen_headless
        proc = launch_process(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        stdout_lines: queue.Queue = queue.Queue()
        stderr_lines: List[str] = []

        def reader():
            try:
                for line in proc.stdout:
                    stdout_lines.put(line.strip())
            except Exception:
                pass
            stdout_lines.put(None)

        def err_reader():
            try:
                for line in proc.stderr:
                    stderr_lines.append(line)
            except Exception:
                pass

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t_err = threading.Thread(target=err_reader, daemon=True)
        t_err.start()

        # Telemetry tracking variables
        current_out_time = 0.0
        last_media_progress_at = time.monotonic()
        current_fps = 0.0
        current_speed_str = "0x"
        current_speed_val = 1.0
        last_progress = "continue"
        elapsed_media_seconds = 0.0

        last_callback_time = 0.0

        while True:
            if cancel_event and cancel_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise RuntimeError("تم إلغاء عملية المعالجة.")

            try:
                line = stdout_lines.get(timeout=0.2)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                if time.monotonic() - last_media_progress_at >= FFMPEG_NO_PROGRESS_TIMEOUT_SECONDS:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                    try:
                        t_err.join(timeout=1.0)
                    except Exception:
                        pass
                    stderr_tail = "".join(stderr_lines)[-1200:].strip()
                    details = f" تفاصيل FFmpeg: {stderr_tail}" if stderr_tail else ""
                    raise RuntimeError(
                        f"توقف FFmpeg عن إحراز تقدم لمدة {FFMPEG_NO_PROGRESS_TIMEOUT_SECONDS} ثانية "
                        f"أثناء تحويل {q_key}. أُوقفت العملية تلقائيًا.{details}"
                    )
                continue

            if line is None:
                break

            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()

                if k == "out_time_us":
                    try:
                        next_out_time = float(v) / 1000000.0
                        if next_out_time > current_out_time + 0.01:
                            current_out_time = next_out_time
                            last_media_progress_at = time.monotonic()
                    except ValueError:
                        pass
                elif k == "out_time_ms":
                    try:
                        next_out_time = float(v) / 1000000.0
                        if next_out_time > current_out_time + 0.01:
                            current_out_time = next_out_time
                            last_media_progress_at = time.monotonic()
                    except ValueError:
                        pass
                elif k == "progress":
                    last_progress = v
                elif k == "fps":
                    try:
                        current_fps = float(v)
                    except ValueError:
                        pass
                elif k == "speed":
                    current_speed_str = v
                    try:
                        current_speed_val = float(v.replace("x", ""))
                    except ValueError:
                        current_speed_val = 1.0

            # Compute progress
            rendition_pct = min(100.0, max(0.0, (current_out_time / duration) * 100)) if duration > 0 else 0.0
            overall_pct = min(100.0, max(0.0, ((idx + (rendition_pct / 100.0)) / total_renditions) * 100))

            # Remaining time in seconds
            rem_seconds_rendition = max(0.0, duration - current_out_time)
            rem_seconds_total = rem_seconds_rendition + (total_renditions - idx - 1) * duration
            eta_seconds = rem_seconds_total / max(0.2, current_speed_val)
            elapsed_media_seconds = max(elapsed_media_seconds, current_out_time)

            # Throttle callback to ~100ms
            now = time.time()
            if now - last_callback_time >= 0.15:
                last_callback_time = now
                if progress_callback:
                    hw_stats = get_full_telemetry()["hardware"]
                    progress_callback({
                        "stage": "transcoding",
                        "current_rendition": q_key,
                        "rendition_index": idx + 1,
                        "total_renditions": total_renditions,
                        "rendition_progress_pct": round(rendition_pct, 1),
                        "overall_progress_pct": round(overall_pct, 1),
                        "fps": round(current_fps, 1),
                        "speed": current_speed_str,
                        "eta_seconds": int(eta_seconds),
                        "eta_formatted": time.strftime("%H:%M:%S", time.gmtime(int(eta_seconds))),
                        "elapsed_seconds": round(time.monotonic() - rendition_started, 1),
                        "elapsed_media_seconds": round(elapsed_media_seconds, 2),
                        "progress_state": last_progress,
                        "encoder_used": rendition_encoder,
                        "hardware": hw_stats,
                    })

        proc.wait()
        try:
            t_err.join(timeout=1.0)
            t.join(timeout=1.0)
        except Exception:
            pass
        if proc.returncode != 0:
            stderr_err = "".join(stderr_lines) if stderr_lines else "Unknown error"
            raise RuntimeError(f"فشل FFmpeg في تحويل جودة {q_key}: {stderr_err[:500]}")

        rendition_elapsed = max(0.001, time.monotonic() - rendition_started)
        if progress_callback:
            progress_callback({
                "stage": "transcoding_rendition_completed",
                "current_rendition": q_key,
                "rendition_index": idx + 1,
                "total_renditions": total_renditions,
                "completed_renditions": [item["quality_label"] for item in encoded_rendition_results],
                "rendition_elapsed_seconds": round(rendition_elapsed, 3),
                "source_duration_seconds": duration,
                "speed_multiplier": round(duration / rendition_elapsed, 4),
                "source_resolution": f"{source_w}x{source_h}",
                "output_resolution": f"{target_w}x{target_h}",
                "codec": "hevc" if ("hevc" in actual_encoder or "265" in actual_encoder) else "h264",
                "subtitle_mode": "softsub",
                "fps": round(fps * duration / rendition_elapsed, 1),
                "encoder_used": rendition_encoder,
                "hardware": get_full_telemetry()["hardware"],
            })

        # Count segments
        seg_count = len(list(rendition_dir.glob("seg_*.ts")))

        # Compute actual output dimensions preserving aspect ratio within bounding box
        sar = source_w / max(1, source_h)
        tar = target_w / max(1, target_h)
        if sar >= tar:
            rend_w = min(target_w, source_w)
            rend_h = int(round(rend_w / sar / 2.0) * 2)
        else:
            rend_h = min(target_h, source_h)
            rend_w = int(round(rend_h * sar / 2.0) * 2)

        encoded_rendition_results.append({
            "quality_label": q_key,
            "width": rend_w,
            "height": rend_h,
            "video_bitrate_bps": int(probe_info.get("video_bitrate_bps") or spec["video_bitrate_bps"]) if copy_4k else spec["video_bitrate_bps"],
            "audio_bitrate_bps": int(probe_info.get("audio_bitrate_bps") or spec["audio_bitrate_bps"]) if copy_audio else spec["audio_bitrate_bps"],
            "playlist_path": f"{q_key}/index.m3u8",
            "segment_count": seg_count,
        })

    # Final 100% callback
    if progress_callback:
        progress_callback({
            "stage": "transcoding_completed",
            "overall_progress_pct": 100.0,
            "rendition_progress_pct": 100.0,
            "total_renditions": total_renditions,
            "encoder_used": actual_encoder,
        })

    return {
        "output_dir": str(output_dir),
        "renditions": encoded_rendition_results,
        "encoder": actual_encoder,
        "rendition_methods": rendition_methods,
        "poster_path": str(poster_path),
        "duration_seconds": duration,
        "elapsed_seconds": round(time.time() - start_time_all, 2),
    }
