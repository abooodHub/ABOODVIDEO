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
        ip_address='127.0.0.1', user_agent='watch-hotfix-acceptance',
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
    print("==================================================")
    print(" ABOODVIDEO — WATCH PAGE CLEANUP HOTFIX ACCEPTANCE")
    print("==================================================")

    print("\n[1/8] Minting Fresh Admin Session...")
    auth = create_admin_session()
    print(f"  -> Authenticated as: {auth['user']}")
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
            if msg.type in ("error", "warning"):
                text = msg.text
                if "Content Security Policy" in text or "CSP" in text:
                    csp_violations.append(text)
                if msg.type == "error":
                    console_errors.append(text)

        page.on("console", on_console)
        page.on("pageerror", lambda err: console_errors.append(str(err)))

        watch_url = f"{BASE_URL}/watch/{TARGET_UUID}"
        print(f"\n[2/8] Navigating to Watch Page: {watch_url}", flush=True)
        res = page.goto(watch_url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_selector("#player-controls", timeout=10000)
        print(f"  -> HTTP Status: {res.status}", flush=True)
        assert res.status == 200, f"Expected 200 OK, got {res.status}"

        # 1. Header Cleanup Verification
        print("\n[3/8] Auditing Cinema Header Cleanup...")
        header_el = page.query_selector("header.watch-cinema-header")
        assert header_el is not None, "Missing .watch-cinema-header on watch page!"

        # Check navigation links: الرئيسية, المكتبة, المسلسلات, لوحة التحكم
        forbidden_texts = ["الرئيسية", "المكتبة", "المسلسلات", "لوحة التحكم"]
        header_text = header_el.inner_text()
        for forbidden in forbidden_texts:
            assert forbidden not in header_text, f"Forbidden nav item '{forbidden}' found in Watch header!"
        print("  -> Passed: No navigation links in cinema header (الرئيسية, المكتبة, المسلسلات, لوحة التحكم are ABSENT).")

        # Check search input absent in header
        header_search = header_el.query_selector("input[type=search], .viewer-header-search")
        assert header_search is None, "Search bar must NOT be present in Watch cinema header!"
        print("  -> Passed: Search bar is ABSENT from Watch cinema header.")

        # Check hamburger button absent in header
        header_hamb = header_el.query_selector(".viewer-hamburger, #viewer-hamburger-btn")
        assert header_hamb is None, "Mobile hamburger button must NOT be present in Watch cinema header!"
        print("  -> Passed: Hamburger drawer toggle is ABSENT from Watch cinema header.")

        # Check minimal branding present
        brand_el = header_el.query_selector(".viewer-brand")
        assert brand_el is not None, "Brand 'ABOODVIDEO' must be present in cinema header!"
        assert "ABOOD" in brand_el.inner_text(), "Brand text mismatch!"
        print("  -> Passed: Minimal branding 'ABOODVIDEO' correctly rendered.")

        # 2. Drawer & Footer Cleanup Verification
        print("\n[4/8] Auditing Mobile Drawer & Footer Removal...")
        drawer_el = page.query_selector("aside.viewer-drawer, #viewer-drawer")
        assert drawer_el is None, "Drawer should be completely omitted from watch page DOM!"
        print("  -> Passed: Mobile drawer is completely omitted from watch.html.")

        footer_el = page.query_selector("footer.viewer-footer, footer")
        assert footer_el is None, "Footer should be completely omitted from watch page DOM!"
        print("  -> Passed: Footer is completely omitted from watch.html.")

        # 3. Cinema Video Title Header Verification
        print("\n[5/8] Auditing Cinema Video Title Header...")
        title_header = page.query_selector(".watch-title-header")
        assert title_header is not None, "Missing .watch-title-header above player!"
        cinema_title = page.query_selector(".watch-cinema-title")
        assert cinema_title is not None, "Missing .watch-cinema-title!"
        title_text = cinema_title.inner_text().strip()
        print(f"  -> Cinema Title rendered: '{title_text}'")
        assert len(title_text) > 0, "Video title text cannot be empty!"

        # 4. Duplicate Player Controls Audit
        print("\n[6/8] Auditing Player Controls for Zero Duplication...")
        page.wait_for_selector("#player-controls", timeout=5000)

        # Check control buttons count
        play_btn = page.query_selector("#btn-play")
        mute_btn = page.query_selector("#btn-mute")
        fs_btn = page.query_selector("#btn-fullscreen")
        timeline = page.query_selector("#timeline")
        time_disp = page.query_selector("#time-display")
        subs_btn = page.query_selector("#btn-subs")
        settings_btn = page.query_selector("#btn-settings")
        pip_btn = page.query_selector("#btn-pip")

        assert play_btn is not None, "Missing #btn-play"
        assert mute_btn is not None, "Missing #btn-mute"
        assert fs_btn is not None, "Missing #btn-fullscreen"
        assert timeline is not None, "Missing #timeline"
        assert time_disp is not None, "Missing #time-display"
        assert subs_btn is not None, "Missing #btn-subs"
        assert settings_btn is not None, "Missing #btn-settings"
        assert pip_btn is not None, "Missing #btn-pip"

        # Check that exactly ONE control bar exists
        assert len(page.query_selector_all("#player-controls")) == 1, "Duplicate #player-controls found!"
        assert len(page.query_selector_all("#btn-play")) == 1, "Duplicate #btn-play found!"
        assert len(page.query_selector_all("#btn-mute")) == 1, "Duplicate #btn-mute found!"
        assert len(page.query_selector_all("#btn-fullscreen")) == 1, "Duplicate #btn-fullscreen found!"

        # Trigger mouse movement over player to wake controls
        page.hover("#player-container")
        page.wait_for_timeout(300)

        # Check VISIBLE SVGs inside each button (must be exactly 1 visible SVG per button)
        def get_visible_svgs(btn_selector):
            svgs_info = page.evaluate(f"""() => {{
                return Array.from(document.querySelectorAll('{btn_selector} svg')).map(el => ({{
                    id: el.id || 'svg',
                    display: window.getComputedStyle(el).display,
                    isHiddenClass: el.classList.contains('hidden'),
                    styleDisplay: el.style.display,
                    rectWidth: el.getBoundingClientRect().width,
                    rectHeight: el.getBoundingClientRect().height
                }}));
            }}""")
            visible = []
            for s in svgs_info:
                # An SVG is visible if its computed display is not 'none' and it does not have .hidden
                if s["display"] != "none" and not s["isHiddenClass"] and s["rectWidth"] > 0:
                    visible.append(s["id"])
            return visible

        vis_play_svgs = get_visible_svgs("#btn-play")
        vis_mute_svgs = get_visible_svgs("#btn-mute")
        vis_fs_svgs = get_visible_svgs("#btn-fullscreen")

        print(f"  -> Visible SVGs in #btn-play: {vis_play_svgs}")
        print(f"  -> Visible SVGs in #btn-mute: {vis_mute_svgs}")
        print(f"  -> Visible SVGs in #btn-fullscreen: {vis_fs_svgs}")

        assert len(vis_play_svgs) == 1, f"Expected exactly 1 visible SVG in #btn-play, found: {vis_play_svgs}"
        assert len(vis_mute_svgs) == 1, f"Expected exactly 1 visible SVG in #btn-mute, found: {vis_mute_svgs}"
        assert len(vis_fs_svgs) == 1, f"Expected exactly 1 visible SVG in #btn-fullscreen, found: {vis_fs_svgs}"
        print("  -> Passed: Exactly ONE icon visible per control button! Zero duplicate icons.")

        # Test Play / Pause Toggle
        print("  -> Testing Play/Pause toggle...")
        play_btn.click()
        page.wait_for_timeout(1000)
        vis_play_svgs_playing = get_visible_svgs("#btn-play")
        print(f"  -> Visible SVGs in #btn-play while playing: {vis_play_svgs_playing}")
        assert len(vis_play_svgs_playing) == 1, f"Expected 1 SVG during play, found {vis_play_svgs_playing}"
        assert vis_play_svgs_playing[0] == "icon-pause", f"Expected icon-pause while playing, got {vis_play_svgs_playing[0]}"

        # Pause again
        play_btn.click()
        page.wait_for_timeout(500)
        vis_play_svgs_paused = get_visible_svgs("#btn-play")
        assert len(vis_play_svgs_paused) == 1 and vis_play_svgs_paused[0] == "icon-play"
        print("  -> Passed: Play/Pause toggles between exactly one icon (icon-play <-> icon-pause).")

        # Test Mute Toggle
        print("  -> Testing Mute toggle...")
        mute_btn.click()
        page.wait_for_timeout(300)
        vis_mute_svgs_muted = get_visible_svgs("#btn-mute")
        print(f"  -> Visible SVGs in #btn-mute while muted: {vis_mute_svgs_muted}")
        assert len(vis_mute_svgs_muted) == 1 and vis_mute_svgs_muted[0] == "icon-vol-mute"

        mute_btn.click()
        page.wait_for_timeout(300)
        vis_mute_svgs_unmuted = get_visible_svgs("#btn-mute")
        assert len(vis_mute_svgs_unmuted) == 1 and vis_mute_svgs_unmuted[0] == "icon-vol-high"
        print("  -> Passed: Mute toggles between exactly one icon (icon-vol-high <-> icon-vol-mute).")

        # Test Settings Menu Popup
        print("  -> Testing Settings menu popup...")
        settings_btn.click()
        page.wait_for_timeout(300)
        menu_panel = page.query_selector("#player-menu-panel")
        assert menu_panel is not None and "is-open" in (menu_panel.get_attribute("class") or ""), "Settings panel did not open!"
        print("  -> Passed: Settings menu opens cleanly without layout breakage.")
        settings_btn.click()
        page.wait_for_timeout(300)

        # Test Subtitles Button (opens Subtitles screen in settings panel)
        print("  -> Testing Subtitles button (panel screen)...")
        subs_btn.click()
        page.wait_for_timeout(300)
        subs_screen = page.query_selector('.player-menu-screen[data-screen="subtitles"]')
        assert subs_screen is not None and "is-active" in (subs_screen.get_attribute("class") or ""), "Subtitles screen did not activate!"
        assert "is-open" in (menu_panel.get_attribute("class") or ""), "Settings panel did not open on CC click!"
        print("  -> Passed: Subtitles button activates subtitles screen cleanly.")
        subs_btn.click()
        page.wait_for_timeout(300)

        # Test Timeline Seeking
        print("  -> Testing Timeline seeking...")
        timeline_box = timeline.bounding_box()
        assert timeline_box is not None
        # Click at 25% of timeline width
        page.mouse.click(timeline_box["x"] + timeline_box["width"] * 0.25, timeline_box["y"] + timeline_box["height"] / 2)
        page.wait_for_timeout(500)
        cur_time = page.evaluate("() => document.getElementById('video-element').currentTime")
        print(f"  -> Current video playback position after seek: {cur_time:.2f}s")
        assert cur_time > 0, "Video currentTime did not advance after timeline seek!"
        print("  -> Passed: Timeline seek operates smoothly.")

        # 5. Multi-Resolution Responsiveness & Horizontal Overflow Audit
        print("\n[7/8] Testing Multi-Resolution Responsiveness (1920x1080, 1440x900, 1280x720, 390x844)...")
        resolutions = [
            ("1920x1080", 1920, 1080),
            ("1440x900", 1440, 900),
            ("1280x720", 1280, 720),
            ("390x844", 390, 844),
        ]

        for res_name, w, h in resolutions:
            page.set_viewport_size({"width": w, "height": h})
            page.wait_for_timeout(400)

            # Check for horizontal scroll / overflow
            has_hscroll = page.evaluate("() => document.documentElement.scrollWidth > document.documentElement.clientWidth")
            scroll_w = page.evaluate("() => document.documentElement.scrollWidth")
            client_w = page.evaluate("() => document.documentElement.clientWidth")
            print(f"  -> Resolution {res_name}: scrollWidth={scroll_w}, clientWidth={client_w}, overflow={has_hscroll}")
            assert not has_hscroll, f"Horizontal scrollbar detected at {res_name}! ({scroll_w} > {client_w})"

            # Check player aspect ratio and controls visibility
            frame_box = page.locator(".watch-player-frame").bounding_box()
            assert frame_box is not None, f"Player frame not rendered at {res_name}!"
            assert frame_box["width"] > 0 and frame_box["height"] > 0

            # On 390x844, check that volume slider is hidden to avoid crowding
            if w <= 640:
                slider_disp = page.evaluate("() => window.getComputedStyle(document.getElementById('volume-slider')).display")
                print(f"     Mobile volume slider display: {slider_disp}")
                assert slider_disp == "none", "Volume slider should be hidden on small screens!"
            else:
                slider_disp = page.evaluate("() => window.getComputedStyle(document.getElementById('volume-slider')).display")
                assert slider_disp != "none", "Volume slider should be visible on desktop screens!"

            # Capture 1440x900 canonical screenshot as required
            if res_name == "1440x900":
                screenshot_path = SCREENSHOT_DIR / "watch_page_cinema_1440x900.png"
                page.screenshot(path=str(screenshot_path), full_page=False)
                print(f"  -> Captured 1440x900 screenshot: {screenshot_path}")

        # 6. Non-Regression Audit on Other Viewer Pages (Home, Browse)
        print("\n[8/8] Non-Regression Audit on Other Viewer Pages...")
        # Reset viewport to 1440x900 for non-regression checks
        page.set_viewport_size({"width": 1440, "height": 900})

        # Check Home page has normal header & footer
        page.goto(f"{BASE_URL}/home", wait_until="domcontentloaded")
        page.wait_for_selector(".viewer-header", timeout=5000)
        home_header_text = page.locator(".viewer-header").inner_text()
        assert "الرئيسية" in home_header_text, "Home page header must contain 'الرئيسية' nav link!"
        assert "المكتبة" in home_header_text, "Home page header must contain 'المكتبة' nav link!"
        home_footer = page.locator(".viewer-footer")
        assert home_footer.count() > 0, "Home page MUST retain viewer-footer!"
        print("  -> Passed: Home page (/home): Navigation and footer are INTACT.")

        # Check Catalog / Browse page has normal header & footer
        page.goto(f"{BASE_URL}/catalog", wait_until="domcontentloaded")
        page.wait_for_selector(".viewer-header", timeout=5000)
        browse_header_text = page.locator(".viewer-header").inner_text()
        assert "الرئيسية" in browse_header_text, "Browse page header must contain 'الرئيسية' nav link!"
        browse_footer = page.locator(".viewer-footer")
        assert browse_footer.count() > 0, "Browse page MUST retain viewer-footer!"
        print("  -> Passed: Browse page (/catalog): Navigation and footer are INTACT.")

        # Check Console Errors
        print("\n=== Console & CSP Diagnostics ===")
        print(f"Console errors: {len(console_errors)}")
        print(f"CSP violations: {len(csp_violations)}")
        if console_errors:
            print("Errors detected:", console_errors)
        assert len(console_errors) == 0, f"Found console errors: {console_errors}"
        assert len(csp_violations) == 0, f"Found CSP violations: {csp_violations}"

        browser.close()

    print("\n==================================================")
    print(" ALL 8 ACCEPTANCE TESTS PASSED PERFECTLY!")
    print("==================================================")


if __name__ == "__main__":
    run_acceptance()
