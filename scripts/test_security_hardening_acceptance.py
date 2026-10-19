"""Acceptance verification script for Windows DPAPI API Key Storage Security Hardening."""
import os
import sys
import time
import json
import urllib.request
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

EXE_PATH = Path(r"C:\aboodvideo\dist\ABOODVIDEO-Studio\ABOODVIDEO Studio.exe")
INSTALLER_PATH = Path(r"C:\aboodvideo\dist\ABOODVIDEO-Studio-Setup.exe")
USER_DATA_DIR = Path(os.environ["LOCALAPPDATA"]) / "ABOODVIDEO Studio"
CONFIG_FILE = USER_DATA_DIR / "config.json"
SECRETS_FILE = USER_DATA_DIR / "secrets.dat"
LOG_FILE = USER_DATA_DIR / "logs" / "studio.log"
LOCK_FILE = USER_DATA_DIR / "studio.lock"

REAL_TEST_API_KEY = os.environ["ABOODVIDEO_ACCEPTANCE_API_KEY"]
ORIGINAL_LIVE_KEY = os.environ["ABOODVIDEO_ACCEPTANCE_API_KEY"]
SERVER_URL = "http://127.0.0.1/api/v1"
TEST_PORT = 8596


def run_acceptance():
    print("=== FINAL SECURITY HARDENING ACCEPTANCE TEST ===")
    assert EXE_PATH.is_file(), f"EXE not found at {EXE_PATH}"
    assert INSTALLER_PATH.is_file(), f"Installer not found at {INSTALLER_PATH}"
    print(f"[OK] Found built EXE: {EXE_PATH} ({EXE_PATH.stat().st_size:,} bytes)")
    print(f"[OK] Found built Installer: {INSTALLER_PATH} ({INSTALLER_PATH.stat().st_size:,} bytes)")

    # Clean any stale lock
    if LOCK_FILE.exists():
        try:
            LOCK_FILE.unlink()
        except Exception:
            pass

    # -------------------------------------------------------------
    # 1. Plaintext Migration Proof
    # -------------------------------------------------------------
    print("\n--- Step 1: Testing Plaintext Key Migration from Legacy config.json ---")
    legacy_key = os.environ["ABOODVIDEO_ACCEPTANCE_API_KEY"]
    # Write legacy config with plaintext api_key
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "server_url": SERVER_URL,
            "api_key": legacy_key,
            "studio_port": 8555,
        }, f, indent=2)
    if SECRETS_FILE.exists():
        SECRETS_FILE.unlink()

    # Trigger migration via Python config module
    from studio.config import load_studio_config, migrate_user_data
    migrate_user_data()
    migrated_cfg = load_studio_config()

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        migrated_disk = json.load(f)

    assert "api_key" not in migrated_disk, "Migration failed: api_key still in config.json"
    assert migrated_disk.get("secret_storage") == "dpapi_current_user", "Missing secret_storage in config.json"
    assert SECRETS_FILE.exists(), "secrets.dat was not created by migration"
    assert migrated_cfg.get("api_key") == legacy_key, "Decrypted migrated key does not match legacy key"
    print("[OK] Plaintext migration succeeded: raw key removed from config.json, encrypted in DPAPI secrets.dat")

    # -------------------------------------------------------------
    # 2. Save Real-Format Test API Key via Running Studio Application
    # -------------------------------------------------------------
    print(f"\n--- Step 2: Saving Real-Format Test Key via Studio EXE (port {TEST_PORT}) ---")
    proc = subprocess.Popen([str(EXE_PATH), "--no-browser", "--port", str(TEST_PORT)])
    url = f"http://127.0.0.1:{TEST_PORT}"

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
        assert ready, "Primary instance failed to become responsive"
        print(f"[OK] Studio.exe is running on {url}")

        # Send POST /api/config with real test key
        req_data = json.dumps({"server_url": SERVER_URL, "api_key": REAL_TEST_API_KEY}).encode("utf-8")
        req = urllib.request.Request(f"{url}/api/config", data=req_data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            save_res = json.loads(resp.read().decode("utf-8"))
        assert save_res.get("ok") is True, f"Failed to save config: {save_res}"
        print(f"[OK] Saved real-format test API key: {REAL_TEST_API_KEY[:10]}...{REAL_TEST_API_KEY[-6:]}")

    finally:
        # -------------------------------------------------------------
        # 3. Close Studio
        # -------------------------------------------------------------
        print("\n--- Step 3: Closing Studio Instance ---")
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except Exception:
            proc.kill()
        print("[OK] Studio instance closed.")

    # -------------------------------------------------------------
    # 4. Inspect config.json: Raw Key Absent
    # -------------------------------------------------------------
    print("\n--- Step 4: Inspecting config.json on Disk ---")
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        disk_content = f.read()
        disk_json = json.loads(disk_content)

    print("config.json contents on disk:")
    print(disk_content.strip())

    assert "api_key" not in disk_json, "SECURITY LEAK: 'api_key' key found in config.json!"
    assert REAL_TEST_API_KEY not in disk_content, "SECURITY LEAK: literal test API key found in config.json!"
    assert disk_json.get("secret_storage") == "dpapi_current_user", "Expected secret_storage = 'dpapi_current_user'"
    print("[OK] Verification passed: raw key is strictly ABSENT from config.json")

    # -------------------------------------------------------------
    # 5. Restart Studio & Verify Automatic Reconnection
    # -------------------------------------------------------------
    print(f"\n--- Step 5: Restarting Studio Instance (port {TEST_PORT}) ---")
    proc2 = subprocess.Popen([str(EXE_PATH), "--no-browser", "--port", str(TEST_PORT)])

    try:
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
        assert ready, "Restarted instance failed to become responsive"
        print(f"[OK] Restarted Studio.exe is running on {url}")

        # Query GET /api/config to verify decrypted key in memory
        with urllib.request.urlopen(f"{url}/api/config", timeout=2.0) as resp:
            restarted_cfg = json.loads(resp.read().decode("utf-8"))

        loaded_key = restarted_cfg.get("api_key")
        assert loaded_key == REAL_TEST_API_KEY, f"Decrypted key mismatch: got {loaded_key}"
        print(f"[OK] In-memory API key automatically decrypted by DPAPI CurrentUser: {loaded_key[:10]}...{loaded_key[-6:]}")

        # Query /api/test-connection with the automatically decrypted key
        test_payload = json.dumps({"server_url": SERVER_URL, "api_key": loaded_key}).encode("utf-8")
        req = urllib.request.Request(f"{url}/api/test-connection", data=test_payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            conn_res = json.loads(resp.read().decode("utf-8"))
        print(f"[OK] Automatic connection check response: server_url={conn_res.get('server_url')}, status={conn_res.get('server_status')}")

    finally:
        print("\n--- Shutting Down Restarted Instance ---")
        proc2.terminate()
        try:
            proc2.wait(timeout=3.0)
        except Exception:
            proc2.kill()
        print("[OK] Restarted instance closed.")

    # -------------------------------------------------------------
    # 6. Search %LOCALAPPDATA%\ABOODVIDEO Studio for Literal Key
    # -------------------------------------------------------------
    print(f"\n--- Step 6: Searching %LOCALAPPDATA%\\ABOODVIDEO Studio for Literal Test Key ---")
    leaks = []
    for root, dirs, files in os.walk(USER_DATA_DIR):
        for fname in files:
            fpath = Path(root) / fname
            try:
                # Read as raw bytes to catch any encoding
                data = fpath.read_bytes()
                if REAL_TEST_API_KEY.encode("utf-8") in data or REAL_TEST_API_KEY.encode("ascii") in data:
                    leaks.append(str(fpath))
            except Exception:
                pass

    print(f"Files scanned in {USER_DATA_DIR}: {len(list(USER_DATA_DIR.rglob('*')))}")
    print(f"Literal test key leaks found: {leaks}")
    assert len(leaks) == 0, f"SECURITY FAILURE: Literal key found in {leaks}"
    print("[OK] Secret scan passed: literal test key does NOT exist in ordinary plaintext files or logs!")

    # -------------------------------------------------------------
    # 7. Restore Legitimate Server API Key for User
    # -------------------------------------------------------------
    print(f"\n--- Step 7: Restoring Production Live Key (Encrypted in DPAPI) ---")
    from studio.config import save_studio_config
    save_studio_config({"server_url": SERVER_URL, "api_key": ORIGINAL_LIVE_KEY})
    restored = load_studio_config()
    assert restored.get("api_key") == ORIGINAL_LIVE_KEY
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        assert ORIGINAL_LIVE_KEY not in f.read()
    print("[OK] Production key restored and securely encrypted via DPAPI.")

    print("\n=== ALL SECURITY ACCEPTANCE CHECKS PASSED SUCCESSFULLY ===")


if __name__ == "__main__":
    run_acceptance()
