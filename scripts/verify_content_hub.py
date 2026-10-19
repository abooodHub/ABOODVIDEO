import sys
import json
import time
import subprocess
from pathlib import Path
from playwright.sync_api import sync_playwright

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

EVIDENCE_DIR = Path(r"C:\Users\abood\.gemini\antigravity\brain\c58b9ae4-6748-4d1c-828d-53dae7a841e8")
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

# Generate fresh auth session
auth_code = """
import json, secrets, uuid
from datetime import datetime, timedelta, timezone
from app.core.database import SyncSessionLocal
from app.models.user import User
from app.models.session import UserSession
from app.security.tokens import hash_token
from app.security.csrf import generate_csrf_token

with SyncSessionLocal() as db:
    admin = db.query(User).filter(User.username == 'abood').first()
    token = secrets.token_urlsafe(32)
    csrf = generate_csrf_token()
    db.add(UserSession(
        id=uuid.uuid4(), user_id=admin.id, token_hash=hash_token(token),
        ip_address='127.0.0.1', user_agent='verify_content_hub',
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=60),
    ))
    db.commit()
    print(json.dumps({'session': token, 'csrf': csrf}))
"""

res = subprocess.run(
    ['ssh', '-o', 'BatchMode=yes', 'abood@127.0.0.1', 'docker exec -i -e PYTHONPATH=/app aboodvideo_backend python'],
    input=auth_code, text=True, capture_output=True, check=True
)
auth = json.loads(res.stdout.strip())
print(f"Authenticated session generated: {auth['session'][:8]}...")

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=["--host-resolver-rules=MAP platform.example 127.0.0.1"]
    )
    context = browser.new_context(
        viewport={"width": 1440, "height": 920},
        locale="ar-SA",
        ignore_https_errors=True
    )
    context.add_cookies([
        {
            "name": "aboodvideo_session",
            "value": auth['session'],
            "domain": "platform.example",
            "path": "/",
            "httpOnly": True,
            "secure": False,
        },
        {
            "name": "aboodvideo_csrf",
            "value": auth['csrf'],
            "domain": "platform.example",
            "path": "/",
            "httpOnly": False,
            "secure": False,
        }
    ])

    page = context.new_page()

    # 1. Check Content Hub Main Page (Tab 1: Videos & Episodes)
    print("\n[Shot 1] Opening Content Hub (http://platform.example/admin/videos)...")
    page.goto("http://platform.example/admin/videos", wait_until="networkidle")
    page.wait_for_selector(".content-hub-tabs", timeout=15000)
    page.wait_for_timeout(1500)
    p1 = EVIDENCE_DIR / "09_content_hub_videos_tab.png"
    page.screenshot(path=str(p1))
    print(f"Captured: {p1} ✓")

    # 2. Switch to Tab 2: Series & Seasons
    print("\n[Shot 2] Switching to Series & Seasons tab...")
    page.click('.hub-tab-btn[data-hub-tab="series"]')
    page.wait_for_timeout(1500)
    p2 = EVIDENCE_DIR / "10_content_hub_series_tab.png"
    page.screenshot(path=str(p2))
    print(f"Captured: {p2} ✓")

    # 3. Switch to Tab 3: Drive / Folders Explorer
    print("\n[Shot 3] Switching to Drive / Folders tab...")
    page.click('.hub-tab-btn[data-hub-tab="drive"]')
    page.wait_for_timeout(2500)
    p3 = EVIDENCE_DIR / "11_content_hub_drive_tab.png"
    page.screenshot(path=str(p3))
    print(f"Captured: {p3} ✓")

    # 4. Switch to Tab 4: Trash
    print("\n[Shot 4] Switching to Trash tab...")
    page.click('.hub-tab-btn[data-hub-tab="trash"]')
    page.wait_for_timeout(2500)
    p4 = EVIDENCE_DIR / "12_content_hub_trash_tab.png"
    page.screenshot(path=str(p4))
    print(f"Captured: {p4} ✓")

    browser.close()
    print("\nAll verification screenshots captured successfully!")
