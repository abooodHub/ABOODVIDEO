"""Empirical verification of Settings integration into the Main Sidebar."""
import json
import sys
import shutil
import subprocess
from pathlib import Path
from playwright.sync_api import sync_playwright

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

print("======================================================================")
print("1. Authenticating Admin Session on Live Production Server")
print("======================================================================")

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
        ip_address='127.0.0.1', user_agent='verify_settings_sidebar',
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
print(f"Authenticated: session={auth['session'][:8]}...")

local_art_dir = Path("c:/aboodvideo/browser-artifacts/settings_integration")
local_art_dir.mkdir(parents=True, exist_ok=True)
brain_art_dir = Path("C:/Users/abood/.gemini/antigravity/brain/c58b9ae4-6748-4d1c-828d-53dae7a841e8")

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=["--host-resolver-rules=MAP platform.example 127.0.0.1"]
    )
    ctx = browser.new_context(
        viewport={"width": 1440, "height": 920},
        locale="ar-SA",
    )
    ctx.add_cookies([
        {"name": "aboodvideo_session", "value": auth["session"], "domain": "platform.example", "path": "/", "httpOnly": True, "secure": False},
        {"name": "aboodvideo_csrf", "value": auth["csrf"], "domain": "platform.example", "path": "/", "httpOnly": False, "secure": False},
    ])
    page = ctx.new_page()

    # 1. Navigate to Settings page directly
    print("\n[Step 2] Navigating to /admin/settings...")
    page.goto("http://platform.example/admin/settings", wait_until="networkidle")
    page.wait_for_selector(".settings-tabs-bar", state="visible")
    page.wait_for_timeout(1000)

    p1 = local_art_dir / "06_settings_main_sidebar_integrated.png"
    page.screenshot(path=str(p1))
    print(f"  Saved: {p1}")

    # 2. Click on 'التضمين والـ CDN الخارجي' from the Main Sidebar
    print("\n[Step 3] Clicking 'التضمين والـ CDN الخارجي' from main sidebar...")
    sidebar_embed_link = page.locator(".sidebar-nav a[data-tab='embed']")
    sidebar_embed_link.click()
    page.wait_for_selector("#tab-embed.is-active", state="visible")
    page.wait_for_timeout(800)

    p2 = local_art_dir / "07_settings_embed_tab_active_fullwidth.png"
    page.screenshot(path=str(p2))
    print(f"  Saved: {p2}")

    # 3. Click on 'المشاركة والتنزيل' from the Main Sidebar
    print("\n[Step 4] Clicking 'المشاركة والتنزيل' from main sidebar...")
    sidebar_sharing_link = page.locator(".sidebar-nav a[data-tab='sharing']")
    sidebar_sharing_link.click()
    page.wait_for_selector("#tab-sharing.is-active", state="visible")
    page.wait_for_timeout(800)

    p3 = local_art_dir / "08_settings_sharing_tab_active.png"
    page.screenshot(path=str(p3))
    print(f"  Saved: {p3}")

    browser.close()

# Copy to brain artifact directory
for f in local_art_dir.glob("*.png"):
    dst = brain_art_dir / f.name
    shutil.copy2(f, dst)
    print(f"Synced to brain artifacts: {dst}")

print("\n======================================================================")
print("SUCCESS: Settings sidebar integration verified and screenshots captured!")
print("======================================================================")
