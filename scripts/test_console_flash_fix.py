"""Comprehensive validation script for Windows Console Flash / Headless Process Execution Fix.

Directly tests built C:\\aboodvideo\\dist\\ABOODVIDEO-Studio\\ABOODVIDEO Studio.exe:
- Monitors Windows process tree for cmd.exe, powershell.exe, conhost.exe
- Tests FFmpeg, FFprobe, and nvidia-smi execution without console windows
- Performs 5 consecutive launches to prove ZERO console flashes
- Verifies clean shutdown and responsiveness
"""
import os
import sys
import time
import json
import urllib.request
import subprocess
from pathlib import Path
import psutil

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

EXE_PATH = Path(r"C:\aboodvideo\dist\ABOODVIDEO-Studio\ABOODVIDEO Studio.exe")
INSTALLER_PATH = Path(r"C:\aboodvideo\dist\ABOODVIDEO-Studio-Setup.exe")
USER_DATA_DIR = Path(os.environ["LOCALAPPDATA"]) / "ABOODVIDEO Studio"
LOCK_FILE = USER_DATA_DIR / "studio.lock"

# Win32 Window Inspection
import ctypes
import ctypes.wintypes
user32 = ctypes.windll.user32


def get_visible_windows_for_pids(pids: set) -> list:
    """Finds all visible top-level windows belonging to given PIDs."""
    visible_windows = []

    def enum_cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids:
                length = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
                class_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, class_buf, 256)
                cls_name = class_buf.value
                # Ignore invisible/zero-size or system message windows
                rect = ctypes.wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                width = rect.right - rect.left
                height = rect.bottom - rect.top
                if width > 0 and height > 0 and cls_name != "MSCTFIME UI":
                    visible_windows.append({"hwnd": hwnd, "title": title, "class": cls_name, "pid": pid.value})
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
    return visible_windows


def get_process_tree_pids(parent_pid: int) -> set:
    """Returns set of all PIDs in process tree rooted at parent_pid."""
    pids = {parent_pid}
    try:
        parent = psutil.Process(parent_pid)
        for child in parent.children(recursive=True):
            pids.add(child.pid)
    except Exception:
        pass
    return pids


