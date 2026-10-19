"""Execute ABOODVIDEO Admin Consolidation V4 Acceptance Verification."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from playwright.async_api import async_playwright, expect

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ARTIFACT_DIR = Path(r"C:\Users\abood\.gemini\antigravity\brain\0181878d-15d4-4078-9f16-05b68b1ff8e2")
BASE_URL = "http://127.0.0.1"

remote_code = """
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from app.core.database import SyncSessionLocal
from app.models.user import User
from app.models.session import UserSession
from app.security.tokens import hash_token
from app.security.csrf import generate_csrf_token

raw_token = secrets.token_urlsafe(32)
token_hashed = hash_token(raw_token)
csrf_token = generate_csrf_token()

with SyncSessionLocal() as session:
    user = session.query(User).filter(User.email == 'admin_live@aboodvideo.local').first()
    if not user:
        raise RuntimeError("User admin_live@aboodvideo.local not found")
    sess = UserSession(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=token_hashed,
        ip_address='127.0.0.1',
        user_agent='playwright',
        expires_at=datetime.now(timezone.utc) + timedelta(days=1)
    )
    session.add(sess)
    session.commit()
    import json
    print(json.dumps({"session": raw_token, "csrf": csrf_token}))
"""

def get_auth_tokens():
    cmd = [
        "ssh", "-o", "BatchMode=yes",
        "abood@127.0.0.1",
        "docker exec -i aboodvideo_api python"
    ]
    res = subprocess.run(cmd, input=remote_code, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Error {res.returncode}:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
    for line in res.stdout.strip().splitlines():
        if line.startswith("{") and "session" in line:
            return json.loads(line)
    raise RuntimeError(f"Could not parse auth tokens: {res.stdout}")


async def main():
    print("=== ABOODVIDEO ADMIN CONSOLIDATION V4 VERIFICATION ===")
    tokens = get_auth_tokens()
    session_token = tokens["session"]
    csrf_token = tokens["csrf"]
    print(f"Obtained Owner Session: {session_token[:10]}... and CSRF: {csrf_token[:10]}...")

    console_errors = []
    csp_violations = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="ar-SA"
        )

        # Set cookies for the domain
        await context.add_cookies([
            {
                "name": "aboodvideo_session",
                "value": session_token,
                "domain": "127.0.0.1",
                "path": "/",
                "httpOnly": True,
                "sameSite": "Lax"
            },
            {
                "name": "aboodvideo_csrf",
                "value": csrf_token,
                "domain": "127.0.0.1",
                "path": "/",
                "httpOnly": False,
                "sameSite": "Lax"
            }
        ])

        page = await context.new_page()

        def on_console(msg):
            text = msg.text
            if msg.type == "error":
                console_errors.append(f"[{page.url}] {text}")
            if "Content Security Policy" in text or "violates" in text:
                csp_violations.append(f"[{page.url}] {text}")

        page.on("console", on_console)
        page.on("pageerror", lambda err: console_errors.append(f"[{page.url}] Uncaught error: {err}"))
        page.on("response", lambda res: print(f"HTTP {res.status}: {res.url}") if res.status >= 400 else None)

        # -------------------------------------------------------------
        # 1. ADMIN DASHBOARD (/admin)
        # -------------------------------------------------------------
        print("\n--- 1. Testing Admin Dashboard (/admin) ---")
        await page.goto(f"{BASE_URL}/admin", wait_until="networkidle")
        await page.wait_for_timeout(1000)

        # Verify page title and header
        title_text = await page.locator(".page-title").text_content()
        print(f"Page Title: {title_text.strip()}")

        # Verify real video count is 1
        video_stat = await page.locator("#stat-videos").text_content()
        print(f"Initial Real Videos Count: {video_stat.strip()}")
        assert video_stat.strip() == "1", f"Expected 1 real video, got {video_stat}"

        async def toggle_switch(input_id, desired_state: bool):
            is_checked = await page.locator(f"#{input_id}").is_checked()
            if is_checked != desired_state:
                await page.locator(f"label:has(#{input_id})").click()
                await page.wait_for_timeout(800)

        # Test toggle test data
        await toggle_switch("toggle-test-data", True)

        toggled_stat = await page.locator("#stat-videos").text_content()
        print(f"With Test Data Included: {toggled_stat.strip()}")
        assert int(toggled_stat.strip()) >= 60, f"Expected >=60 videos with test data, got {toggled_stat}"

        # Toggle back
        await toggle_switch("toggle-test-data", False)
        reverted_stat = await page.locator("#stat-videos").text_content()
        print(f"Reverted Real Videos Count: {reverted_stat.strip()}")
        assert reverted_stat.strip() == "1", f"Expected 1 real video, got {reverted_stat}"

        # Capture Dashboard Screenshot
        dash_shot = ARTIFACT_DIR / "admin_dashboard_1440x900.png"
        await page.screenshot(path=str(dash_shot))
        print(f"Saved: {dash_shot.name}")

        # -------------------------------------------------------------
        # 2. PROCESSING QUEUE (/admin/processing)
        # -------------------------------------------------------------
        print("\n--- 2. Testing Processing Queue (/admin/processing) ---")
        await page.goto(f"{BASE_URL}/admin/processing", wait_until="networkidle")
        await page.wait_for_timeout(1000)

        # Verify Active Tab is selected by default
        active_tab_btn = page.locator(".tab-btn[data-tab='active']")
        assert "is-active" in (await active_tab_btn.get_attribute("class")), "Active tab should be active by default"
        print("Active Jobs Tab verified as default.")

        # Switch to History tab
        history_tab_btn = page.locator(".tab-btn[data-tab='history']")
        await history_tab_btn.click()
        await page.wait_for_timeout(1000)

        # Verify 0 real historical jobs (isolated from test jobs)
        history_rows = page.locator("#jobs-tbody tr")
        history_empty = await page.locator("#jobs-tbody .table-empty-cell").count()
        print(f"History Empty Cell count (clean): {history_empty}")
        assert history_empty > 0 or await history_rows.count() == 0

        # Toggle test jobs
        await toggle_switch("toggle-test-jobs", True)

        all_history_count = await page.locator("#jobs-tbody tr").count()
        print(f"History Jobs with Test Jobs: {all_history_count}")
        assert all_history_count == 10, f"Expected 10 historical transcode jobs, got {all_history_count}"

        # Toggle back to clean
        await toggle_switch("toggle-test-jobs", False)

        # Switch back to Active tab for screenshot representation
        await active_tab_btn.click()
        await page.wait_for_timeout(500)

        # Capture Processing Screenshot
        proc_shot = ARTIFACT_DIR / "admin_processing_1440x900.png"
        await page.screenshot(path=str(proc_shot))
        print(f"Saved: {proc_shot.name}")

        # -------------------------------------------------------------
        # 3. SETTINGS (/admin/settings)
        # -------------------------------------------------------------
        print("\n--- 3. Testing Settings (/admin/settings) ---")
        await page.goto(f"{BASE_URL}/admin/settings", wait_until="networkidle")
        await page.wait_for_timeout(1000)

        # Verify 5 tabs exist
        tabs = await page.locator(".settings-tab-btn").all_text_contents()
        clean_tabs = [t.strip() for t in tabs]
        print(f"Settings Tabs: {clean_tabs}")
        assert len(clean_tabs) == 5, f"Expected 5 tabs, got {len(clean_tabs)}"

        # Check Storage Telemetry
        storage_tab_btn = page.locator(".settings-tab-btn[data-tab='storage']")
        await storage_tab_btn.click()
        await page.wait_for_timeout(800)
        storage_status = await page.locator("#storage-status").text_content()
        storage_used = await page.locator("#storage-used").text_content()
        print(f"Storage Telemetry: Status={storage_status.strip()}, Used={storage_used.strip()}")

        # Switch back to Transcode Tab
        transcode_tab_btn = page.locator(".settings-tab-btn[data-tab='transcode']")
        await transcode_tab_btn.click()
        await page.wait_for_timeout(500)

        # Test dirty save bar
        save_bar = page.locator("#sticky-save-bar")
        assert not ("is-visible" in (await save_bar.get_attribute("class") or "")), "Save bar should be hidden initially"

        # Toggle a quality checkbox
        first_checkbox = page.locator("#quality-options input[type='checkbox']").first
        await first_checkbox.click(force=True)
        await page.wait_for_timeout(500)

        # Save bar must now be visible!
        assert "is-visible" in (await save_bar.get_attribute("class")), "Save bar should be visible after form change"
        print("Sticky Save Bar revealed on dirty state.")

        # Click Reset button
        btn_reset = page.locator("#btn-reset-settings")
        await btn_reset.click()
        await page.wait_for_timeout(500)

        # Save bar must now be hidden!
        assert not ("is-visible" in (await save_bar.get_attribute("class") or "")), "Save bar should hide after reset"
        print("Sticky Save Bar hid after reset.")

        # Capture Settings Screenshot
        settings_shot = ARTIFACT_DIR / "admin_settings_1440x900.png"
        await page.screenshot(path=str(settings_shot))
        print(f"Saved: {settings_shot.name}")

        # -------------------------------------------------------------
        # 4. API KEYS (/admin/api-keys)
        # -------------------------------------------------------------
        print("\n--- 4. Testing API Keys (/admin/api-keys) ---")
        await page.goto(f"{BASE_URL}/admin/api-keys", wait_until="networkidle")
        await page.wait_for_timeout(1000)

        # Check total count and rows (clean view)
        keys_total_text = await page.locator("#keys-total-count").text_content()
        print(f"Initial Keys Count: {keys_total_text.strip()}")
        real_key_rows = await page.locator("#keys-tbody tr").count()
        print(f"Real Keys Rows: {real_key_rows}")
        assert real_key_rows == 2, f"Expected 2 real API keys, got {real_key_rows}"

        page_info = await page.locator("#key-page-info").text_content()
        print(f"Pagination Info: {page_info.strip()}")
        assert "صفحة 1 من 1" in page_info

        # Toggle test keys
        await toggle_switch("toggle-test-keys", True)

        all_keys_total_text = await page.locator("#keys-total-count").text_content()
        print(f"Total Keys with Tests: {all_keys_total_text.strip()}")
        all_key_rows = await page.locator("#keys-tbody tr").count()
        print(f"Page 1 Rows with Tests (Limit 20): {all_key_rows}")
        assert all_key_rows == 20, f"Expected 20 keys per page, got {all_key_rows}"

        all_page_info = await page.locator("#key-page-info").text_content()
        print(f"All Keys Pagination: {all_page_info.strip()}")
        assert "صفحة 1 من 13" in all_page_info, f"Expected 13 pages, got {all_page_info}"

        # Test pagination next
        btn_next = page.locator("#btn-key-next")
        assert not await btn_next.is_disabled(), "Next button should be enabled"
        await btn_next.click()
        await page.wait_for_timeout(1000)

        page2_info = await page.locator("#key-page-info").text_content()
        print(f"After Clicking Next: {page2_info.strip()}")
        assert "صفحة 2 من 13" in page2_info, f"Expected page 2, got {page2_info}"

        # Toggle back to clean view
        await toggle_switch("toggle-test-keys", False)

        clean_again_rows = await page.locator("#keys-tbody tr").count()
        assert clean_again_rows == 2, f"Expected 2 real API keys after toggle back, got {clean_again_rows}"

        # Capture API Keys Screenshot
        keys_shot = ARTIFACT_DIR / "admin_api_keys_1440x900.png"
        await page.screenshot(path=str(keys_shot))
        print(f"Saved: {keys_shot.name}")

        await browser.close()

    # -------------------------------------------------------------
    # Console and CSP Verification
    # -------------------------------------------------------------
    print("\n--- Verifying Console & CSP Errors ---")
    if csp_violations:
        print(f"FAIL: {len(csp_violations)} CSP violations detected:")
        for v in csp_violations:
            print("  ", v)
    else:
        print("PASS: 0 CSP violations detected.")

    if console_errors:
        print(f"FAIL: {len(console_errors)} Console errors detected:")
        for e in console_errors:
            print("  ", e)
    else:
        print("PASS: 0 Console errors detected.")

    assert len(csp_violations) == 0, f"{len(csp_violations)} CSP violations found!"
    assert len(console_errors) == 0, f"{len(console_errors)} Console errors found!"
    print("\n=== ALL ACCEPTANCE CRITERIA PASSED SUCCESSFULLY! ===")

if __name__ == "__main__":
    asyncio.run(main())
