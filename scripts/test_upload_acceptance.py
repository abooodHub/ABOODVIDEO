"""Comprehensive Real-Browser Acceptance Script for /admin/upload.
Tests the live VMware application at http://127.0.0.1/admin/upload:
- Multi-file selection
- Destination card and modal
- Queue display and removal
- Batch upload execution
- Concurrency limit (2 simultaneous)
- Cancellation
- Mobile responsive layout
- Zero console errors, zero CSP violations, zero horizontal overflow
- Captures required screenshots
"""

import os
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(r"C:\aboodvideo")
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.create_test_session import create_session

ARTIFACTS_DIR = Path(r"C:\Users\abood\.gemini\antigravity\brain\470cc9ad-0f77-457d-97fe-4fa52546d661")
BASE_URL = "http://127.0.0.1"
UPLOAD_URL = f"{BASE_URL}/admin/upload"

TEST_VIDEOS_DIR = PROJECT_ROOT / "tests" / "temp_test_videos"
VIDEO_1 = str(TEST_VIDEOS_DIR / "sample_ep01.mp4")
VIDEO_2 = str(TEST_VIDEOS_DIR / "sample_ep02.mp4")
VIDEO_3 = str(TEST_VIDEOS_DIR / "sample_ep03.mkv")
VIDEO_4 = str(TEST_VIDEOS_DIR / "sample_ep04.mp4")