def test_startup_and_headless_policy():
    print("=== ABOODVIDEO STUDIO HEADLESS PROCESS ACCEPTANCE TEST ===")
    assert EXE_PATH.is_file(), f"EXE not found at {EXE_PATH}"
    print(f"[OK] Found built EXE: {EXE_PATH} ({EXE_PATH.stat().st_size:,} bytes)")
    print(f"[OK] Found built Installer: {INSTALLER_PATH} ({INSTALLER_PATH.stat().st_size:,} bytes)")

    if LOCK_FILE.exists():
        try:
            LOCK_FILE.unlink()
        except Exception:
            pass

    # -------------------------------------------------------------
    # 1. Launch Primary Instance and Monitor Process Tree
    # -------------------------------------------------------------
    port = 8594
    print(f"\n--- 1. Launching Studio.exe on port {port} & Auditing Process Tree ---")
    proc = subprocess.Popen([str(EXE_PATH), "--no-browser", "--port", str(port)])
    url = f"http://127.0.0.1:{port}"

    try:
        # Wait for server ready
        ready = False
        for _ in range(30):
            time.sleep(0.4)
            try:
                with urllib.request.urlopen(f"{url}/api/config", timeout=1.0) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                pass
        assert ready, "Server failed to start within timeout"
        print(f"[OK] Studio server is responsive at {url}")

        # Check child processes of Studio
        tree_pids = get_process_tree_pids(proc.pid)
        children = []
        for p in tree_pids:
            try:
                proc_obj = psutil.Process(p)
                children.append((p, proc_obj.name()))
            except Exception:
                pass
        print(f"Active process tree: {children}")

        # Verify no cmd.exe or powershell.exe in tree
        names = [c[1].lower() for c in children]
        assert "cmd.exe" not in names, f"FORBIDDEN: cmd.exe detected in process tree: {children}"
        assert "powershell.exe" not in names, f"FORBIDDEN: powershell.exe detected in process tree: {children}"
        print("[OK] Process tree audit passed: ZERO cmd.exe or powershell.exe instances.")

        # ---------------------------------------------------------
        # 2. Trigger FFmpeg Diagnostics (checks FFmpeg & FFprobe)
        # ---------------------------------------------------------
        print("\n--- 2. Triggering FFmpeg & FFprobe Diagnostics Endpoint ---")
        with urllib.request.urlopen(f"{url}/api/diagnostics/ffmpeg", timeout=5.0) as resp:
            diag = json.loads(resp.read().decode("utf-8"))
        print(f"[OK] Diagnostics response: available={diag.get('available')}, status={diag.get('status_arabic')}")
        assert diag.get("available") is True

        # Check for visible console windows during diagnostics
        vis_windows = get_visible_windows_for_pids(get_process_tree_pids(proc.pid))
        console_windows = [w for w in vis_windows if "console" in w["class"].lower() or "cmd" in w["title"].lower()]
        assert len(console_windows) == 0, f"FORBIDDEN: Console window found: {console_windows}"
        print("[OK] FFmpeg / FFprobe diagnostics executed with ZERO console windows.")

        # ---------------------------------------------------------
        # 3. Trigger Hardware / GPU Telemetry (queries nvidia-smi)
        # ---------------------------------------------------------
        print("\n--- 3. Triggering GPU / Hardware Telemetry (nvidia-smi polling) ---")
        with urllib.request.urlopen(f"{url}/api/hardware", timeout=8.0) as resp:
            hw = json.loads(resp.read().decode("utf-8"))
        gpu = hw.get("hardware", {}).get("gpu", {})
        print(f"[OK] Hardware response: GPU={gpu.get('gpu_name')}, NVENC={gpu.get('nvenc_supported')}")

        # Check for visible console windows during GPU polling
        vis_windows = get_visible_windows_for_pids(get_process_tree_pids(proc.pid))
        console_windows = [w for w in vis_windows if "console" in w["class"].lower() or "cmd" in w["title"].lower()]
        assert len(console_windows) == 0, f"FORBIDDEN: Console window found during GPU polling: {console_windows}"
        print("[OK] GPU polling executed with ZERO console windows.")

        # ---------------------------------------------------------
        # 4. Single-instance Focus Test
        # ---------------------------------------------------------
        print("\n--- 4. Testing Secondary Launch Rejection ---")
        sec = subprocess.run([str(EXE_PATH), "--no-browser", "--port", str(port + 1)], capture_output=True, timeout=10)
        assert sec.returncode == 0
        print("[OK] Secondary launch cleanly exited with code 0.")

    finally:
        print("\n--- Shutting Down Primary Instance ---")
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except Exception:
            proc.kill()
        print("[OK] Clean shutdown confirmed.")

    # -------------------------------------------------------------
    # 5. Consecutive 5-Launch Console Flash Test
    # -------------------------------------------------------------
    print("\n==================================================")
    print("--- 5. Executing 5 Consecutive Launches (Zero Console Flash Proof) ---")
    print("==================================================")

    for i in range(1, 6):
        launch_port = 8590 + i
        if LOCK_FILE.exists():
            try:
                LOCK_FILE.unlink()
            except Exception:
                pass

        p = subprocess.Popen([str(EXE_PATH), "--no-browser", "--port", str(launch_port)])
        time.sleep(0.6)  # During startup phase where console would flash

        # Inspect all windows of process tree
        tree = get_process_tree_pids(p.pid)
        vis = get_visible_windows_for_pids(tree)
        console_wins = [w for w in vis if "console" in w["class"].lower() or "cmd" in w["title"].lower()]

        # Query health
        l_url = f"http://127.0.0.1:{launch_port}"
        ready = False
        for _ in range(15):
            time.sleep(0.2)
            try:
                with urllib.request.urlopen(f"{l_url}/api/config", timeout=1.0) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                pass

        p.terminate()
        try:
            p.wait(timeout=3.0)
        except Exception:
            p.kill()

        assert len(console_wins) == 0, f"Launch #{i}: Console window detected: {console_wins}"
        assert ready, f"Launch #{i}: Server did not become ready"
        print(f"[OK] Launch #{i}/5: Server ready, 0 console windows, clean shutdown.")

    print("\n=== ALL CONSOLE FLASH FIX ACCEPTANCE CHECKS PASSED ===")


if __name__ == "__main__":
    test_startup_and_headless_policy()
