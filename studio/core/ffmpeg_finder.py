"""FFmpeg and FFprobe binary detection and diagnostic engine for ABOODVIDEO Studio."""
import os
import sys
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any

from studio.core.process import run_headless


def get_ffmpeg_path() -> Optional[str]:
    """Finds FFmpeg binary in bundled application directories, PATH, or standard Windows paths."""
    # 1. Bundled with application (frozen or alongside executable)
    if getattr(sys, "frozen", False):
        candidates = [
            Path(sys.executable).parent / "bin" / "ffmpeg.exe",
            Path(sys.executable).parent / "ffmpeg.exe",
        ]
        if hasattr(sys, "_MEIPASS"):
            candidates.extend([
                Path(sys._MEIPASS) / "bin" / "ffmpeg.exe",
                Path(sys._MEIPASS) / "ffmpeg.exe",
            ])
        for c in candidates:
            if c.is_file():
                return str(c.resolve())

    # Alongside current script/project root
    proj_root = Path(__file__).resolve().parent.parent.parent
    proj_candidates = [
        proj_root / "bin" / "ffmpeg.exe",
        proj_root / "ffmpeg.exe",
    ]
    for c in proj_candidates:
        if c.is_file():
            return str(c.resolve())

    # 2. System PATH
    found = shutil.which("ffmpeg")
    if found:
        return str(Path(found).resolve())

    # 3. Standard Windows locations
    std_locations = [
        Path("C:/tools/bin/ffmpeg.exe"),
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "ffmpeg" / "bin" / "ffmpeg.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe",
        Path("C:/ProgramData/chocolatey/bin/ffmpeg.exe"),
    ]
    for p in std_locations:
        if p.is_file():
            return str(p.resolve())

    return None


def get_ffprobe_path() -> Optional[str]:
    """Finds FFprobe binary in bundled application directories, PATH, or standard Windows paths."""
    # 1. Bundled with application (frozen or alongside executable)
    if getattr(sys, "frozen", False):
        candidates = [
            Path(sys.executable).parent / "bin" / "ffprobe.exe",
            Path(sys.executable).parent / "ffprobe.exe",
        ]
        if hasattr(sys, "_MEIPASS"):
            candidates.extend([
                Path(sys._MEIPASS) / "bin" / "ffprobe.exe",
                Path(sys._MEIPASS) / "ffprobe.exe",
            ])
        for c in candidates:
            if c.is_file():
                return str(c.resolve())

    proj_root = Path(__file__).resolve().parent.parent.parent
    proj_candidates = [
        proj_root / "bin" / "ffprobe.exe",
        proj_root / "ffprobe.exe",
    ]
    for c in proj_candidates:
        if c.is_file():
            return str(c.resolve())

    # 2. System PATH
    found = shutil.which("ffprobe")
    if found:
        return str(Path(found).resolve())

    # 3. Standard Windows locations
    std_locations = [
        Path("C:/tools/bin/ffprobe.exe"),
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "ffmpeg" / "bin" / "ffprobe.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "ffprobe.exe",
        Path("C:/ProgramData/chocolatey/bin/ffprobe.exe"),
    ]
    for p in std_locations:
        if p.is_file():
            return str(p.resolve())

    return None


_CACHED_DIAGNOSTICS: Optional[Dict[str, Any]] = None


def get_ffmpeg_diagnostics(force_refresh: bool = False) -> Dict[str, Any]:
    """Returns detailed diagnostics of FFmpeg, FFprobe, and NVENC hardware encoder availability."""
    global _CACHED_DIAGNOSTICS
    if _CACHED_DIAGNOSTICS is not None and not force_refresh:
        return _CACHED_DIAGNOSTICS

    ffmpeg_bin = get_ffmpeg_path()
    ffprobe_bin = get_ffprobe_path()

    is_avail = bool(ffmpeg_bin and ffprobe_bin)
    nvenc_supported = False
    encoders = []

    if ffmpeg_bin:
        try:
            proc = run_headless(
                [ffmpeg_bin, "-encoders"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
                check=False,
            )
            for line in proc.stdout.splitlines():
                if "nvenc" in line.lower():
                    parts = line.split()
                    if len(parts) >= 2:
                        encoders.append(parts[1])
            nvenc_supported = "h264_nvenc" in encoders
        except Exception:
            pass

    status_str = "FFmpeg متوفر ✓ (NVENC مدعوم)" if (is_avail and nvenc_supported) else ("FFmpeg متوفر ✓" if is_avail else "FFmpeg غير مثبت أو غير متاح")

    res = {
        "available": is_avail,
        "ffmpeg_path": ffmpeg_bin or "غير متوفر",
        "ffprobe_path": ffprobe_bin or "غير متوفر",
        "nvenc_supported": nvenc_supported,
        "encoders": encoders,
        "status_arabic": status_str,
        "error_message": None if is_avail else "FFmpeg غير مثبت أو غير متاح",
    }
    _CACHED_DIAGNOSTICS = res
    return res

