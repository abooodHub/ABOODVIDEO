"""Verification script for ABOODVIDEO DRIVE — PROFESSIONAL LIBRARY REDESIGN V5.
Captures all 6 required verification screenshots, checks console errors & CSP violations.
"""
import os
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding='utf-8')

ARTIFACTS_DIR = Path(r"C:\Users\abood\.gemini\antigravity\brain\0181878d-15d4-4078-9f16-05b68b1ff8e2")
BASE_URL = "http://127.0.0.1/preview"

def main():
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Artifacts output directory: {ARTIFACTS_DIR}")

    console_errors = []
    csp_violations = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            locale="ar-SA",
        )
        page = context.new_page()

        def handle_console(msg):
            text = msg.text
            if msg.type == "error":
                console_errors.append(text)
                if "Content Security Policy" in text or "violates" in text.lower():
                    csp_violations.append(text)
            elif "violates" in text.lower() or "csp" in text.lower():
                csp_violations.append(text)

        page.on("console", handle_console)

        # -------------------------------------------------------------
        # 1. Desktop Viewport (1440x900) - Root Library View
        # -------------------------------------------------------------
        print("\n[1] Capturing drive_v5_root_1440.png...")
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(f"{BASE_URL}/", wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(1000)

        shot_root = ARTIFACTS_DIR / "drive_v5_root_1440.png"
        page.screenshot(path=str(shot_root), full_page=False)
        print(f"Captured: {shot_root.name}")

        # -------------------------------------------------------------
        # 2. Desktop Viewport (1440x900) - Folder View (Donghua)
        # -------------------------------------------------------------
        print("\n[2] Navigating to Donghua folder and capturing drive_v5_folder_1440.png...")
        donghua_row = page.locator("tr.drive-row:has-text('Donghua')").first
        if donghua_row.is_visible():
            donghua_row.click()
            page.wait_for_timeout(1000)

        shot_folder = ARTIFACTS_DIR / "drive_v5_folder_1440.png"
        page.screenshot(path=str(shot_folder), full_page=False)
        print(f"Captured: {shot_folder.name}")

        # -------------------------------------------------------------
        # 3. Desktop Viewport (1440x900) - Context Menu with Red Trashing
        # -------------------------------------------------------------
        print("\n[3] Opening Context Menu and capturing drive_v5_context_menu_1440.png...")
        dots_btn = page.locator(".action-dots-btn").first
        if dots_btn.is_visible():
            dots_btn.click()
            page.wait_for_timeout(600)

        shot_ctx = ARTIFACTS_DIR / "drive_v5_context_menu_1440.png"
        page.screenshot(path=str(shot_ctx), full_page=False)
        print(f"Captured: {shot_ctx.name}")

        # Close context menu
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

        # -------------------------------------------------------------
        # 4. Desktop Viewport (1440x900) - Bulk Selection Action Bar
        # -------------------------------------------------------------
        print("\n[4] Selecting items and capturing drive_v5_bulk_selection_1440.png...")
        first_check = page.locator(".drive-table tbody .drive-checkbox").first
        if first_check.is_visible():
            first_check.check()
            page.wait_for_timeout(500)

        shot_bulk = ARTIFACTS_DIR / "drive_v5_bulk_selection_1440.png"
        page.screenshot(path=str(shot_bulk), full_page=False)
        print(f"Captured: {shot_bulk.name}")

        # Uncheck selection
        page.evaluate("() => document.getElementById('btnCancelSelection').click()")
        page.wait_for_timeout(300)

        # -------------------------------------------------------------
        # 5. Desktop Viewport (1440x900) - Trash View with item
        # -------------------------------------------------------------
        print("\n[5] Navigating to Trash and capturing drive_v5_trash_1440.png...")
        # Create a temporary folder to trash so trash view demonstrates items
        temp_create = page.evaluate("""async () => {
            const res = await fetch('/api/v1/drive/folders', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ name: 'ملف تجريبي للسلة' })
            });
            const d = await res.json();
            if (d.folder) {
                await fetch('/api/v1/drive/trash', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ items: [{ id: d.folder.id, type: 'folder' }] })
                });
                return d.folder.id;
            }
            return null;
        }""")

        trash_nav = page.locator("#navTrashLink")
        if trash_nav.is_visible():
            trash_nav.click()
            page.wait_for_timeout(1000)

        shot_trash = ARTIFACTS_DIR / "drive_v5_trash_1440.png"
        page.screenshot(path=str(shot_trash), full_page=False)
        print(f"Captured: {shot_trash.name}")

        # Cleanup: permanently delete temporary folder from trash
        if temp_create:
            page.evaluate(f"""async () => {{
                await fetch('/api/v1/drive/trash/delete-permanent', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{ items: [{{ id: '{temp_create}', type: 'folder' }}] }})
                }});
            }}""")

        # -------------------------------------------------------------
        # 6. Mobile Viewport (390x844) - Mobile Responsiveness
        # -------------------------------------------------------------
        print("\n[6] Switching to Mobile (390x844) and capturing drive_v5_mobile_390.png...")
        page.goto(f"{BASE_URL}/", wait_until="networkidle", timeout=15000)
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(1000)

        shot_mobile = ARTIFACTS_DIR / "drive_v5_mobile_390.png"
        page.screenshot(path=str(shot_mobile), full_page=False)
        print(f"Captured: {shot_mobile.name}")

        # Check horizontal overflow on mobile
        scroll_width = page.evaluate("() => document.documentElement.scrollWidth")
        client_width = page.evaluate("() => document.documentElement.clientWidth")
        print(f"Mobile Scroll Width: {scroll_width}px, Client Width: {client_width}px")
        if scroll_width > client_width:
            print(f"WARNING: Horizontal scroll detected on mobile: {scroll_width} > {client_width}")
        else:
            print("OK: Zero horizontal overflow on mobile (390px).")

        browser.close()

    print("\n" + "=" * 50)
    print("VERIFICATION RESULTS SUMMARY")
    print("=" * 50)
    print(f"Console Errors: {len(console_errors)}")
    for err in console_errors:
        print(f"  - {err}")
    print(f"CSP Violations: {len(csp_violations)}")
    for v in csp_violations:
        print(f"  - {v}")
    print("=" * 50)

if __name__ == "__main__":
    main()
