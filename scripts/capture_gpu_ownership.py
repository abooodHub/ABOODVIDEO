"""Read-only Windows WDDM per-process GPU accounting, with explicit ownership.

Device-wide nvidia-smi memory must never be labelled as Studio-owned memory.
No elevated privileges, process control, credentials or user configuration reads.
"""
import json
import re
import subprocess

import psutil


def capture_gpu_ownership(owner_pid, additional_owner_pids=()):
    owned = set()
    for pid in (owner_pid, *additional_owner_pids):
        try:
            parent = psutil.Process(pid)
            owned.update({pid, *(p.pid for p in parent.children(recursive=True))})
        except psutil.NoSuchProcess:
            pass
    command = (
        "Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUProcessMemory "
        "| Select-Object Name,DedicatedUsage,SharedUsage,TotalCommitted | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode or not result.stdout.strip():
        return {"available": False, "reason": "WDDM process counters unavailable"}
    rows = json.loads(result.stdout)
    if isinstance(rows, dict):
        rows = [rows]
    processes = {}
    for row in rows:
        match = re.search(r"pid_(\d+)_", row["Name"])
        if not match:
            continue
        pid = int(match.group(1))
        try:
            name = psutil.Process(pid).name()
        except psutil.Error:
            name = "exited-or-inaccessible"
        entry = processes.setdefault(pid, {"pid": pid, "name": name, "owned": pid in owned,
                                          "dedicated_bytes": 0, "shared_bytes": 0})
        entry["dedicated_bytes"] += int(row["DedicatedUsage"])
        entry["shared_bytes"] += int(row["SharedUsage"])
    values = list(processes.values())
    return {"available": True, "source": "Windows WDDM process counters",
            "owned_dedicated_bytes": sum(p["dedicated_bytes"] for p in values if p["owned"]),
            "external_dedicated_bytes": sum(p["dedicated_bytes"] for p in values if not p["owned"]),
            "processes": sorted(values, key=lambda p: p["dedicated_bytes"], reverse=True)}
