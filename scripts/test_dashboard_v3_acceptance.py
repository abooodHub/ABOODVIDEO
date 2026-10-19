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
SCREENSHOT_DIR = Path(r"C:\aboodvideo\screenshots")
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
BRAIN_DIR = Path(r"C:\Users\abood\.gemini\antigravity\brain\d00a1f3c-9a6f-479a-95b9-eb67986f12cb\screenshots")
BRAIN_DIR.mkdir(parents=True, exist_ok=True)


def create_admin_session() -> dict:
    code = """
import json, secrets, uuid
from datetime import datetime, timedelta, timezone
from app.core.database import SyncSessionLocal
from app.models.user import User, UserRole
from app.models.session import UserSession
from app.security.tokens import hash_token

with SyncSessionLocal() as db:
    admin = db.query(User).filter(User.username == 'admin_live').first()
    if not admin:
        admin = db.query(User).filter(User.role.in_([UserRole.OWNER, UserRole.ADMIN]), User.is_active.is_(True)).first()
    token = secrets.token_urlsafe(32)
    db.add(UserSession(
        id=uuid.uuid4(), user_id=admin.id, token_hash=hash_token(token),
        ip_address='127.0.0.1', user_agent='dashboard-v3-acceptance',
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=60),
    ))
    db.commit()
    print(json.dumps({'session': token, 'user': admin.username, 'role': str(admin.role)}))
"""
    res = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "abood@127.0.0.1", "docker exec -i -e PYTHONPATH=/app aboodvideo_api python"],
        input=code, text=True, capture_output=True, check=True
    )
    return json.loads(res.stdout.strip())


