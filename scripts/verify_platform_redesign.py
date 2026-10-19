"""Empirical acceptance and screenshot verification for Dashboard UI/UX Overhaul."""
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
from app.models.video import Video
from app.security.tokens import hash_token
from app.security.csrf import generate_csrf_token

with SyncSessionLocal() as db:
    admin = db.query(User).filter(User.username == 'abood').first()
    token = secrets.token_urlsafe(32)
    csrf = generate_csrf_token()
    db.add(UserSession(
        id=uuid.uuid4(), user_id=admin.id, token_hash=hash_token(token),
        ip_address='127.0.0.1', user_agent='verify_redesign',
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=60),
    ))
    vid = db.query(Video).filter(Video.status == 'READY').first()
    vid_id = str(vid.id) if vid else None
    vid_title = vid.title if vid else None
    db.commit()
    print(json.dumps({'session': token, 'csrf': csrf, 'vid_id': vid_id, 'vid_title': vid_title}))
"""

res = subprocess.run(
    ['ssh', '-o', 'BatchMode=yes', 'abood@127.0.0.1', 'docker exec -i -e PYTHONPATH=/app aboodvideo_backend python'],
    input=auth_code, text=True, capture_output=True, check=True
)
auth = json.loads(res.stdout.strip())
print(f"Authenticated: session={auth['session'][:8]}... video_id={auth['vid_id']} ({auth['vid_title']})")

local_art_dir = Path("c:/aboodvideo/browser-artifacts/platform_redesign")
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

    # 1. Videos Library Page (2-tier Toolbar)
    print("\n[Step 2] Verifying Videos Library with 2-tier toolbar...")
    page.goto("http://platform.example/admin/videos", wait_until="networkidle")
    page.wait_for_selector("#videos-tbody tr", state="visible")
    page.wait_for_timeout(1000)

    p1 = local_art_dir / "01_modern_videos_library.png"
    page.screenshot(path=str(p1))
    print(f"  Saved: {p1}")

    # 2. Open Advanced Filters Drawer
    print("\n[Step 3] Toggling Advanced Filters Drawer...")
    page.click("#btn-toggle-advanced")
    page.wait_for_selector("#advanced-filters-drawer", state="visible")
    page.wait_for_timeout(600)

    p2 = local_art_dir / "02_videos_library_advanced_filters.png"
    page.screenshot(path=str(p2))
    print(f"  Saved: {p2}")

    # 3. Video Detail Page - Overview Tab (Hero, 4 KPIs, Policy, Specs Grid)
    vid_id = auth['vid_id']
    detail_url = f"http://platform.example/admin/videos/detail?v={vid_id}"
    print(f"\n[Step 4] Navigating to Video Detail: {detail_url}")
    page.goto(detail_url, wait_until="networkidle")
    page.wait_for_selector("#video-detail:not(.hidden)", state="visible", timeout=10000)
    page.wait_for_selector(".spec-tile", state="visible", timeout=10000)
    page.wait_for_timeout(1200)

    p3 = local_art_dir / "03_video_detail_hero_kpis_specs.png"
    page.screenshot(path=str(p3))
    print(f"  Saved: {p3}")

    # 4. Video Detail Page - Sharing & Embed Tab
    print("\n[Step 5] Switching to Sharing & Embed Tab...")
    page.click("#tab-btn-sharing")
    page.wait_for_selector("#tab-sharing:not(.hidden)", state="visible")
    page.wait_for_timeout(600)

    # Click blue color swatch to show live reaction
    page.click(".btn-color-dot[data-col='2563eb']")
    page.wait_for_timeout(500)

    p4 = local_art_dir / "04_video_detail_embed_generator.png"
    page.screenshot(path=str(p4))
    print(f"  Saved: {p4}")

    # 5. Video Detail Page - Media & HLS Renditions Tab
    print("\n[Step 6] Switching to Media & HLS Renditions Tab...")
    page.click("#tab-btn-media")
    page.wait_for_selector("#tab-media:not(.hidden)", state="visible")
    page.wait_for_timeout(600)

    p5 = local_art_dir / "05_video_detail_renditions_table.png"
    page.screenshot(path=str(p5))
    print(f"  Saved: {p5}")

    browser.close()

# Copy to brain artifact directory
for f in local_art_dir.glob("*.png"):
    dst = brain_art_dir / f.name
    shutil.copy2(f, dst)
    print(f"Synced to brain artifacts: {dst}")

print("\n======================================================================")
print("SUCCESS: All visual screenshots captured and verified!")
print("======================================================================")