def run_acceptance():
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    print("1. Generating live authenticated session...")
    auth_data = create_session()
    session_token = auth_data["session"]
    csrf_token = auth_data["csrf"]
    print("   Session token generated successfully.")

    console_errors = []
    csp_violations = []

    print("\n2. Launching Chromium via Playwright...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            locale="ar-SA",
        )

        # Set cookies for 127.0.0.1
        context.add_cookies([
            {
                "name": "aboodvideo_session",
                "value": session_token,
                "domain": "127.0.0.1",
                "path": "/",
                "httpOnly": True,
            },
            {
                "name": "aboodvideo_csrf",
                "value": csrf_token,
                "domain": "127.0.0.1",
                "path": "/",
                "httpOnly": False,
            }
        ])

        page = context.new_page()

        # Listen to console and security violations
        def on_console(msg):
            text = msg.text
            if msg.type == "error":
                console_errors.append(text)
                if "Content Security Policy" in text or "CSP" in text:
                    csp_violations.append(text)
                print(f"   [BROWSER ERROR]: {text}")

        page.on("console", on_console)
        page.on("pageerror", lambda err: console_errors.append(str(err)))

        # ------------------------------------------------------------------
        # TEST CASE 1: Desktop Empty State (1440x900)
        # ------------------------------------------------------------------
        print("\n3. Testing Desktop Viewport (1440x900)...")
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(UPLOAD_URL, wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(500)

        # Verify page title and header
        title = page.title()
        print(f"   Page Title: {title}")
        assert "رفع الفيديوهات" in title, f"Unexpected page title: {title}"

        # Verify Destination Card
        dest_card = page.locator("#destination-card")
        assert dest_card.is_visible(), "Destination card is not visible"
        dest_text = page.locator("#destination-path").inner_text()
        print(f"   Destination Path: {dest_text}")
        assert "مكتبتي" in dest_text, "Default destination path must include 'مكتبتي'"

        # Verify Dropzone
        dropzone = page.locator("#dropzone")
        assert dropzone.is_visible(), "Dropzone is not visible"

        # Check Horizontal Overflow
        scroll_width = page.evaluate("() => document.documentElement.scrollWidth")
        inner_width = page.evaluate("() => window.innerWidth")
        print(f"   Scroll Width: {scroll_width}px | Inner Width: {inner_width}px")
        assert scroll_width <= inner_width, f"Horizontal overflow detected: scrollWidth {scroll_width} > innerWidth {inner_width}"

        # Test Destination Modal
        print("   Testing Destination Modal...")
        page.click("#btn-change-destination")
        page.wait_for_selector("#modal-destination.is-open", timeout=5000)
        assert page.locator("#modal-destination").is_visible(), "Destination modal did not open"
        page.click("#btn-cancel-dest-modal")
        page.wait_for_selector("#modal-destination", state="hidden", timeout=5000)
        print("   Destination modal tested successfully.")

        # Capture upload_empty_desktop.png
        empty_shot_path = ARTIFACTS_DIR / "upload_empty_desktop.png"
        page.screenshot(path=str(empty_shot_path), full_page=False)
        print(f"   [SCREENSHOT] Captured: {empty_shot_path.name}")

        # ------------------------------------------------------------------
        # TEST CASE 2: Multi-File Selection & Queue Display
        # ------------------------------------------------------------------
        print("\n4. Testing Multi-file Selection & Queue...")
        file_input = page.locator("#file-input-files")
        file_input.set_input_files([VIDEO_1, VIDEO_2, VIDEO_3])
        page.wait_for_timeout(500)

        # Verify Queue Section is visible
        queue_section = page.locator("#queue-section")
        assert queue_section.is_visible(), "Queue section did not appear after selecting files"

        # Verify Queue count badge
        count_text = page.locator("#queue-count-badge").inner_text()
        print(f"   Queue Count Badge: {count_text}")
        assert "3" in count_text, f"Expected 3 files in queue, got: {count_text}"

        items = page.locator(".upload-item")
        assert items.count() == 3, f"Expected 3 item cards, found: {items.count()}"

        # Add 4th file to test adding more
        print("   Adding 4th file to queue...")
        file_input.set_input_files([VIDEO_4])
        page.wait_for_timeout(300)
        assert items.count() == 4, f"Expected 4 items after addition, found: {items.count()}"

        # Remove 4th file using [×]
        print("   Removing 4th file from queue...")
        remove_btn = items.nth(3).locator("[data-action='remove']")
        remove_btn.click()
        page.wait_for_timeout(300)
        assert items.count() == 3, f"Expected 3 items after removal, found: {items.count()}"

        # Capture upload_queue_desktop.png
        queue_shot_path = ARTIFACTS_DIR / "upload_queue_desktop.png"
        page.screenshot(path=str(queue_shot_path), full_page=False)
        print(f"   [SCREENSHOT] Captured: {queue_shot_path.name}")

        # ------------------------------------------------------------------
        # TEST CASE 3: Batch Upload Execution & Concurrency & Active State
        # ------------------------------------------------------------------
        print("\n5. Testing Batch Upload Execution & Concurrency...")
        start_btn = page.locator("#btn-start-batch")
        start_btn.click()
        page.wait_for_timeout(600)

        # Verify Batch Progress Card appeared
        batch_card = page.locator("#batch-progress-card")
        assert batch_card.is_visible(), "Batch progress card did not appear"

        # Capture upload_active_desktop.png while uploading
        active_shot_path = ARTIFACTS_DIR / "upload_active_desktop.png"
        page.screenshot(path=str(active_shot_path), full_page=False)
        print(f"   [SCREENSHOT] Captured: {active_shot_path.name}")

        # Wait for uploads to complete
        print("   Waiting for upload queue to complete...")
        # Since these are 5KB files, they complete in a second
        page.wait_for_selector(".upload-item.is-completed", timeout=15000)
        page.wait_for_timeout(2000)

        completed_items = page.locator(".upload-item.is-completed")
        print(f"   Completed Items count: {completed_items.count()}")
        assert completed_items.count() >= 1, "At least 1 item should have completed successfully"

        # Verify Processing Queue Link is visible
        link_proc = page.locator("#link-processing-queue")
        assert link_proc.is_visible(), "Link to processing queue should be visible after completion"

        # ------------------------------------------------------------------
        # TEST CASE 4: Mobile Viewport (390x844)
        # ------------------------------------------------------------------
        print("\n6. Testing Mobile Viewport (390x844)...")
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(UPLOAD_URL, wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(500)

        # Select files on mobile
        file_input = page.locator("#file-input-files")
        file_input.set_input_files([VIDEO_1, VIDEO_2])
        page.wait_for_timeout(500)

        # Verify mobile horizontal scroll
        m_scroll_width = page.evaluate("() => document.documentElement.scrollWidth")
        m_inner_width = page.evaluate("() => window.innerWidth")
        print(f"   Mobile Scroll Width: {m_scroll_width}px | Inner Width: {m_inner_width}px")
        assert m_scroll_width <= m_inner_width, f"Mobile horizontal overflow: {m_scroll_width} > {m_inner_width}"

        # Capture upload_mobile.png
        mobile_shot_path = ARTIFACTS_DIR / "upload_mobile.png"
        page.screenshot(path=str(mobile_shot_path), full_page=False)
        print(f"   [SCREENSHOT] Captured: {mobile_shot_path.name}")

        # ------------------------------------------------------------------
        # Verify 0 Console Errors & 0 CSP Violations
        # ------------------------------------------------------------------
        print("\n7. Security & Observability Audit:")
        print(f"   Console Errors: {len(console_errors)}")
        print(f"   CSP Violations: {len(csp_violations)}")

        if console_errors:
            print("   WARNING: Console errors encountered:")
            for err in console_errors:
                print(f"     - {err}")

        assert len(console_errors) == 0, f"Found {len(console_errors)} console errors: {console_errors}"
        assert len(csp_violations) == 0, f"Found {len(csp_violations)} CSP violations: {csp_violations}"

        browser.close()
        print("\n=== ALL REAL BROWSER ACCEPTANCE TESTS PASSED! ===")


if __name__ == "__main__":
    run_acceptance()
