import os
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

OUTPUT_DIR = Path(r"C:\aboodvideo\screenshots")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL = "http://127.0.0.1"

def run():
    print(f"Starting acceptance capture against {BASE_URL}...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # -------------------------------------------------------------
        # 1. Desktop Context (1440 x 900)
        # -------------------------------------------------------------
        desktop_ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="ar-SA",
        )
        page = desktop_ctx.new_page()

        console_errors = []
        csp_violations = []

        def on_console(msg):
            if msg.type == "error":
                console_errors.append(f"[{msg.type}] {msg.text}")

        page.on("console", on_console)

        # Login first to get session cookie for admin pages
        print("Logging in to Admin...")
        page.goto(f"{BASE_URL}/admin/login", wait_until="networkidle")
        page.fill("#username", "admin")
        page.fill("#password", "AdminPass2026!")
        page.click("#login-submit")
        page.wait_for_timeout(2000)

        desktop_targets = [
            ("01_dashboard", f"{BASE_URL}/admin"),
            ("02_library", f"{BASE_URL}/preview"),
            ("03_upload", f"{BASE_URL}/admin/upload"),
            ("04_processing", f"{BASE_URL}/admin/processing"),
            ("05_shares", f"{BASE_URL}/admin/shares"),
            ("06_api_keys", f"{BASE_URL}/admin/api-keys"),
            ("07_settings", f"{BASE_URL}/admin/settings"),
            ("08_public_home", f"{BASE_URL}/preview/home"),
            ("09_browse", f"{BASE_URL}/preview/catalog"),
            ("10_series", f"{BASE_URL}/preview/series/Spirit Cage"),
            ("11_watch", f"{BASE_URL}/admin/videos/2c36137a-7066-4615-a61e-7b8d9a1dccc1/watch"),
            ("12_video_unavailable", f"{BASE_URL}/preview/error/video-unavailable"),
            ("13_error_404", f"{BASE_URL}/preview/error/404"),
        ]

        overflow_issues = []

        for name, url in desktop_targets:
            print(f"Capturing Desktop 1440x900: {name} -> {url}")
            page.goto(url, wait_until="networkidle")
            page.wait_for_timeout(1500)
            
            # Check horizontal overflow
            scroll_w = page.evaluate("() => document.documentElement.scrollWidth")
            client_w = page.evaluate("() => document.documentElement.clientWidth")
            if scroll_w > client_w:
                overflow_issues.append(f"{name} has horizontal overflow: scrollWidth {scroll_w} > clientWidth {client_w}")

            save_path = OUTPUT_DIR / f"{name}_desktop_1440x900.png"
            page.screenshot(path=str(save_path), full_page=False)
            print(f"  Saved: {save_path.name}")

        desktop_ctx.close()

        # -------------------------------------------------------------
        # 2. Mobile Context (390 x 844, iPhone 12 / 13 / 14 equivalent)
        # -------------------------------------------------------------
        mobile_ctx = browser.new_context(
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
            locale="ar-SA",
        )
        mpage = mobile_ctx.new_page()
        mpage.on("console", on_console)

        # Login on mobile
        mpage.goto(f"{BASE_URL}/admin/login", wait_until="networkidle")
        mpage.fill("#username", "admin")
        mpage.fill("#password", "AdminPass2026!")
        mpage.click("#login-submit")
        mpage.wait_for_timeout(2000)

        mobile_targets = [
            ("14_public_home_mobile", f"{BASE_URL}/preview/home"),
            ("15_browse_mobile", f"{BASE_URL}/preview/catalog"),
            ("16_series_mobile", f"{BASE_URL}/preview/series/Spirit Cage"),
            ("17_watch_mobile", f"{BASE_URL}/admin/videos/2c36137a-7066-4615-a61e-7b8d9a1dccc1/watch"),
            ("18_dashboard_mobile", f"{BASE_URL}/admin"),
            ("19_shares_mobile", f"{BASE_URL}/admin/shares"),
        ]

        for name, url in mobile_targets:
            print(f"Capturing Mobile 390x844: {name} -> {url}")
            mpage.goto(url, wait_until="networkidle")
            mpage.wait_for_timeout(1500)

            scroll_w = mpage.evaluate("() => document.documentElement.scrollWidth")
            client_w = mpage.evaluate("() => document.documentElement.clientWidth")
            if scroll_w > client_w:
                overflow_issues.append(f"Mobile {name} has horizontal overflow: scrollWidth {scroll_w} > clientWidth {client_w}")

            save_path = OUTPUT_DIR / f"{name}_390x844.png"
            mpage.screenshot(path=str(save_path), full_page=False)
            print(f"  Saved: {save_path.name}")

        mobile_ctx.close()
        browser.close()

    print("\n--- Acceptance Audit Report ---")
    print(f"Total Screenshots: {len(desktop_targets) + len(mobile_targets)}")
    print(f"Console Errors: {len(console_errors)}")
    for err in console_errors:
        print(f"  {err}")
    print(f"Overflow Issues: {len(overflow_issues)}")
    for of in overflow_issues:
        print(f"  {of}")
    print("Done!")

if __name__ == "__main__":
    run()
