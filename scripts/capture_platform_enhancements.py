"""Capture visual proof for all new platform features."""
import sys
import json
import subprocess
from pathlib import Path
from playwright.sync_api import sync_playwright

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

EVIDENCE_DIR = Path(r"C:\aboodvideo\browser-artifacts\platform_features_acceptance")
SHARE_ID = "kEFx-MxuCSKg1VXk6x3GYw"

def main():
    print("=" * 70)
    print("Capturing Visual Proof for All New Platform Enhancements")
    print("=" * 70)

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=True
        )

        # 1. Capture Unauthorized Domain Shield Screen (Hotlink Protection)
        print("\n[Shot 1] Testing unauthorized pirate site referer...")
        page_pirate = context.new_page()
        # Set extra HTTP headers to simulate referer from unauthorized site
        page_pirate.set_extra_http_headers({"Referer": "https://fake-pirate-site.net/watch"})
        page_pirate.goto(f"https://platform.example/embed/{SHARE_ID}", wait_until="domcontentloaded")
        page_pirate.screenshot(path=str(EVIDENCE_DIR / "01_hotlink_protection_blocked.png"))
        print("Captured 01_hotlink_protection_blocked.png ✓")
        page_pirate.close()

        # 2. Capture Custom Styled Embed Player (Turquoise #00d2be + Custom Watermark)
        print("\n[Shot 2] Testing custom embed styling (?color=00d2be&watermark=ANIME_PORTAL)...")
        page_custom = context.new_page()
        page_custom.goto(
            f"https://platform.example/embed/{SHARE_ID}?color=00d2be&watermark=ANIME_PORTAL&banner=1",
            wait_until="domcontentloaded"
        )
        page_custom.wait_for_selector("#player-container", timeout=15000)
        page_custom.screenshot(path=str(EVIDENCE_DIR / "02_custom_player_color_and_watermark.png"))
        print("Captured 02_custom_player_color_and_watermark.png ✓")
        page_custom.close()

        browser.close()

    print("\n" + "=" * 70)
    print("ALL SCREENSHOTS CAPTURED SUCCESSFULLY!")
    print("=" * 70)

if __name__ == "__main__":
    main()
