"""Hardware telemetry and verified FFmpeg encoder capability detection."""
from __future__ import annotations

import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

import psutil

from studio.core.process import run_headless


_CAPABILITY_CACHE: Optional[List[Dict[str, Any]]] = None

_ENCODER_DEFINITIONS = (
    ("h264_nvenc", "H.264 NVENC", "gpu", "NVIDIA"),
    ("hevc_nvenc", "HEVC NVENC", "gpu", "NVIDIA"),
    ("h264_qsv", "H.264 Intel Quick Sync", "gpu", "Intel"),
    ("hevc_qsv", "HEVC Intel Quick Sync", "gpu", "Intel"),
    ("h264_amf", "H.264 AMD AMF", "gpu", "AMD"),
    ("hevc_amf", "HEVC AMD AMF", "gpu", "AMD"),
    ("libx264", "CPU x264", "cpu", "CPU"),
    ("libx265", "CPU x265", "cpu", "CPU"),
)


def _ffmpeg_encoder_names() -> set[str]:
    from studio.core.ffmpeg_finder import get_ffmpeg_path
    ffmpeg_bin = get_ffmpeg_path()
    if not ffmpeg_bin:
        return set()
    try:
        proc = run_headless(
            [ffmpeg_bin, "-hide_banner", "-encoders"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            timeout=10, check=False,
        )
    except Exception:
        return set()
    names: set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            names.add(parts[1])
    return names


def _runtime_probe(encoder_id: str) -> tuple[bool, str]:
    """Encode one synthetic frame so listed but unusable hardware is rejected."""
    from studio.core.ffmpeg_finder import get_ffmpeg_path
    ffmpeg_bin = get_ffmpeg_path()
    if not ffmpeg_bin:
        return False, "FFmpeg غير متوفر"
    cmd = [
        ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", "color=c=black:s=320x180:r=1", "-frames:v", "1",
        "-c:v", encoder_id, "-f", "null", "-",
    ]
    try:
        proc = run_headless(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", timeout=15, check=False,
        )
        if proc.returncode == 0:
            return True, ""
        detail = (proc.stderr or "").strip().splitlines()
        return False, (detail[-1][:180] if detail else "تعذر تشغيل المشفر")
    except Exception as exc:
        return False, str(exc)[:180]


def detect_encoder_capabilities(force_refresh: bool = False) -> List[Dict[str, Any]]:
    global _CAPABILITY_CACHE
    if _CAPABILITY_CACHE is not None and not force_refresh:
        return [dict(item) for item in _CAPABILITY_CACHE]
    listed = _ffmpeg_encoder_names()
    capabilities: List[Dict[str, Any]] = []
    for encoder_id, name, group, vendor in _ENCODER_DEFINITIONS:
        is_listed = encoder_id in listed
        available, reason = _runtime_probe(encoder_id) if is_listed else (False, "غير موجود في FFmpeg")
        capabilities.append({
            "id": encoder_id, "name": name, "group": group, "vendor": vendor,
            "listed_by_ffmpeg": is_listed, "available": available, "reason": reason,
        })
    _CAPABILITY_CACHE = capabilities
    return [dict(item) for item in capabilities]


def available_encoder_ids() -> List[str]:
    return [item["id"] for item in detect_encoder_capabilities() if item["available"]]


def resolve_encoder_choice(choice: str) -> str:
    requested = str(choice or "auto").casefold()
    available = available_encoder_ids()
    if requested in ("auto", "gpu", "nvenc"):
        selected = next((item for item in ("h264_nvenc", "h264_qsv", "h264_amf") if item in available), None)
        if selected:
            return selected
        if requested != "auto":
            raise RuntimeError("لا يوجد مشفر GPU متوافق اجتاز اختبار FFmpeg الفعلي.")
        if "libx264" in available:
            return "libx264"
    if requested == "cpu":
        requested = "libx264"
    if requested not in available:
        raise RuntimeError(f"المشفر {choice} غير متاح على هذا الجهاز.")
    return requested


def check_nvenc_support() -> bool:
    return "h264_nvenc" in available_encoder_ids()


def get_gpu_telemetry() -> Dict[str, Any]:
    nvidia_smi = shutil.which("nvidia-smi")
    empty = {
        "gpu_detected": False, "gpu_name": "غير متوفر", "nvenc_supported": check_nvenc_support(),
        "gpu_util_pct": 0, "gpu_temp_c": 0, "vram_used_mb": 0, "vram_total_mb": 0,
        "vram_util_pct": 0.0,
    }
    if not nvidia_smi:
        return empty
    cmd = [nvidia_smi, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"]
    try:
        proc = run_headless(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            parts = [part.strip() for part in proc.stdout.strip().splitlines()[0].split(",")]
            used = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            total = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            return {
                "gpu_detected": True, "gpu_name": parts[0], "nvenc_supported": check_nvenc_support(),
                "gpu_util_pct": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
                "vram_used_mb": used, "vram_total_mb": total,
                "vram_util_pct": round(used / total * 100, 1) if total else 0.0,
                "gpu_temp_c": int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0,
            }
    except Exception:
        pass
    return {**empty, "gpu_name": "خطأ في القراءة"}


def get_cpu_and_ram_telemetry() -> Dict[str, Any]:
    vm = psutil.virtual_memory()
    logical = psutil.cpu_count(logical=True) or 1
    return {
        "cpu_util_pct": round(psutil.cpu_percent(interval=None), 1),
        "cpu_cores_logical": logical,
        "cpu_cores_physical": psutil.cpu_count(logical=False) or logical,
        "ram_used_mb": int(vm.used / 1024 / 1024),
        "ram_total_mb": int(vm.total / 1024 / 1024),
        "ram_util_pct": round(vm.percent, 1),
    }


def benchmark_encoder(encoder_choice: str, duration_seconds: float = 3.0) -> Dict[str, Any]:
    from studio.core.ffmpeg_finder import get_ffmpeg_path
    encoder_id = resolve_encoder_choice(encoder_choice)
    ffmpeg_bin = get_ffmpeg_path()
    frames = max(30, int(duration_seconds * 30))
    cmd = [
        ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", "testsrc2=size=1280x720:rate=30", "-frames:v", str(frames),
        "-c:v", encoder_id, "-pix_fmt", "yuv420p", "-f", "null", "-",
    ]
    started = time.monotonic()
    proc = run_headless(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=45, check=False)
    elapsed = max(0.001, time.monotonic() - started)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "تعذر اختبار المشفر")[-500:])
    fps = frames / elapsed
    capability = next(item for item in detect_encoder_capabilities() if item["id"] == encoder_id)
    return {
        "encoder": encoder_id, "encoder_name": capability["name"], "fps": round(fps, 1),
        "speed_multiplier": round(fps / 30.0, 2), "elapsed_seconds": round(elapsed, 2),
        "result": "PASS", "hardware": get_full_telemetry()["hardware"],
    }


def get_full_telemetry() -> Dict[str, Any]:
    gpu = get_gpu_telemetry()
    cpu_ram = get_cpu_and_ram_telemetry()
    capabilities = detect_encoder_capabilities()
    recommended = "nvenc" if any(item["id"] == "h264_nvenc" and item["available"] for item in capabilities) else "cpu"
    return {"hardware": {"gpu": gpu, "cpu_ram": cpu_ram, "recommended_encoder": recommended, "encoders": capabilities}}
