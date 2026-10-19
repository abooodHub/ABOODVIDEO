"""Capture real production screenshots from VMware server http://127.0.0.1/preview."""
import os
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

ARTIFACTS_DIR = Path(r"C:\Users\abood\.gemini\antigravity\brain\dce23a9a-79eb-4d6e-8726-1e716ffab914")
BASE_URL = "http://127.0.0.1/preview"
WATCH_URL = f"{BASE_URL}/watch/ZdEVBfy5caKNY0e8mzbhRA"
TITLE_URL = f"{BASE_URL}/title/ZdEVBfy5caKNY0e8mzbhRA"

VIEWPORTS = [
    ("desktop_1920", 1920, 1080),
    ("desktop_1440", 1440, 900),
    ("desktop_1280", 1280, 720),
    ("tablet_768", 768, 1024),
    ("mobile_390", 390, 844),
]

def main():
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    print("Launching Chromium...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            locale="ar-SA",
        )

        page = context.new_page()

        console_errors = []
        failed_requests = []

        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("requestfailed", lambda req: failed_requests.append(f"{req.method} {req.url}: {req.failure}"))

        print("\n--- Testing & Capturing Viewports ---")

        for vp_name, width, height in VIEWPORTS:
            print(f"\n[Viewport: {vp_name} ({width}x{height})]")
            page.set_viewport_size({"width": width, "height": height})

            # 1. Homepage
            print("  Visiting Homepage...")
            page.goto(f"{BASE_URL}/", wait_until="networkidle", timeout=20000)
            page.wait_for_timeout(500)
            home_shot = ARTIFACTS_DIR / f"live_preview_home_{vp_name}.png"
            page.screenshot(path=str(home_shot), full_page=False)
            print(f"  Captured: {home_shot.name}")

            # Verify image natural widths on homepage
            broken_imgs = page.evaluate("""() => {
                const imgs = Array.from(document.querySelectorAll('img'));
                return imgs.filter(img => img.naturalWidth === 0).map(img => img.src);
            }""")
            if broken_imgs:
                print(f"  WARNING: Broken images detected on homepage ({vp_name}): {broken_imgs}")
            else:
                print(f"  [OK] 0 broken images on homepage ({vp_name})")

            # Check horizontal overflow
            overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
            print(f"  Horizontal overflow: {overflow}px (allowed <= 0)")

            # 2. Browse page
            print("  Visiting Browse Catalog...")
            page.goto(f"{BASE_URL}/browse", wait_until="networkidle", timeout=20000)
            page.wait_for_timeout(500)
            browse_shot = ARTIFACTS_DIR / f"live_preview_browse_{vp_name}.png"
            page.screenshot(path=str(browse_shot), full_page=False)
            print(f"  Captured: {browse_shot.name}")

            # Verify image natural widths on browse page
            broken_browse = page.evaluate("""() => {
                const imgs = Array.from(document.querySelectorAll('img'));
                return imgs.filter(img => img.naturalWidth === 0).map(img => img.src);
            }""")
            if broken_browse:
                print(f"  WARNING: Broken images detected on browse ({vp_name}): {broken_browse}")
            else:
                print(f"  [OK] 0 broken images on browse ({vp_name})")

            # 3. Watch page
            print("  Visiting Watch Page...")
            page.goto(WATCH_URL, wait_until="networkidle", timeout=20000)
            page.wait_for_timeout(500)
            watch_shot = ARTIFACTS_DIR / f"live_preview_watch_{vp_name}.png"
            page.screenshot(path=str(watch_shot), full_page=False)
            print(f"  Captured: {watch_shot.name}")

        # Extra: Title details view on 1440
        print("\n[Extra: Title Details & Mobile Drawer]")
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(TITLE_URL, wait_until="networkidle", timeout=20000)
        page.wait_for_timeout(500)
        title_shot = ARTIFACTS_DIR / "live_preview_title_desktop_1440.png"
        page.screenshot(path=str(title_shot), full_page=False)
        print(f"  Captured: {title_shot.name}")

        # Mobile drawer toggle
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{BASE_URL}/", wait_until="networkidle", timeout=20000)
        page.locator("#viewer-hamburger-btn").click()
        page.wait_for_timeout(400)
        drawer_shot = ARTIFACTS_DIR / "live_preview_mobile_drawer_390.png"
        page.screenshot(path=str(drawer_shot), full_page=False)
        print(f"  Captured: {drawer_shot.name}")

        print("\n--- Summary of Live Capture ---")
        print(f"Total Console Errors: {len(console_errors)}")
        if console_errors:
            for err in console_errors[:5]:
                print(f"  Console error: {err}")
        print(f"Total Failed Requests: {len(failed_requests)}")
        if failed_requests:
            for fr in failed_requests[:5]:
                print(f"  Failed request: {fr}")

        browser.close()
        print("\nFinished successfully!")

if __name__ == "__main__":
    main()
