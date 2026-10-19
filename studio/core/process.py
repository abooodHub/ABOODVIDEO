"""Centralized Windows-Safe Headless Process Execution Engine for ABOODVIDEO Studio.

Enforces zero-console execution for all CLI child processes (FFmpeg, FFprobe,
nvidia-smi, etc.) on Windows using CREATE_NO_WINDOW and STARTUPINFO SW_HIDE.
Guarantees no CMD/console window flash appears while preserving full stdout/stderr
pipe streaming, progress telemetry, cancellation, and exit-code detection.
"""
import os
import sys
import subprocess
from typing import Dict, Any, List, Optional, Union

# Win32 Process Creation Constants
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
STARTF_USESHOWWINDOW = 0x00000001 if os.name == "nt" else 0
SW_HIDE = 0


def get_headless_subprocess_kwargs(extra_creationflags: int = 0) -> Dict[str, Any]:
    """Returns keyword arguments to ensure a child process spawns with zero console window on Windows."""
    if os.name != "nt":
        return {}

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = SW_HIDE

    flags = subprocess.CREATE_NO_WINDOW | extra_creationflags

    return {
        "creationflags": flags,
        "startupinfo": startupinfo,
    }


def run_headless(
    cmd: Union[List[str], str],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text: bool = False,
    timeout: Optional[float] = None,
    check: bool = False,
    **kwargs
) -> subprocess.CompletedProcess:
    """Executes subprocess.run safely with headless/no-console creation flags on Windows."""
    kwargs.setdefault("shell", False)
    headless_kwargs = get_headless_subprocess_kwargs(kwargs.pop("creationflags", 0))

    if "startupinfo" in kwargs and "startupinfo" in headless_kwargs:
        si = kwargs["startupinfo"]
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = SW_HIDE
        headless_kwargs["startupinfo"] = si

    final_kwargs = {**headless_kwargs, **kwargs}
    return subprocess.run(
        cmd,
        stdout=stdout,
        stderr=stderr,
        text=text,
        timeout=timeout,
        check=check,
        **final_kwargs
    )


def popen_headless(
    cmd: Union[List[str], str],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    **kwargs
) -> subprocess.Popen:
    """Executes subprocess.Popen safely with headless/no-console creation flags on Windows."""
    kwargs.setdefault("shell", False)
    headless_kwargs = get_headless_subprocess_kwargs(kwargs.pop("creationflags", 0))

    if "startupinfo" in kwargs and "startupinfo" in headless_kwargs:
        si = kwargs["startupinfo"]
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = SW_HIDE
        headless_kwargs["startupinfo"] = si

    final_kwargs = {**headless_kwargs, **kwargs}
    return subprocess.Popen(
        cmd,
        stdout=stdout,
        stderr=stderr,
        **final_kwargs
    )
