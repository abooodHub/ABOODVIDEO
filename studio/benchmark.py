import json
import os
import shutil
import subprocess
import sys
import time
import threading
from pathlib import Path
from studio import __version__
from typing import Dict, Any, List

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import psutil
from studio.core.hardware import get_gpu_telemetry, check_nvenc_support
from studio.core.probe import LADDER_SPECS, probe_video
from studio.core.process import run_headless


def run_4k_nvenc_benchmark(source_video_path: Path, output_root: Path) -> Dict[str, Any]:
    """
    Executes true 4K NVENC benchmark across all 5 renditions:
    2160p, 1080p, 720p, 480p, 360p.
    Measures encoding time, average FPS, speed, peak GPU %, peak VRAM, peak CPU %, and output size.
    """
    source_video_path = Path(source_video_path).resolve()
    output_root = Path(output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    probe = probe_video(source_video_path)
    source_w = probe["width"]
    source_h = probe["height"]
    duration = probe["duration_seconds"]
    fps = probe["fps"]

    ladder_keys = ["2160p", "1080p", "720p", "480p", "360p"]
    # Filter: Downscale only, no upscale (supports scope/cinematic)
    valid_ladder = [k for k in ladder_keys if LADDER_SPECS[k]["height"] <= source_h or LADDER_SPECS[k]["width"] <= source_w]

    ffmpeg_bin = shutil.which("ffmpeg")
    rendition_reports = []

    print(f"\n========================================================")
    print(f"  ABOODVIDEO Studio v{__version__} — 4K NVENC Hardware Benchmark")
    print(f"  Source: {source_video_path.name}")
    print(f"  Resolution: {source_w}x{source_h} | FPS: {fps} | Duration: {duration:.2f}s")
    print(f"  Renditions to benchmark: {valid_ladder}")
    print(f"========================================================\n")

    for q_key in valid_ladder:
        spec = LADDER_SPECS[q_key]
        target_w = spec["width"]
        target_h = spec["height"]
        v_bitrate_kbps = spec["video_bitrate_bps"] // 1000
        a_bitrate_kbps = spec["audio_bitrate_bps"] // 1000

        rendition_dir = output_root / q_key
        rendition_dir.mkdir(parents=True, exist_ok=True)

        seg_pattern = rendition_dir / "seg_%03d.ts"
        m3u8_path = rendition_dir / "index.m3u8"
        gop = max(1, int(fps * 6))

        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-i", str(source_video_path),
            "-vf", f"scale=w={target_w}:h={target_h}:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "h264_nvenc",
            "-preset", "p4",
            "-rc", "vbr",
            "-cq", "22",
            "-b:v", f"{v_bitrate_kbps}k",
            "-maxrate", f"{int(v_bitrate_kbps * 1.5)}k",
            "-bufsize", f"{int(v_bitrate_kbps * 2.0)}k",
            "-pix_fmt", "yuv420p",
            "-force_key_frames", "expr:gte(t,n_forced*6)",
            "-g", str(gop),
            "-keyint_min", str(gop),
            "-sc_threshold", "0",
            "-c:a", "aac",
            "-b:a", f"{a_bitrate_kbps}k",
            "-ar", "48000",
            "-ac", "2",
            "-nostats",
            "-f", "hls",
            "-hls_time", "6",
            "-hls_playlist_type", "vod",
            "-hls_flags", "independent_segments",
            "-hls_segment_filename", str(seg_pattern),
            str(m3u8_path),
        ]

        # Telemetry monitors
        peak_gpu = 0
        peak_vram = 0
        peak_cpu = 0.0
        stop_monitor = threading.Event()

        def monitor_telemetry():
            nonlocal peak_gpu, peak_vram, peak_cpu
            while not stop_monitor.is_set():
                try:
                    gpu = get_gpu_telemetry()
                    if gpu["gpu_detected"]:
                        peak_gpu = max(peak_gpu, gpu["gpu_util_pct"])
                        peak_vram = max(peak_vram, gpu["vram_used_mb"])
                    cpu_pct = psutil.cpu_percent(interval=None)
                    peak_cpu = max(peak_cpu, cpu_pct)
                except Exception:
                    pass
                time.sleep(0.1)

        t_mon = threading.Thread(target=monitor_telemetry, daemon=True)
        t_mon.start()

        start_time = time.time()
        proc = run_headless(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        end_time = time.time()

        stop_monitor.set()
        t_mon.join(timeout=1.0)

        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg NVENC encode failed for {q_key}: {proc.stderr[:400]}")

        elapsed = max(0.001, end_time - start_time)
        total_frames = int(duration * fps)
        avg_fps = round(total_frames / elapsed, 1)
        avg_speed = round(duration / elapsed, 2)

        # Measure rendition output size
        total_bytes = sum(f.stat().st_size for f in rendition_dir.glob("*.*"))

        rep = {
            "rendition": q_key,
            "resolution": f"{target_w}x{target_h}",
            "bitrate_target_kbps": v_bitrate_kbps,
            "encoding_time_sec": round(elapsed, 2),
            "avg_fps": avg_fps,
            "avg_speed": f"{avg_speed}x",
            "peak_gpu_util_pct": peak_gpu,
            "peak_vram_mb": peak_vram,
            "peak_cpu_pct": round(peak_cpu, 1),
            "output_size_bytes": total_bytes,
            "output_size_formatted": f"{total_bytes / (1024 * 1024):.2f} MB",
        }
        rendition_reports.append(rep)

        print(f"  [OK] {q_key:<6} ({target_w}x{target_h}): Time: {elapsed:.2f}s | FPS: {avg_fps} | Speed: {avg_speed}x | GPU: {peak_gpu}% | VRAM: {peak_vram} MB | CPU: {peak_cpu:.1f}% | Size: {rep['output_size_formatted']}")

    return {
        "source": str(source_video_path),
        "source_resolution": f"{source_w}x{source_h}",
        "duration_seconds": round(duration, 2),
        "renditions": rendition_reports,
    }


if __name__ == "__main__":
    cut_file = Path("spirit_cage_4k_cut.mkv")
    out_dir = Path("bench_4k_output")
    res = run_4k_nvenc_benchmark(cut_file, out_dir)
    print("\nBenchmark Summary JSON:\n", json.dumps(res, indent=2, ensure_ascii=False))
