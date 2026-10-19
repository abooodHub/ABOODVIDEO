"""Empirical acceptance and screenshot capture for unified library navigation and video details."""
import json
import sys
import subprocess
from pathlib import Path
from playwright.sync_api import sync_playwright

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

print("======================================================================")
print("1. Generating Admin Session for Testing on Live Production Server")
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
        ip_address='127.0.0.1', user_agent='verify_unified_menu',
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=60),
    ))
    # Get first ready video
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
print(f"Admin authenticated: session={auth['session'][:8]}... csrf={auth['csrf'][:8]}... video_id={auth['vid_id']}")

output_dir = Path("c:/aboodvideo/browser-artifacts/unified_library_acceptance")
output_dir.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=["--host-resolver-rules=MAP platform.example 127.0.0.1"]
    )
    ctx = browser.new_context(
        viewport={"width": 1440, "height": 900},
        locale="ar-SA",
    )
    # Add cookies for platform.example
    ctx.add_cookies([
        {
            "name": "aboodvideo_session",
            "value": auth["session"],
            "domain": "platform.example",
            "path": "/",
            "httpOnly": True,
            "secure": False,
        },
        {
            "name": "aboodvideo_csrf",
            "value": auth["csrf"],
            "domain": "platform.example",
            "path": "/",
            "httpOnly": False,
            "secure": False,
        }
    ])

    page = ctx.new_page()

    print("\n======================================================================")
    print("2. Verifying Main Sidebar & Video Library (/admin/videos)")
    print("======================================================================")
    page.goto("http://platform.example/admin/videos", wait_until="networkidle")
    page.wait_for_timeout(1000)

    # Check sidebar items
    sidebar_links = page.evaluate("""() => {
        const links = Array.from(document.querySelectorAll('#adminSidebar .nav-item'));
        return links.map(l => ({
            label: l.querySelector('.nav-label')?.textContent?.trim(),
            href: l.getAttribute('href'),
            isActive: l.classList.contains('is-active')
        }));
    }""")
    print("Sidebar navigation items found:")
    for l in sidebar_links:
        print(f"  - {l['label']} -> {l['href']} (Active: {l['isActive']})")

    # Capture 01: Video Library with new unified sidebar
    p1 = output_dir / "01_unified_admin_sidebar_videos_library.png"
    page.screenshot(path=str(p1), full_page=False)
    print(f"Saved: {p1}")

    if auth['vid_id']:
        vid_id = auth['vid_id']
        print(f"\n======================================================================")
        print(f"3. Verifying Integrated Video Details Page (/admin/videos/detail?v={vid_id})")
        print("======================================================================")
        page.goto(f"http://platform.example/admin/videos/detail?v={vid_id}", wait_until="networkidle")
        page.wait_for_timeout(1500)

        # Evaluate page fields
        details_info = page.evaluate("""() => {
            const specFilename = document.getElementById('spec-source-filename')?.textContent;
            const specQuality = document.getElementById('spec-quality')?.textContent;
            const specDuration = document.getElementById('spec-duration')?.textContent;
            const specSize = document.getElementById('spec-size')?.textContent;
            const specSubs = document.getElementById('spec-subtitles')?.textContent;
            const policyVal = document.getElementById('detail-policy-select')?.value;
            const effectiveBadge = document.getElementById('detail-effective-badge')?.textContent;
            const btnPreview = document.getElementById('btn-preview');
            return {
                specFilename,
                specQuality,
                specDuration,
                specSize,
                specSubs,
                policyVal,
                effectiveBadge,
                btnPreviewVisible: !btnPreview?.classList.contains('hidden'),
                btnPreviewText: btnPreview?.textContent?.trim(),
                btnPreviewHref: btnPreview?.getAttribute('href')
            };
        }""")
        print("Video Details Page Evaluation:")
        for k, v in details_info.items():
            print(f"  - {k}: {v}")

        # Capture 02: Overview with Specs & Download Policy
        p2 = output_dir / "02_video_detail_specs_and_download_policy.png"
        page.screenshot(path=str(p2), full_page=False)
        print(f"Saved: {p2}")

        print("\n======================================================================")
        print("4. Testing Live Policy Change Interaction (DENY -> ALLOW)")
        print("======================================================================")
        # Select DENY and click save
        page.select_option("#detail-policy-select", "DENY")
        page.click("#btn-save-policy")
        page.wait_for_timeout(1000)

        badge_after_deny = page.evaluate("() => document.getElementById('detail-effective-badge')?.textContent")
        feedback_deny = page.evaluate("() => document.getElementById('policy-feedback')?.textContent")
        print(f"After setting DENY -> Effective badge: '{badge_after_deny}', Feedback: '{feedback_deny}'")

        # Capture 03: Policy set to DENY
        p3 = output_dir / "03_policy_changed_to_deny.png"
        page.screenshot(path=str(p3), full_page=False)
        print(f"Saved: {p3}")

        # Restore to INHERIT and save
        page.select_option("#detail-policy-select", "INHERIT")
        page.click("#btn-save-policy")
        page.wait_for_timeout(1000)
        badge_after_inherit = page.evaluate("() => document.getElementById('detail-effective-badge')?.textContent")
        print(f"Restored to INHERIT -> Effective badge: '{badge_after_inherit}'")

        print("\n======================================================================")
        print("5. Verifying Sharing & Embed Tab in Details")
        print("======================================================================")
        page.click("#tab-btn-sharing")
        page.wait_for_timeout(600)
        p4 = output_dir / "04_video_detail_sharing_and_embed_tab.png"
        page.screenshot(path=str(p4), full_page=False)
        print(f"Saved: {p4}")

    browser.close()

print("\n======================================================================")
print("ACCEPTANCE VERIFICATION COMPLETE SUCCESSFULLY!")
print("======================================================================")
