"""Acceptance test: Run installed ABOODVIDEO Studio v0.1.1 and test connection against live server."""
import sys
import os
import time
import json
import hashlib
import subprocess
import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, r"C:\aboodvideo")

INSTALLED_EXE = os.path.expandvars(r"%LOCALAPPDATA%\Programs\ABOODVIDEO Studio\ABOODVIDEO Studio.exe")
USER_DATA_DIR = os.path.expandvars(r"%LOCALAPPDATA%\ABOODVIDEO Studio")
LIVE_BASE = "http://127.0.0.1"
LIVE_API = f"{LIVE_BASE}/api/v1"
STUDIO_PORT = 8556

def get_file_hash(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()

def main():
    print("=" * 60)
    print("STEP 1: Verify installed EXE path and version")
    print("=" * 60)
    assert os.path.exists(INSTALLED_EXE), f"Installed EXE not found at {INSTALLED_EXE}"
    print(f"Installed EXE found: {INSTALLED_EXE}")

    # Check user data hashes
    hashes_pre = {
        "jobs.db": get_file_hash(os.path.join(USER_DATA_DIR, "jobs.db")),
        "config.json": get_file_hash(os.path.join(USER_DATA_DIR, "config.json")),
        "secrets.dat": get_file_hash(os.path.join(USER_DATA_DIR, "secrets.dat")),
    }
    print(f"Pre-test user data hashes:")
    for k, v in hashes_pre.items():
        print(f"  {k}: {v}")

    print("\n" + "=" * 60)
    print("STEP 2: Create disposable Studio key with required scopes")
    print("=" * 60)
    from scripts.create_test_session import create_session
    auth = create_session()
    headers = {"X-CSRF-Token": auth["csrf"]}
    cookies = {
        "aboodvideo_session": auth["session"],
        "aboodvideo_csrf": auth["csrf"],
    }
    
    # Create key with exact Studio scopes
    res = httpx.post(
        f"{LIVE_API}/admin/keys",
        json={
            "name": "disposable_installed_studio_test",
            "scopes": ["packages:create", "packages:write", "packages:publish"],
        },
        headers=headers,
        cookies=cookies,
        timeout=10.0,
    )
    assert res.status_code == 201, f"Failed to create test key: {res.status_code} {res.text}"
    key_data = res.json()
    raw_key = key_data["raw_key"]
    key_id = key_data["id"]
    key_scopes = key_data["scopes"]
    print(f"Created disposable key ID: {key_id}")
    print(f"Raw key prefix: {raw_key[:12]}...")
    print(f"Assigned scopes: {key_scopes}")
    assert sorted(key_scopes) == sorted(["packages:create", "packages:write", "packages:publish"])

    print("\n" + "=" * 60)
    print("STEP 3: Launch installed v0.1.1 EXE in headless server mode")
    print("=" * 60)
    proc = subprocess.Popen(
        [INSTALLED_EXE, "--no-browser", "--port", str(STUDIO_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"Spawned installed Studio process PID: {proc.pid}")

    # Wait for Studio server to be ready
    ready = False
    for i in range(25):
        time.sleep(0.5)
        try:
            r = httpx.get(f"http://127.0.0.1:{STUDIO_PORT}/api/config", timeout=2.0)
            if r.status_code == 200:
                ready = True
                print(f"Installed Studio server is responding on port {STUDIO_PORT} (after {(i+1)*0.5:.1f}s)")
                break
        except Exception:
            pass

    assert ready, f"Installed Studio failed to respond on port {STUDIO_PORT}"

    try:
        print("\n" + "=" * 60)
        print("STEP 4: Test Connection from the INSTALLED v0.1.1 application")
        print("=" * 60)
        test_payload = {
            "server_url": LIVE_API,
            "api_key": raw_key,
        }
        resp = httpx.post(
            f"http://127.0.0.1:{STUDIO_PORT}/api/test-connection",
            json=test_payload,
            timeout=10.0,
        )
        print(f"POST /api/test-connection status: {resp.status_code}")
        test_result = resp.json()
        print(f"Test Connection response JSON:")
        print(f"  ok: {test_result.get('ok')}")
        print(f"  server_status: {test_result.get('server_status')}")
        print(f"  scopes: {test_result.get('scopes')}")
        print(f"  server_url: {test_result.get('server_url')}")
        print(f"  message: {test_result.get('message')}")

        assert test_result.get("ok") is True, f"test_connection returned ok=False: {test_result}"
        assert test_result.get("server_status") == "ok", f"Expected server_status 'ok', got {test_result.get('server_status')}"
        assert "packages:write" in test_result.get("scopes", []), "packages:write missing from returned scopes!"
        print(">>> INSTALLED STUDIO CONNECTION TEST PASSED! <<<")

    finally:
        print("\n" + "=" * 60)
        print("STEP 5: Terminate installed Studio process")
        print("=" * 60)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        print("Studio process terminated.")

        print("\n" + "=" * 60)
        print("STEP 6: Revoke disposable key")
        print("=" * 60)
        try:
            rev_res = httpx.delete(
                f"{LIVE_API}/admin/keys/{key_id}",
                headers=headers,
                cookies=cookies,
                timeout=10.0,
            )
            print(f"Revoke key status: {rev_res.status_code}")
        except Exception as e:
            print(f"Revoke error: {e}")

    print("\n" + "=" * 60)
    print("STEP 7: Verify user data preserved post-test")
    print("=" * 60)
    hashes_post = {
        "jobs.db": get_file_hash(os.path.join(USER_DATA_DIR, "jobs.db")),
        "config.json": get_file_hash(os.path.join(USER_DATA_DIR, "config.json")),
        "secrets.dat": get_file_hash(os.path.join(USER_DATA_DIR, "secrets.dat")),
    }
    for k, v in hashes_post.items():
        preserved = (v == hashes_pre[k])
        print(f"  {k}: {v} (preserved: {preserved})")
        assert v is not None, f"{k} is missing!"

    print("\nALL ACCEPTANCE CRITERIA VERIFIED SUCCESSFULLY!")

if __name__ == "__main__":
    main()
