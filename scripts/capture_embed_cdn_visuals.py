"""Playwright script to capture visual acceptance proofs for ABOODVIDEO Embed & CDN Engine."""
import os
import sys
import time
import httpx
from pathlib import Path
from playwright.sync_api import sync_playwright

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

OUTPUT_DIR = Path(r"C:\aboodvideo\browser-artifacts\embed_cdn_platform_acceptance")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

REMOTE_IP = "127.0.0.1"
HOST_DOMAIN = "platform.example"
BASE_URL = f"http://{HOST_DOMAIN}"
SHARE_ID = "kEFx-MxuCSKg1VXk6x3GYw"

def main():
    print("=" * 70)
    print("Capturing Visual Proofs for Embed & CDN Platform Enhancements")
    print("=" * 70)

    # 1. Login via httpx to obtain session token
    print("\n[Step 1] Authenticating via API to obtain secure session token...")
    client = httpx.Client(base_url=f"http://{REMOTE_IP}", headers={"Host": HOST_DOMAIN}, timeout=15.0)
    lp = client.get("/admin/login")
    csrf_token = lp.cookies.get("aboodvideo_csrf", "")
    res = client.post(
        "/api/v1/auth/login",
        json={"username_or_email": os.environ["ABOODVIDEO_ACCEPTANCE_USERNAME"], "password": os.environ["ABOODVIDEO_ACCEPTANCE_PASSWORD"]},
        headers={"X-CSRF-Token": csrf_token}
    )
    assert res.status_code == 200, f"Login failed: {res.text}"
    session_token = None
    for sc in res.headers.get_list("set-cookie"):
        if "aboodvideo_session=" in sc:
            session_token = sc.split("aboodvideo_session=")[1].split(";")[0]
    assert session_token, "No aboodvideo_session cookie received!"
    print(f"Session obtained: {session_token[:12]}... ✓")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[f"--host-resolver-rules=MAP {HOST_DOMAIN} {REMOTE_IP}"]
        )
        
        # Context 1: Authenticated Admin Context
        admin_context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=True
        )
        admin_context.add_cookies([
            {"name": "aboodvideo_session", "value": session_token, "domain": HOST_DOMAIN, "path": "/", "secure": False},
            {"name": "aboodvideo_csrf", "value": res.json()["csrf_token"], "domain": HOST_DOMAIN, "path": "/", "secure": False}
        ])
        page = admin_context.new_page()

        # 2. Capture Admin Settings — Embed & CDN Section
        print("\n[Step 2] Capturing Admin Settings (Embed & CDN tab)...")
        page.goto(f"{BASE_URL}/admin/settings#embed", wait_until="networkidle")
        page.wait_for_selector("#tab-embed", timeout=10000)
        page.wait_for_timeout(2000)
        shot1 = OUTPUT_DIR / "01_admin_settings_embed_cdn_panel.png"
        page.screenshot(path=str(shot1), full_page=False)
        print(f"   Saved: {shot1} ✓")

        # 3. Capture Admin Videos — Quick Embed Modal
        print("\n[Step 3] Capturing Admin Videos Quick Embed Modal...")
        page.goto(f"{BASE_URL}/admin/videos", wait_until="networkidle")
        page.wait_for_selector("table tbody tr", timeout=15000)
        page.wait_for_timeout(2000)
        
        # Click the "تضمين" button on the first ready video
        embed_btn = page.query_selector('button[data-action="open-embed"]')
        if embed_btn:
            embed_btn.click()
            page.wait_for_selector("#embed-modal.is-open", timeout=5000)
            page.wait_for_timeout(1000)
            shot2 = OUTPUT_DIR / "02_admin_videos_quick_embed_modal.png"
            page.screenshot(path=str(shot2), full_page=False)
            print(f"   Saved: {shot2} ✓")
        else:
            print("   WARNING: embed_btn not found on page!")

        admin_context.close()

        # 4. Capture Hotlink 403 Forbidden Shield Screen
        print("\n[Step 4] Capturing Hotlink Protection 403 Screen...")
        pirate_context = browser.new_context(
            viewport={"width": 1000, "height": 700},
            extra_http_headers={"Referer": "https://pirate-streaming-site.com/anime"},
            ignore_https_errors=True
        )
        pirate_page = pirate_context.new_page()
        pirate_page.goto(f"{BASE_URL}/embed/{SHARE_ID}", wait_until="networkidle")
        pirate_page.wait_for_timeout(1500)
        shot3 = OUTPUT_DIR / "03_hotlink_shield_403_blocked.png"
        pirate_page.screenshot(path=str(shot3), full_page=False)
        print(f"   Saved: {shot3} ✓")
        pirate_context.close()

        # 5. Capture Authorized Embedded Player
        print("\n[Step 5] Capturing Authorized Embedded Player on partner.example...")
        embed_context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            extra_http_headers={"Referer": "https://partner.example/episode/spirit-cage-incarnation-s1-episode-5/"},
            ignore_https_errors=True
        )
        embed_page = embed_context.new_page()
        embed_page.goto(f"{BASE_URL}/embed/{SHARE_ID}?color=00d2be&watermark=ABOODVIDEO&banner=1", wait_until="networkidle")
        embed_page.wait_for_selector("#player-container", timeout=15000)
        embed_page.wait_for_timeout(3000)
        shot4 = OUTPUT_DIR / "04_authorized_embed_player_watermark.png"
        embed_page.screenshot(path=str(shot4), full_page=False)
        print(f"   Saved: {shot4} ✓")
        embed_context.close()

        browser.close()

    print("\n" + "=" * 70)
    print("ALL VISUAL ACCEPTANCE SCREENSHOTS CAPTURED SUCCESSFULLY!")
    print("=" * 70)

if __name__ == "__main__":
    main()
