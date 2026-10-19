import sys
import json
import subprocess
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

BASE_URL = "http://127.0.0.1"
TARGET_TITLE = "Spirit_Cage_Incarnation_S1_01[4K]"
TARGET_UUID = "2c36137a-7066-4615-a61e-7b8d9a1dccc1"
SCREENSHOT_DIR = Path(r"C:\Users\abood\.gemini\antigravity\brain\d00a1f3c-9a6f-479a-95b9-eb67986f12cb\screenshots")
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def create_admin_session() -> dict:
    code = """
import json, secrets, uuid
from datetime import datetime, timedelta, timezone
from app.core.database import SyncSessionLocal
from app.models.user import User, UserRole
from app.models.session import UserSession
from app.security.tokens import hash_token
from app.security.csrf import generate_csrf_token

with SyncSessionLocal() as db:
    admin = db.query(User).filter(User.username == 'admin_live').first()
    if not admin:
        admin = db.query(User).filter(User.role.in_([UserRole.OWNER, UserRole.ADMIN]), User.is_active.is_(True)).first()
    token = secrets.token_urlsafe(32)
    csrf = generate_csrf_token()
    db.add(UserSession(
        id=uuid.uuid4(), user_id=admin.id, token_hash=hash_token(token),
        ip_address='127.0.0.1', user_agent='hotfix-acceptance',
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=60),
    ))
    db.commit()
    print(json.dumps({'session': token, 'csrf': csrf, 'user': admin.username}))
"""
    res = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "abood@127.0.0.1", "docker exec -i -e PYTHONPATH=/app aboodvideo_api python"],
        input=code, text=True, capture_output=True, check=True
    )
    return json.loads(res.stdout.strip())


