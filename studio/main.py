"""Main entry point for ABOODVIDEO Studio Desktop Application (Windows)."""
import os
import hashlib
from pathlib import Path
import subprocess
import sys
import json
import socket
import logging
import argparse
import threading
import time
import urllib.request
import webbrowser
import uvicorn

from studio.config import (
    VERSION,
    APP_NAME,
    USER_DATA_DIR,
    LOCK_FILE,
    load_studio_config,
    setup_studio_logging,
    migrate_user_data,
)

logger = logging.getLogger("aboodvideo_studio.main")
_mutex_handle = None


def get_single_instance_mutex_name(user_data_dir: Path = USER_DATA_DIR) -> str:
    """Keep one Studio process per profile while allowing isolated profiles."""
    profile = os.path.normcase(str(Path(user_data_dir).resolve()))
    profile_id = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:12]
    return f"Local\\{APP_NAME.replace(' ', '_')}_{profile_id}_SingleInstance_Mutex"


def acquire_single_instance_lock(port: int) -> bool:
    """Attempts to acquire a single-instance lock.
    Returns True if primary instance, False if another instance is already running.
    """
    global _mutex_handle
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            CreateMutexW = kernel32.CreateMutexW
            CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            CreateMutexW.restype = wintypes.HANDLE

            mutex_name = get_single_instance_mutex_name()
            mutex = CreateMutexW(None, True, mutex_name)
            last_err = kernel32.GetLastError()
            if last_err == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(mutex)
                return False
            _mutex_handle = mutex
        except Exception:
            pass

    # Check lock file
    if LOCK_FILE.exists():
        try:
            with open(LOCK_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            prev_port = data.get("port")
            if prev_port:
                # Ping previous instance to verify it is responsive
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{prev_port}/api/config", timeout=2.0) as resp:
                        if resp.status == 200:
                            return False
                except Exception:
                    pass
        except Exception:
            pass

    # Record active lock
    try:
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "pid": os.getpid(),
                "port": port,
                "url": f"http://127.0.0.1:{port}",
                "started_at": time.time(),
            }, f, indent=2)
    except Exception:
        pass

    return True


def notify_existing_instance() -> bool:
    """Notifies the running Studio instance to focus and open."""
    if not LOCK_FILE.exists():
        return False
    try:
        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        port = data.get("port")
        url = data.get("url") or f"http://127.0.0.1:{port}"
        if port:
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/system/app-focus",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                pass
        webbrowser.open(url)
        return True
    except Exception:
        return False


def find_free_port(start_port: int = 8599) -> int:
    """Finds an available local port on 127.0.0.1 starting from start_port."""
    for p in range(start_port, start_port + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start_port


def wait_for_server(url: str, timeout: float = 12.0) -> bool:
    """Waits for local server to be ready to accept requests."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(f"{url}/api/config", timeout=2.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


class StudioServer:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.url = f"http://{host}:{port}"
        self.server = None
        self.thread = None

    def start(self):
        from studio.web.app import app
        config = uvicorn.Config(
            app=app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True, name="StudioUvicorn")
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.should_exit = True
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3.0)


def main():
    # If stdout/stderr are None (--noconsole mode), redirect to devnull
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    setup_studio_logging()
    migrate_user_data()

    parser = argparse.ArgumentParser(description=f"{APP_NAME} v{VERSION}")
    parser.add_argument("--port", type=int, default=None, help="Server port (default: 8599)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Server host (default: 127.0.0.1)")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open window/browser")
    parser.add_argument("--browser", action="store_true", help="Force browser window instead of native WebView")

    args = parser.parse_args()

    # Security constraint: Server binds strictly to 127.0.0.1
    host = "127.0.0.1"

    cfg = load_studio_config()
    target_port = args.port or cfg.get("studio_port", 8599)
    port = find_free_port(target_port)

    # Single-instance check
    if not acquire_single_instance_lock(port):
        logger.info("Another instance of %s is already running. Activating existing instance.", APP_NAME)
        notify_existing_instance()
        sys.exit(0)

    logger.info("APP_START version=%s", VERSION)
    logger.info("APPDATA_ROOT=%s", USER_DATA_DIR.resolve())
    from studio.config import DEFAULT_DB_PATH, TEMP_DIR
    logger.info("JOBS_DB=%s", DEFAULT_DB_PATH.resolve())
    logger.info("OUTPUT_ROOT=%s", load_studio_config().get("converted_output_folder") or "<not configured>")
    logger.info("TEMP_ROOT=%s", TEMP_DIR.resolve())
    logger.info("Starting %s v%s on http://%s:%s", APP_NAME, VERSION, host, port)

    server = StudioServer(host=host, port=port)
    server.start()

    url = f"http://{host}:{port}"
    if not wait_for_server(url, timeout=12.0):
        logger.error("Studio server failed to start within timeout.")
        server.stop()
        sys.exit(1)

    use_native_window = not args.no_browser and not args.browser

    def on_app_exit():
        global _mutex_handle
        logger.info("Shutting down %s gracefully...", APP_NAME)
        try:
            from studio.core.batch_runner import batch_runner
            batch_runner.shutdown()
        except Exception:
            pass

        server.stop()
        try:
            if LOCK_FILE.exists():
                LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            pass

        if _mutex_handle and os.name == "nt":
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(_mutex_handle)
                _mutex_handle = None
            except Exception:
                pass

        logger.info("Shutdown complete.")

    if not args.no_browser:
        if use_native_window:
            try:
                import webview
                logger.info("Launching native WebView2 desktop window (%s)...", url)
                webview.create_window(
                    title=f"{APP_NAME} v{VERSION}",
                    url=url,
                    width=1440,
                    height=900,
                    min_size=(1024, 700),
                )
                webview.start(gui="edgechromium", debug=False)
                on_app_exit()
                sys.exit(0)
            except Exception as e:
                logger.warning("WebView2 initialization failed, falling back to browser: %s", e)

        # Fallback to Edge app-mode or default browser
        edge_launched = False
        if os.name == "nt":
            edge_paths = [
                Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            ]
            for ep in edge_paths:
                if ep.is_file():
                    try:
                        subprocess.Popen([str(ep), f"--app={url}", "--window-size=1440,900"])
                        edge_launched = True
                        break
                    except Exception:
                        pass
        if not edge_launched:
            webbrowser.open(url)

        try:
            while True:
                time.sleep(0.5)
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            on_app_exit()
    else:
        # Headless server mode (e.g. for testing / daemon usage)
        try:
            while True:
                time.sleep(0.5)
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            on_app_exit()


if __name__ == "__main__":
    main()
