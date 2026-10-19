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
SCREENSHOT_DIRS = [
    Path(r"C:\Users\abood\.gemini\antigravity\brain\d00a1f3c-9a6f-479a-95b9-eb67986f12cb\screenshots"),
    Path(r"C:\aboodvideo\screenshots"),
]
for d in SCREENSHOT_DIRS:
    d.mkdir(parents=True, exist_ok=True)

def create_admin_session() -> dict:
    code = """
import json, secrets, uuid
from datetime import datetime, timedelta, timezone
from app.core.database import SyncSessionLocal
from app.models.user import User
from app.models.session import UserSession
from app.security.tokens import hash_token
from app.security.csrf import generate_csrf_token

with SyncSessionLocal() as db:
    admin = db.query(User).filter(User.username == 'admin_live').first()
    token = secrets.token_urlsafe(32)
    csrf = generate_csrf_token()
    db.add(UserSession(
        id=uuid.uuid4(), user_id=admin.id, token_hash=hash_token(token),
        ip_address='127.0.0.1', user_agent='library-v3-acceptance',
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

def save_screenshot(page, filename: str):
    for d in SCREENSHOT_DIRS:
        page.screenshot(path=str(d / filename))
    print(f"[CAPTURED] {filename}")

def run():
    print("=== Step 0: Minting Admin Session ===")
    auth = create_admin_session()
    print(f"Logged in as {auth['user']}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="ar-SA",
        )
        context.add_cookies([
            {"name": "aboodvideo_session", "value": auth["session"], "domain": "127.0.0.1", "path": "/", "httpOnly": True},
            {"name": "aboodvideo_csrf", "value": auth["csrf"], "domain": "127.0.0.1", "path": "/"},
        ])

        page = context.new_page()

        console_errors = []
        csp_violations = []

        def on_console(msg):
            txt = msg.text
            if msg.type == "error":
                console_errors.append(f"[{msg.location}] {txt}")
            if "content security policy" in txt.lower() or "csp" in txt.lower():
                csp_violations.append(f"[{msg.location}] {txt}")

        page.on("console", on_console)

        print("Navigating to /preview/...")
        page.goto(f"{BASE_URL}/preview/", wait_until="networkidle")
        page.wait_for_timeout(2000)

        # Check initial overflow
        overflow = page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth")
        print(f"Initial Horizontal Overflow: {overflow}")

        # Check rows
        rows = page.locator("#driveTableBody tr")
        row_count = rows.count()
        print(f"Found {row_count} rows in table")

        # SCENARIO 1: Empty-area main context menu
        print("--- Scenario 1: Empty Area Context Menu ---")
        page.locator(".table-container").click(button="right", position={"x": 550, "y": 480})
        page.wait_for_timeout(400)
        save_screenshot(page, "1_empty_area_menu.png")

        # SCENARIO 2: "جديد" Submenu
        print("--- Scenario 2: 'جديد' Submenu ---")
        empty_new = page.locator("#emptyCtxNew")
        if empty_new.is_visible():
            empty_new.hover()
            page.wait_for_timeout(400)
            save_screenshot(page, "2_new_submenu.png")
        else:
            print("WARNING: #emptyCtxNew not visible")

        # SCENARIO 3: "رفع" Submenu
        print("--- Scenario 3: 'رفع' Submenu ---")
        empty_upload = page.locator("#emptyCtxUpload")
        if empty_upload.is_visible():
            empty_upload.hover()
            page.wait_for_timeout(400)
            save_screenshot(page, "3_upload_submenu.png")
        else:
            print("WARNING: #emptyCtxUpload not visible")

        # Close context menu
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

        # SCENARIO 4: Video Context Menu
        print("--- Scenario 4: Video Context Menu ---")
        video_row = page.locator("tr[data-type='video']").first
        if video_row.count() > 0:
            video_row.click(button="right")
            page.wait_for_timeout(400)
            save_screenshot(page, "4_video_context_menu.png")
        else:
            print("WARNING: No video row found")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

        # SCENARIO 5: Folder Context Menu
        print("--- Scenario 5: Folder Context Menu ---")
        folder_row = page.locator("tr[data-type='folder']").first
        if folder_row.count() > 0:
            folder_row.click(button="right")
            page.wait_for_timeout(400)
            save_screenshot(page, "5_folder_context_menu.png")
        else:
            print("WARNING: No folder row found")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

        # SCENARIO 6: Multi-Selection Context Menu
        print("--- Scenario 6: Multi-Selection Context Menu ---")
        cbs = page.locator("#driveTableBody .drive-checkbox")
        print(f"Found {cbs.count()} drive checkboxes in table body")
        if cbs.count() >= 2:
            cbs.nth(0).check()
            cbs.nth(1).check()
            page.wait_for_timeout(300)
            # Right click one of the selected rows
            rows.nth(0).click(button="right")
            page.wait_for_timeout(400)
            save_screenshot(page, "6_multi_selection_menu.png")
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            # Uncheck
            cbs.nth(0).uncheck()
            cbs.nth(1).uncheck()
            page.wait_for_timeout(200)
        else:
            print(f"Not enough checkboxes for multi-selection: {cbs.count()}")

        # SCENARIO 7: New-Folder Modal
        print("--- Scenario 7: New-Folder Modal ---")
        page.locator(".table-container").click(button="right", position={"x": 550, "y": 480})
        page.wait_for_timeout(300)
        page.locator("#emptyCtxNew").hover()
        page.wait_for_timeout(300)
        btn_new_folder = page.locator("#emptyCtxNewFolder")
        if btn_new_folder.is_visible():
            btn_new_folder.click()
            page.wait_for_timeout(400)
            # Check location text
            loc_text = page.locator("#folderModalLocation").inner_text()
            print(f"Folder Modal Location: {loc_text}")
            save_screenshot(page, "7_new_folder_modal.png")
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        else:
            print("WARNING: #emptyCtxNewFolder not visible")

        # SCENARIO 8: Rename Modal
        print("--- Scenario 8: Rename Modal ---")
        first_row = rows.first
        first_row.click()
        page.wait_for_timeout(200)
        page.keyboard.press("F2")
        page.wait_for_timeout(400)
        rename_modal = page.locator("#renameModal")
        if rename_modal.is_visible():
            loc_text = page.locator("#renameModalLocation").inner_text()
            print(f"Rename Modal Location: {loc_text}")
            save_screenshot(page, "8_rename_modal.png")
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        else:
            print("F2 didn't open modal, trying context menu rename")
            first_row.click(button="right")
            page.wait_for_timeout(300)
            page.locator("#ctxMenuRename").click()
            page.wait_for_timeout(400)
            save_screenshot(page, "8_rename_modal.png")
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)

        # KEYBOARD SHORTCUTS TEST
        print("--- Testing Keyboard Shortcuts ---")
        page.focus("body")
        # 1. Ctrl+A
        page.keyboard.press("Control+a")
        page.wait_for_timeout(200)
        checked_count = page.evaluate("() => document.querySelectorAll('#driveTableBody .drive-checkbox:checked').length")
        print(f"Ctrl+A selected: {checked_count} items")

        # 2. Ctrl+C
        page.keyboard.press("Control+c")
        page.wait_for_timeout(200)
        clip_items = page.evaluate("() => (window.__abood_drive_state && window.__abood_drive_state.clipboard) ? window.__abood_drive_state.clipboard.items.length : 0")
        print(f"Ctrl+C clipboard items: {clip_items}")

        # 3. Escape
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        cleared_count = page.evaluate("() => document.querySelectorAll('#driveTableBody .drive-checkbox:checked').length")
        print(f"Escape cleared count: {cleared_count}")

        # 4. Ctrl+X and Paste readiness test
        first_row.click()
        page.wait_for_timeout(200)
        page.keyboard.press("Control+x")
        page.wait_for_timeout(200)
        cut_mode = page.evaluate("() => (window.__abood_drive_state && window.__abood_drive_state.clipboard) ? window.__abood_drive_state.clipboard.mode : null")
        print(f"Ctrl+X clipboard mode: {cut_mode}")

        # POSITIONING BOUNDS TESTS
        print("--- Testing Menu Viewport Bounds ---")
        corners = [
            ("top-left", 30, 30),
            ("top-right", 1400, 30),
            ("bottom-left", 30, 860),
            ("bottom-right", 1400, 860),
        ]
        for name, cx, cy in corners:
            page.evaluate(f"() => {{ const evt = new MouseEvent('contextmenu', {{ clientX: {cx}, clientY: {cy}, bubbles: true }}); document.querySelector('.table-container').dispatchEvent(evt); }}")
            page.wait_for_timeout(150)
            bounds = page.evaluate("""() => {
                const el = document.getElementById('emptyContextMenu');
                const rect = el.getBoundingClientRect();
                return {
                    left: rect.left,
                    top: rect.top,
                    right: rect.right,
                    bottom: rect.bottom,
                    width: rect.width,
                    height: rect.height,
                    windowW: window.innerWidth,
                    windowH: window.innerHeight,
                    clippedX: rect.left < 0 || rect.right > window.innerWidth,
                    clippedY: rect.top < 0 || rect.bottom > window.innerHeight
                };
            }""")
            print(f"Corner {name} at ({cx}, {cy}): rect={bounds['left']},{bounds['top']} to {bounds['right']},{bounds['bottom']}, clippedX={bounds['clippedX']}, clippedY={bounds['clippedY']}")
            page.keyboard.press("Escape")
            page.wait_for_timeout(100)

        # Final health check
        print(f"Console errors ({len(console_errors)}): {console_errors}")
        print(f"CSP violations ({len(csp_violations)}): {csp_violations}")
        final_overflow = page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth")
        print(f"Final Horizontal Overflow: {final_overflow}")

        browser.close()

if __name__ == "__main__":
    run()