def run_acceptance():
    print("==================================================")
    print(" ABOODVIDEO — ADMIN DASHBOARD V3 ACCEPTANCE")
    print("==================================================")

    print("\n[1/7] Minting Admin Session...")
    auth = create_admin_session()
    print(f"  -> Authenticated as: {auth['user']} ({auth['role']})")
    print(f"  -> Session Token: {auth['session'][:8]}...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        console_errors = []
        csp_violations = []

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
                "sameSite": "Lax",
            },
            {
                "name": "session",
                "value": auth["session"],
                "domain": "127.0.0.1",
                "path": "/",
                "httpOnly": True,
                "sameSite": "Lax",
            }
        ])

        page = context.new_page()

        def on_console(msg):
            print(f"CONSOLE [{msg.type}]: {msg.text}")
            if "Content Security Policy" in msg.text or "CSP" in msg.text:
                csp_violations.append(msg.text)
            if msg.type == "error":
                console_errors.append(msg.text)

        page.on("console", on_console)
        page.on("pageerror", lambda err: (console_errors.append(str(err)), print(f"PAGE ERROR: {err}")))
        page.on("requestfailed", lambda req: print(f"REQ FAILED: {req.url} {req.failure}"))
        page.on("response", lambda resp: print(f"API RESP: {resp.status} {resp.url}") if "/api/" in resp.url else None)

        admin_url = f"{BASE_URL}/admin"
        print(f"\n[2/7] Navigating to Dashboard: {admin_url}", flush=True)
        res = page.goto(admin_url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_selector(".dash-hero", timeout=10000)
        # Give JS a moment to complete initial fetch and populate widgets
        page.wait_for_timeout(2500)
        print(f"  -> HTTP Status: {res.status}", flush=True)
        assert res.status == 200, f"Expected 200 OK, got {res.status}"

        # 1. Topbar Content Verification
        print("\n[3/7] Auditing Topbar Content...", flush=True)
        # Check that topbar-title is empty or omitted
        topbar_title_el = page.query_selector(".topbar-title")
        if topbar_title_el:
            topbar_title_text = topbar_title_el.inner_text().strip()
            print(f"  -> .topbar-title text: '{topbar_title_text}'")
            assert "لوحة التحكم" not in topbar_title_text, "Redundant 'لوحة التحكم' found in topbar!"
        else:
            print("  -> .topbar-title slot: Completely removed (passed)")

        # Check that global search is removed on /admin
        global_search = page.query_selector("#global-search, .topbar-search")
        assert global_search is None, "Global search input should be removed from /admin topbar!"
        print("  -> Passed: #global-search is absent from /admin topbar.")

        # Check topbar utility action (+ رفع فيديو)
        dash_upload_btn = page.query_selector(".dash-upload-btn, a[href='/admin/upload']")
        assert dash_upload_btn is not None, "Missing quick upload action in dashboard topbar!"
        print(f"  -> Passed: Dashboard topbar action present: '{dash_upload_btn.inner_text().strip()}'")

        # 2. Hero Section Verification
        print("\n[4/7] Auditing Hero Section...", flush=True)
        hero = page.query_selector(".dash-hero")
        assert hero is not None, "Missing .dash-hero section!"
        hero_title = page.locator(".dash-hero-title").inner_text().strip()
        hero_sub = page.locator(".dash-hero-sub").inner_text().strip()
        print(f"  -> Hero title: '{hero_title}'")
        print(f"  -> Hero subtitle: '{hero_sub}'")
        assert "مرحباً بك مجدداً" in hero_title, f"Unexpected hero title: {hero_title}"
        assert "أداء منصتك" in hero_sub or "مكتبتك" in hero_sub, f"Unexpected hero subtitle: {hero_sub}"

        # Check Hero quick action buttons
        hero_actions = page.locator(".dash-hero-actions a")
        assert hero_actions.count() >= 3, f"Expected at least 3 quick actions in hero, found {hero_actions.count()}"
        print(f"  -> Hero quick action buttons: {[hero_actions.nth(i).inner_text().strip() for i in range(hero_actions.count())]}")

        # 3. KPI Cards Verification
        print("\n[5/7] Auditing KPI Cards (Real Data)...", flush=True)
        kpi_videos = page.locator("#kpiVideosVal").inner_text().strip()
        kpi_storage = page.locator("#kpiStorageVal").inner_text().strip()
        kpi_processing = page.locator("#kpiProcessingVal").inner_text().strip()
        kpi_shares = page.locator("#kpiSharesVal").inner_text().strip()

        print(f"  -> KPI Total Videos: '{kpi_videos}' ({page.locator('#kpiVideosSub').inner_text().strip()})")
        print(f"  -> KPI Storage: '{kpi_storage}' ({page.locator('#kpiStorageSub').inner_text().strip()})")
        print(f"  -> KPI Processing: '{kpi_processing}' ({page.locator('#kpiProcessingSub').inner_text().strip()})")
        print(f"  -> KPI Active Shares: '{kpi_shares}' ({page.locator('#kpiSharesSub').inner_text().strip()})")

        assert kpi_videos not in ("—", "undefined", "NaN", ""), "KPI Videos value did not load!"
        assert kpi_storage not in ("—", "undefined", "NaN", ""), "KPI Storage value did not load!"
        assert kpi_processing not in ("—", "undefined", "NaN", ""), "KPI Processing value did not load!"
        assert kpi_shares not in ("—", "undefined", "NaN", ""), "KPI Shares value did not load!"
        print("  -> Passed: All 4 KPI cards loaded with REAL backend values.")

        # 4. Main Grid Widgets Verification
        print("\n[6/7] Auditing Main Grid Widgets...", flush=True)
        # Check Activity Chart
        chart_el = page.query_selector("#dashChart")
        assert chart_el is not None, "Missing #dashChart!"
        chart_summary = page.locator("#dashChartSummary").inner_text().strip()
        print(f"  -> Chart summary: '{chart_summary}'")

        # Check System & Storage status
        svc_api = page.locator("#svcApiVal").inner_text().strip()
        svc_db = page.locator("#svcDbVal").inner_text().strip()
        svc_redis = page.locator("#svcRedisVal").inner_text().strip()
        svc_worker = page.locator("#svcWorkerVal").inner_text().strip()
        svc_storage = page.locator("#svcStorageVal").inner_text().strip()
        print(f"  -> System services: API={svc_api}, DB={svc_db}, Redis={svc_redis}, Worker={svc_worker}, Storage={svc_storage}")

        # Check Latest Videos (deduplicated logical videos with quality summary)
        latest_video_rows = page.locator("#dashLatestVideos .dash-video-row")
        latest_count = latest_video_rows.count()
        print(f"  -> Latest videos rendered items: {latest_count}")
        assert latest_count > 0, "No latest videos rendered!"
        
        video_titles = []
        for i in range(latest_count):
            row = latest_video_rows.nth(i)
            t = row.locator(".dash-video-title").inner_text().strip()
            meta_text = row.locator(".dash-video-meta").inner_text().strip()
            print(f"     Video {i+1}: '{t}' | meta: '{meta_text}'")
            video_titles.append(t)
            # Verify quality summary format (e.g., 4K · 6 جودات)
            if "Spirit_Cage" in t:
                assert "جودات" in meta_text or "4K" in meta_text, f"Quality summary missing in video meta: {meta_text}"
        
        # Verify deduplication: no duplicate video titles
        assert len(video_titles) == len(set(video_titles)), f"Duplicate logical videos found: {video_titles}"
        print("  -> Passed: All logical videos are strictly deduplicated.")

        # Check Processing Queue (No test records like 'Test Source Video')
        queue_items = page.locator("#dashQueue .dash-queue-item")
        queue_count = queue_items.count()
        print(f"  -> Queue rendered active items: {queue_count}")
        for i in range(queue_count):
            item_text = queue_items.nth(i).inner_text()
            assert "Test Source Video" not in item_text, f"Test record found in processing queue: {item_text}"
        print("  -> Passed: No test/demo records in processing queue.")

        # Check Activity Feed (No test noise like 'Test Source Video')
        feed_items = page.locator("#dashActivity .dash-feed-item")
        feed_count = feed_items.count()
        print(f"  -> Activity feed items: {feed_count}")
        assert feed_count > 0, "No activity feed items rendered!"
        for i in range(feed_count):
            item_text = feed_items.nth(i).inner_text()
            print(f"     Feed {i+1}: '{item_text}'")
            assert "Test Source Video" not in item_text, f"Test noise found in activity feed: {item_text}"
            assert "test" not in item_text.lower() or "Spirit_Cage" in item_text, f"Suspicious test item in feed: {item_text}"
        print("  -> Passed: No test/demo noise in activity feed.")

        # 5. Multi-Resolution Visual & Responsive Audits
        print("\n[7/7] Multi-Resolution Responsiveness & Screenshot Capture...", flush=True)
        resolutions = [
            ("1920x1080", 1920, 1080),
            ("1440x900", 1440, 900),
            ("1280x720", 1280, 720),
        ]

        for res_name, w, h in resolutions:
            page.set_viewport_size({"width": w, "height": h})
            page.wait_for_timeout(400)

            # Check horizontal overflow
            has_hscroll = page.evaluate("() => document.documentElement.scrollWidth > document.documentElement.clientWidth")
            scroll_w = page.evaluate("() => document.documentElement.scrollWidth")
            client_w = page.evaluate("() => document.documentElement.clientWidth")
            print(f"  -> {res_name}: scrollWidth={scroll_w}, clientWidth={client_w}, overflow={has_hscroll}")
            assert not has_hscroll, f"Horizontal scrollbar detected at {res_name}! ({scroll_w} > {client_w})"

            # Capture screenshots
            file_name = f"dashboard_v3_desktop_{res_name}.png"
            p1 = SCREENSHOT_DIR / file_name
            p2 = BRAIN_DIR / file_name
            page.screenshot(path=str(p1), full_page=False)
            page.screenshot(path=str(p2), full_page=False)
            print(f"  -> Saved screenshot: {p1}")

        # Console and CSP Diagnostics
        print("\n=== Console & CSP Diagnostics ===")
        print(f"Console errors: {len(console_errors)}")
        print(f"CSP violations: {len(csp_violations)}")
        if console_errors:
            print("Errors detected:", console_errors)
        assert len(console_errors) == 0, f"Found console errors: {console_errors}"
        assert len(csp_violations) == 0, f"Found CSP violations: {csp_violations}"

        browser.close()

    print("\n==================================================")
    print(" ALL DASHBOARD V3 ACCEPTANCE CHECKS PASSED!")
    print("==================================================")


if __name__ == "__main__":
    run_acceptance()
