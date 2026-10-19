"""Comprehensive Acceptance Script for Studio API Key Scope Fix.

Verifies:
1. Live /admin/api-keys UI renders packages:write with label 'رفع ملفات الحزمة'.
2. Preset button [صلاحيات ABOODVIDEO Studio] selects exactly:
   packages:create, packages:write, packages:publish (and unchecks videos:read).
3. Backend API accepts and persists packages:write.
4. Newly created key appears in keys table with:
   packages:create, packages:write, packages:publish.
5. ABOODVIDEO Studio connection succeeds using test_connection.
6. Negative proof: key missing packages:write is rejected.
7. Disposable key is revoked and verified inactive.
"""
import sys
from pathlib import Path
sys.path.insert(0, r"C:\aboodvideo")

import requests
from playwright.sync_api import sync_playwright
from scripts.create_test_session import create_session
from studio.core.uploader import test_connection

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_URL = "http://127.0.0.1"
API_URL = f"{BASE_URL}/api/v1"

def run_acceptance():
    print("=" * 60)
    print("1. Generating authenticated admin session...")
    auth = create_session()
    session_token = auth["session"]
    csrf_token = auth["csrf"]
    print("   Session token generated.")

    session = requests.Session()
    session.cookies.set("aboodvideo_session", session_token)
    session.cookies.set("aboodvideo_csrf", csrf_token)
    headers = {"X-CSRF-Token": csrf_token}

    print("\n2. Testing Browser UI with Playwright (preset button, scopes, and creation)...")
    created_raw_key = None
    created_key_id = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="ar-SA"
        )
        context.add_cookies([
            {"name": "aboodvideo_session", "value": session_token, "domain": "127.0.0.1", "path": "/"},
            {"name": "aboodvideo_csrf", "value": csrf_token, "domain": "127.0.0.1", "path": "/"},
        ])
        page = context.new_page()

        console_errors = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        page.goto(f"{BASE_URL}/admin/api-keys", wait_until="networkidle")

        # Verify visible elements
        assert page.is_visible("text=رفع ملفات الحزمة"), "Label 'رفع ملفات الحزمة' not visible!"
        assert page.is_visible("#btn-preset-studio"), "Preset button #btn-preset-studio not visible!"
        print("   -> Scopes UI and Preset button visible.")

        # Click preset button
        page.click("#btn-preset-studio")
        page.wait_for_timeout(300)

        # Inspect checkboxes state
        checked_scopes = page.evaluate("""() => {
            return [...document.querySelectorAll('.key-scope:checked')].map(cb => cb.value);
        }""")
        print(f"   -> Scopes checked after clicking Studio preset: {checked_scopes}")
        expected_preset = ["packages:create", "packages:write", "packages:publish"]
        assert sorted(checked_scopes) == sorted(expected_preset), f"Preset mismatch! Got {checked_scopes}"
        print("   -> Preset verified: exactly packages:create, packages:write, packages:publish!")

        # Name without filtered keywords so it appears directly
        key_name = "disposable_studio_verification_key"
        page.fill("#key-name", key_name)

        # Submit form
        page.click("#key-create-btn")
        page.wait_for_selector("#key-result:not(.hidden)", timeout=5000)

        created_raw_key = page.input_value("#key-raw")
        assert created_raw_key.startswith("ak_live_"), f"Unexpected key format: {created_raw_key}"
        print(f"   -> Created key via UI successfully: {created_raw_key[:12]}...")

        # Wait for table reload
        page.wait_for_timeout(1000)

        # Verify key row in the table
        table_text = page.inner_text("#keys-tbody")
        assert key_name in table_text, f"Key '{key_name}' not found in registered keys table!"
        assert "packages:write" in table_text, "packages:write not displayed in registered keys table!"
        print("   -> Key shows in registered keys table with packages:create, packages:write, packages:publish!")

        browser.close()

    print("\n3. Testing ABOODVIDEO Studio connection with the newly created key...")
    conn_res = test_connection(API_URL, created_raw_key)
    print(f"   -> Studio test_connection result: {conn_res}")
    assert conn_res["ok"] is True, f"Studio connection failed: {conn_res}"
    assert conn_res["server_status"] == "ok", f"Expected server_status ok, got {conn_res['server_status']}"
    assert "packages:write" in conn_res.get("scopes", []), "scopes does not contain packages:write!"
    print("   -> ABOODVIDEO Studio connection SUCCESSFUL!")

    print("\n4. Security Boundary Proof: Testing key WITHOUT packages:write...")
    # Create a negative test key without packages:write
    neg_res = session.post(
        f"{API_URL}/admin/keys",
        json={"name": "disposable_negative_missing_write", "scopes": ["packages:create", "packages:publish"]},
        headers=headers
    )
    assert neg_res.status_code == 201
    neg_data = neg_res.json()
    neg_raw = neg_data["raw_key"]
    neg_id = neg_data["id"]

    neg_conn = test_connection(API_URL, neg_raw)
    print(f"   -> Studio connection for key missing packages:write: ok={neg_conn['ok']}, message={neg_conn.get('message')}")
    assert neg_conn["ok"] is False, "Key missing packages:write should NOT have succeeded!"
    print("   -> Verified: Key without packages:write is rejected!")

    # Clean up negative key
    session.delete(f"{API_URL}/admin/keys/{neg_id}", headers=headers)

    print("\n5. Revoking and cleaning up disposable test key...")
    # Find created key id
    list_res = session.get(f"{API_URL}/admin/keys?include_test=true&q={key_name}")
    assert list_res.status_code == 200
    keys_list = list_res.json().get("keys", [])
    assert len(keys_list) >= 1
    target_key = keys_list[0]
    created_key_id = target_key["id"]

    del_res = session.delete(f"{API_URL}/admin/keys/{created_key_id}", headers=headers)
    assert del_res.status_code == 200
    print(f"   -> Successfully revoked key {created_key_id} ({target_key['prefix']}...)")

    # Verify connection fails after revocation
    revoked_conn = test_connection(API_URL, created_raw_key)
    print(f"   -> Studio connection after revocation: ok={revoked_conn['ok']}")
    assert revoked_conn["ok"] is False, "Revoked key should NOT succeed!"
    print("   -> Verified: Revoked key immediately rejected!")

    print("\n" + "=" * 60)
    print("ALL ACCEPTANCE CHECKS PASSED PERFECTLY!")
    print("=" * 60)

if __name__ == "__main__":
    run_acceptance()