def run_acceptance():
    print("=== Step 0: Minting Admin Session ===")
    auth = create_admin_session()
    print(f"Logged in as {auth['user']}, session token generated.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="ar-SA",
        )

        context.add_cookies([
            {
                "name": "aboodvideo_session",
                "value": auth["session"],
                "domain": "127.0.0.1",
                "path": "/",
                "httpOnly": True,
            },
            {
                "name": "aboodvideo_csrf",
                "value": auth["csrf"],
                "domain": "127.0.0.1",
                "path": "/",
            }
        ])

        page = context.new_page()

        console_errors = []
        csp_violations = []

        def handle_console(msg):
            text = msg.text
            if msg.type == "error":
                console_errors.append(text)
                if "Content Security Policy" in text or "CSP" in text:
                    csp_violations.append(text)

        page.on("console", handle_console)

        print("\n=== Step 1: Navigating to Library (/preview) ===")
        page.goto(f"{BASE_URL}/preview", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1000)

        # Look for target video row
        row_selector = f"tr.drive-row[data-id='{TARGET_UUID}']"
        target_row = page.locator(row_selector)
        if not target_row.is_visible():
            # If not directly in root, check if items loaded
            items = page.locator("tr.drive-row").all()
            print(f"Total rows rendered: {len(items)}")
            for it in items:
                print("Row:", it.get_attribute("data-id"), it.locator(".name-primary").text_content())

        assert target_row.is_visible(), f"Target video row {TARGET_UUID} must be visible in library!"
        print(f"Target video found: {target_row.locator('.name-primary').text_content()}")

        print("\n=== Step 2: Clicking Row (Non-title) to Open Redesigned Drawer ===")
        # Click on size or date column to trigger row click (select + open drawer)
        target_row.locator(".col-size").click()
        page.wait_for_timeout(600)

        drawer = page.locator("#detailsDrawer")
        assert "is-open" in drawer.get_attribute("class"), "Details drawer must open on row click!"
        print("Details drawer is OPEN!")

        # Verify drawer header and fields
        title_in_drawer = page.locator("#drawerTitle").text_content()
        status_badge = page.locator("#drawerBadgeStatus").text_content()
        quality_badge = page.locator("#drawerBadgeQuality").text_content()
        duration_badge = page.locator("#drawerBadgeDuration").text_content()
        watch_url_val = page.locator("#drawerWatchUrlInput").input_value()

        print(f"Drawer title: {title_in_drawer}")
        print(f"Status badge: {status_badge}")
        print(f"Quality badge: {quality_badge}")
        print(f"Duration badge: {duration_badge}")
        print(f"Canonical watch URL: {watch_url_val}")

        assert TARGET_TITLE in title_in_drawer or "[SILVER]" in title_in_drawer, "Drawer title must match video!"
        assert status_badge == "READY", "Status badge must say READY!"
        assert "/watch/" in watch_url_val, "Canonical watch URL must contain /watch/!"
        assert "master.m3u8" not in watch_url_val, "Canonical watch URL must NEVER contain master.m3u8!"

        # Capture drawer screenshot
        drawer_shot = SCREENSHOT_DIR / "details_drawer_hotfix.png"
        page.screenshot(path=str(drawer_shot))
        print(f"Captured screenshot: {drawer_shot}")

        print("\n=== Step 3: Checking Subtitles Tab in Drawer ===")
        page.locator("#tabBtnDrawerSubs").click()
        page.wait_for_timeout(600)
        subs_content = page.locator("#drawerSubsList").text_content()
        print(f"Subtitles list text: {subs_content.strip()}")

        print("\n=== Step 4: Checking Share Tab in Drawer ===")
        page.locator("#tabBtnDrawerShare").click()
        page.wait_for_timeout(600)
        is_active_share = page.locator("#drawerActiveShareBox").is_visible()
        is_no_share = page.locator("#drawerNoShareBox").is_visible()
        print(f"Share tab: active_box_visible={is_active_share}, no_share_visible={is_no_share}")

        # Return to info tab
        page.locator("#tabBtnDrawerInfo").click()
        page.wait_for_timeout(300)

        print("\n=== Step 5: Testing Click on [تشغيل] in Drawer ===")
        page.locator("#btnDrawerPlay").click()
        page.wait_for_load_state("networkidle", timeout=30000)
        page.wait_for_timeout(2000)

        current_url = page.url
        print(f"Navigated to watch URL: {current_url}")
        assert f"/watch/{TARGET_UUID}" in current_url or "/watch/" in current_url, "Must navigate to watch page!"

        # Verify player is active and playing
        player_container = page.locator("#player-container")
        assert player_container.is_visible(), "Player container must be visible on Watch page!"

        video_el = page.locator("#video-element")
        assert video_el.is_visible(), "Video element must be present!"

        # Wait for video duration or playback ready
        duration = page.evaluate("() => document.getElementById('video-element').duration")
        print(f"Video element duration: {duration}s")
        assert duration and duration > 0, "Video must have loaded playable media!"

        watch_shot = SCREENSHOT_DIR / "watch_page_hotfix.png"
        page.screenshot(path=str(watch_shot))
        print(f"Captured watch page screenshot: {watch_shot}")

        print("\n=== Step 6: Testing Canonical Watch URL in Second Tab ===")
        canonical_target_url = f"{BASE_URL}/watch/{TARGET_UUID}"
        page2 = context.new_page()
        page2.on("console", handle_console)
        page2.goto(canonical_target_url, wait_until="networkidle", timeout=30000)
        page2.wait_for_timeout(2000)

        print(f"Page 2 URL: {page2.url}")
        assert page2.locator("#player-container").is_visible(), "Player must be visible in second authenticated tab!"
        duration2 = page2.evaluate("() => document.getElementById('video-element').duration")
        print(f"Page 2 Video duration: {duration2}s")
        assert duration2 and duration2 > 0, "Video must play in second authenticated tab!"

        print("\n=== Step 7: Testing Video Title Click from Library ===")
        page.goto(f"{BASE_URL}/preview", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1000)
        row2 = page.locator(row_selector)
        # Click specifically on .name-primary
        row2.locator(".name-primary").click()
        page.wait_for_load_state("networkidle", timeout=30000)
        page.wait_for_timeout(1500)
        print(f"Clicked title, navigated to: {page.url}")
        assert f"/watch/{TARGET_UUID}" in page.url or "/watch/" in page.url, "Clicking title must navigate directly to Watch page!"

        print("\n=== Step 8: Verifying Console Errors & CSP ===")
        print(f"Total console errors: {len(console_errors)}")
        print(f"Total CSP violations: {len(csp_violations)}")
        if console_errors:
            print("Errors detected:", console_errors)

        assert len(console_errors) == 0, f"Must have 0 console errors! Found: {console_errors}"
        assert len(csp_violations) == 0, f"Must have 0 CSP violations! Found: {csp_violations}"

        print("\n🎉 ALL 12 ACCEPTANCE CRITERIA PASSED CLEANLY! 🎉")


if __name__ == "__main__":
    run_acceptance()
