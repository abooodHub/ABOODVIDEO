"""Capture live screenshots of ABOODVIDEO Drive & File Manager from VMware VM."""
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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            locale="ar-SA",
        )
        page = context.new_page()

        console_errors = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        # -------------------------------------------------------------
        # 1. Desktop Viewport (1440x900) - Root Library View
        # -------------------------------------------------------------
        print("\n[1] Capturing Desktop Root View (1440x900)...")
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(f"{BASE_URL}/", wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(1000)

        shot_root = ARTIFACTS_DIR / "drive_desktop_1440x900_root.png"
        page.screenshot(path=str(shot_root), full_page=False)
        print(f"Captured: {shot_root.name}")

        # -------------------------------------------------------------
        # 2. Desktop Viewport (1440x900) - Season 1 View (Donghua -> Spirit Cage -> الموسم 1)
        # -------------------------------------------------------------
        print("\n[2] Navigating to Donghua -> Spirit Cage -> الموسم 1...")
        # Click on Donghua folder in table
        donghua_cell = page.locator("text='Donghua'").first
        if donghua_cell.is_visible():
            donghua_cell.click()
            page.wait_for_timeout(800)

            # Click on Spirit Cage
            sc_cell = page.locator("text='Spirit Cage'").first
            if sc_cell.is_visible():
                sc_cell.click()
                page.wait_for_timeout(800)

                # Click on الموسم 1
                s1_cell = page.locator("text='الموسم 1'").first
                if s1_cell.is_visible():
                    s1_cell.click()
                    page.wait_for_timeout(800)

        shot_season = ARTIFACTS_DIR / "drive_desktop_1440x900_season.png"
        page.screenshot(path=str(shot_season), full_page=False)
        print(f"Captured: {shot_season.name}")

        # -------------------------------------------------------------
        # 3. Desktop Viewport (1440x900) - Context Menu open on video
        # -------------------------------------------------------------
        print("\n[3] Opening Context Menu on video...")
        kebab_btn = page.locator(".btn-kebab").first
        if kebab_btn.is_visible():
            kebab_btn.click()
            page.wait_for_timeout(500)

        shot_menu = ARTIFACTS_DIR / "drive_desktop_1440x900_context_menu.png"
        page.screenshot(path=str(shot_menu), full_page=False)
        print(f"Captured: {shot_menu.name}")

        # -------------------------------------------------------------
        # 4. Mobile Viewport (390x844) - Mobile View
        # -------------------------------------------------------------
        print("\n[4] Capturing Mobile Viewport (390x844)...")
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{BASE_URL}/", wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(1000)

        shot_mobile = ARTIFACTS_DIR / "drive_mobile_390x844.png"
        page.screenshot(path=str(shot_mobile), full_page=False)
        print(f"Captured: {shot_mobile.name}")

        browser.close()

    if console_errors:
        print(f"\nBrowser Console Errors: {console_errors}")
    else:
        print("\nZero console errors detected!")

if __name__ == "__main__":
    main()
