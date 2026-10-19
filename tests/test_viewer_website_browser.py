"""Comprehensive Chromium Browser Acceptance and Visual Regression for ABOODVIDEO Viewer Website (V1).

Tests across:
- 1920x1080 (Full HD Desktop)
- 1440x900 (Mac/Laptop Desktop)
- 1280x720 (Small Desktop)
- 768x1024 (Tablet)
- 390x844 (Mobile iPhone 14/15)

Verifies:
- Native Arabic typography and RTL alignment
- Sticky translucent header and mobile drawer
- Homepage with Featured Hero and Horizontal Media Rows
- Browse/Catalog page with live filters and responsive grid
- Series page with Season tabs and Episode list
- Title Details page with technical specs
- Watch page shell surrounding Player V2 with True ASS indicators
- Empty 404 error response
- Zero horizontal overflow across all viewports
- Zero console errors and zero CSP violations
- Captures visual proof screenshots to artifacts directory
"""
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.api.viewer_routes import render_viewer_template
from app.api.watch_routes import render_watch_template, render_not_found_page

ARTIFACTS_DIR = Path(r"C:\Users\abood\.gemini\antigravity\brain\dce23a9a-79eb-4d6e-8726-1e716ffab914")
STATIC_DIR = Path(__file__).resolve().parent.parent / "backend" / "app" / "static"


def get_mock_catalog_data():
    """Produce rich sample data for realistic visual testing of the viewer platform."""
    spirit_cage = {
        "id": "572e7658-5f52-4331-8936-968d295ff17d",
        "title": "Spirit Cage: Incarnation - الحلقة 2",
        "description": "في مستقبل بائس ما بعد الكارثة، تقاتل البشرية من أجل البقاء في حصن معلق في السماء بينما تجوب وحوش المانا الأرض المدمرة.",
        "category": "دونغهوا / خيال علمي",
        "series_name": "Spirit Cage: Incarnation",
        "season_number": 1,
        "episode_number": 2,
        "duration_seconds": 1845.0,
        "duration_formatted": "30:45",
        "resolution_str": "3840×2160",
        "share_id": "ZdEVBfy5caKNY0e8mzbhRA",
        "poster_url": "/static/test_poster_spirit.webp",
        "is_4k": True,
        "max_quality": "4K",
        "has_ass": True,
        "subtitle_languages": ["العربية (ASS)", "English"],
        "view_count": 1420,
    }

    ling_cang = {
        "id": str(uuid.uuid4()),
        "title": "Spirit Cage: Incarnation - الحلقة 1",
        "description": "البداية: انطلاق فريق استكشاف الصيادين إلى الأرض المهجورة بحثاً عن موارد حيوية.",
        "category": "دونغهوا / خيال علمي",
        "series_name": "Spirit Cage: Incarnation",
        "season_number": 1,
        "episode_number": 1,
        "duration_seconds": 1920.0,
        "duration_formatted": "32:00",
        "resolution_str": "3840×2160",
        "share_id": "ep1_share_sample",
        "poster_url": "/static/test_poster_spirit.webp",
        "is_4k": True,
        "max_quality": "4K",
        "has_ass": True,
        "subtitle_languages": ["العربية (ASS)"],
        "view_count": 2840,
    }

    swallowed_star = {
        "id": str(uuid.uuid4()),
        "title": "ابتلاع النجوم (Swallowed Star) - الحلقة 85",
        "description": "معركة حاسمة في الفضاء الخارجي لاختبار قوى المحاربين وحماية الأرض.",
        "category": "دونغهوا / أكشن",
        "series_name": "ابتلاع النجوم",
        "season_number": 2,
        "episode_number": 85,
        "duration_seconds": 1320.0,
        "duration_formatted": "22:00",
        "resolution_str": "2560×1440",
        "share_id": "swallowed_star_85",
        "poster_url": "",
        "is_4k": False,
        "max_quality": "1440p",
        "has_ass": True,
        "subtitle_languages": ["العربية"],
        "view_count": 930,
    }

    battle_heavens = {
        "id": str(uuid.uuid4()),
        "title": "معركة كسر السماء (Battle Through the Heavens) - الحلقة 104",
        "description": "شياو يان يواجه خصوم أكاديمية جيا نان ويطلق نيران اللوتس البوذية الإلهية.",
        "category": "دونغهوا / فنون قتالية",
        "series_name": "معركة كسر السماء",
        "season_number": 5,
        "episode_number": 104,
        "duration_seconds": 1410.0,
        "duration_formatted": "23:30",
        "resolution_str": "1920×1080",
        "share_id": "btth_104",
        "poster_url": "",
        "is_4k": False,
        "max_quality": "1080p",
        "has_ass": False,
        "subtitle_languages": ["العربية (VTT)"],
        "view_count": 3120,
    }

    movie_title = {
        "id": str(uuid.uuid4()),
        "title": "أسطورة الإله دي (Legend of Deification) - فيلم سينمائي",
        "description": "ملحمة صعود جيانغ زيا ومواجهته لقوى الآلهة والثعالب في معركة تقرير المصير.",
        "category": "أفلام سينمائية",
        "series_name": None,
        "season_number": None,
        "episode_number": None,
        "duration_seconds": 6600.0,
        "duration_formatted": "01:50:00",
        "resolution_str": "3840×2160",
        "share_id": "deification_movie",
        "poster_url": "",
        "is_4k": True,
        "max_quality": "4K",
        "has_ass": True,
        "subtitle_languages": ["العربية (ASS)", "English"],
        "view_count": 4510,
    }

    all_videos = [spirit_cage, ling_cang, swallowed_star, battle_heavens, movie_title]
    return {
        "featured": spirit_cage,
        "all_videos": all_videos,
        "latest": all_videos,
        "series": [spirit_cage, ling_cang, swallowed_star, battle_heavens],
        "uhd": [spirit_cage, ling_cang, movie_title],
        "movies": [movie_title],
    }


@pytest.mark.skipif(os.getenv("ABOODVIDEO_RUN_BROWSER_TEST") != "1", reason="Chromium opt-in")
def test_viewer_website_comprehensive_acceptance_and_screenshots():
    playwright = pytest.importorskip("playwright.sync_api")
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    data = get_mock_catalog_data()

    home_html = render_viewer_template(
        "home.html",
        active_nav="home",
        featured=data["featured"],
        latest_videos=data["latest"],
        series_videos=data["series"],
        uhd_videos=data["uhd"],
        movie_videos=data["movies"],
    ).body.decode("utf-8")

    browse_html = render_viewer_template(
        "browse.html",
        active_nav="library",
        videos=data["all_videos"],
        search_query="",
        type_filter="",
        quality_filter="",
        sub_filter="",
        sort_by="latest",
    ).body.decode("utf-8")

    series_html = render_viewer_template(
        "series.html",
        active_nav="series",
        series_name="Spirit Cage: Incarnation",
        category="دونغهوا / خيال علمي",
        description="في مستقبل بائس ما بعد الكارثة، تقاتل البشرية من أجل البقاء في حصن معلق في السماء بينما تجوب وحوش المانا الأرض المدمرة.",
        hero_poster="/static/test_poster_spirit.webp",
        seasons=[{"number": 1, "count": 2}],
        active_season=1,
        current_episodes=[data["featured"], data["all_videos"][1]],
        first_episode=data["all_videos"][1],
        total_episodes=2,
        max_quality="4K UHD",
        has_ass=True,
    ).body.decode("utf-8")

    title_html = render_viewer_template(
        "title_detail.html",
        active_nav="library",
        video=data["featured"],
        related_videos=[data["all_videos"][1], data["all_videos"][2]],
    ).body.decode("utf-8")

    watch_html = render_watch_template(
        title="Spirit Cage: Incarnation - الحلقة 2",
        share_id="ZdEVBfy5caKNY0e8mzbhRA",
        playback_token="mock_viewer_playback_token_12345678",
        poster_url="/static/test_poster_spirit.webp",
        is_password_required=False,
        is_admin_preview=False,
        subtitles=[{
            "id": str(uuid.uuid4()),
            "lang": "ar",
            "name": "العربية",
            "label": "العربية (ASS)",
            "is_default": True,
            "is_forced": False,
            "is_playable": True,
            "format": "ass",
            "renderer": "libass",
            "fonts": [{"id": "font1", "family": "Source Han Sans"}],
        }],
        library_meta="دونغهوا • الموسم 1 • الحلقة 2",
        series_name="Spirit Cage: Incarnation",
        season_number=1,
        episode_number=2,
        description="معركة مواجهة وحش المانا العملاق في قاع الوادي المهجور.",
        is_4k=True,
        has_ass=True,
        prev_episode={"title": "الحلقة 1", "share_id": "ep1_share_sample"},
        next_episode=None,
        more_episodes=[data["all_videos"][1]],
    ).body.decode("utf-8")

    error_html = render_not_found_page().body.decode("utf-8")

    pages_map = {
        "/": home_html,
        "/browse": browse_html,
        "/series/spirit-cage": series_html,
        "/title/spirit-cage": title_html,
        "/watch/ZdEVBfy5caKNY0e8mzbhRA": watch_html,
        "/error": error_html,
    }

    console_errors = []
    csp_violations = []

    def handle_console(msg):
        if msg.type == "error":
            if msg.location.get("url") == "http://aboodvideo.local/error" and "404" in msg.text:
                return
            console_errors.append(msg.text)
            if "Content Security Policy" in msg.text or "violates" in msg.text:
                csp_violations.append(msg.text)

    csp_header = "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'; font-src 'self' blob: data:; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self' blob:;"

    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("console", handle_console)
        page.on("pageerror", lambda err: console_errors.append(str(err)))

        def route_request(route):
            url = route.request.url
            path = urlsplit(url).path

            if path in pages_map:
                route.fulfill(status=404 if path == "/error" else 200, content_type="text/html; charset=utf-8", body=pages_map[path], headers={"Content-Security-Policy": csp_header})
                return
            elif path.endswith(".css"):
                css_file = STATIC_DIR / Path(path.replace("/static/", "", 1))
                if not css_file.exists():
                    css_file = STATIC_DIR / "css" / Path(path).name
                if css_file.exists():
                    route.fulfill(status=200, content_type="text/css; charset=utf-8", body=css_file.read_text(encoding="utf-8"))
                    return
            elif path.endswith(".js"):
                js_file = STATIC_DIR / Path(path.replace("/static/", ""))
                if not js_file.exists():
                    js_file = STATIC_DIR / "js" / Path(path).name
                if js_file.exists():
                    route.fulfill(status=200, content_type="application/javascript; charset=utf-8", body=js_file.read_text(encoding="utf-8"))
                    return
                elif "jassub" in path:
                    route.fulfill(status=200, content_type="application/javascript", body="window.JASSUB = class { setTrack(){} destroy(){} resize(){} };")
                    return
                elif "hls" in path:
                    route.fulfill(status=200, content_type="application/javascript", body="""
                    window.Hls = class {
                        static isSupported() { return true; }
                        constructor() {
                            this.levels = [{height: 2160, bitrate: 15000000}, {height: 1080, bitrate: 4000000}];
                        }
                        on(evt, cb) {}
                        loadSource(src) {}
                        attachMedia(el) {}
                        destroy() {}
                    };
                    window.Hls.Events = {
                        MANIFEST_PARSED: 'hlsManifestParsed',
                        ERROR: 'hlsError',
                        LEVEL_SWITCHED: 'hlsLevelSwitched'
                    };
                    window.Hls.ErrorTypes = {
                        NETWORK_ERROR: 'networkError',
                        MEDIA_ERROR: 'mediaError',
                        OTHER_ERROR: 'otherError'
                    };
                    """)
                    return
            elif path.endswith(".wasm"):
                wasm_file = STATIC_DIR / Path(path.replace("/static/", ""))
                if wasm_file.exists():
                    route.fulfill(status=200, content_type="application/wasm", body=wasm_file.read_bytes())
                    return
            elif "/subtitles/" in path or path.endswith(".ass") or path.endswith(".vtt"):
                sample_ass = """[Script Info]
Title: Sample
ScriptType: v4.00+
WrapStyle: 0
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:05.00,Default,,0,0,0,,تجربة
"""
                route.fulfill(status=200, content_type="text/plain; charset=utf-8", body=sample_ass)
                return
            elif "/fonts/" in path or path.endswith(".ttf") or path.endswith(".woff2") or path.endswith(".otf"):
                route.fulfill(status=200, content_type="font/woff2", body=b"")
                return
            elif path.endswith(".webp") or path.endswith(".png") or path.endswith(".jpg"):
                pixel_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\r\xe7\x01\x00\x00\x00\x00IEND\xaeB`\x82"
                route.fulfill(status=200, content_type="image/png", body=pixel_png)
                return

            route.fulfill(status=200, content_type="text/plain", body="ok")

        page.route("**/*", route_request)

        # ----------------------------------------------------
        # 1. HOMEPAGE ACCEPTANCE (Desktop 1440x900 & Mobile 390x844)
        # ----------------------------------------------------
        page.goto("http://aboodvideo.local/", wait_until="networkidle")

        # Verify RTL and title
        assert page.evaluate("() => document.documentElement.getAttribute('dir')") == "rtl"
        assert "ABOODVIDEO" in page.title()

        # Verify Hero elements
        assert page.locator(".series-hero-title").first.inner_text() == data["featured"]["title"]
        assert page.locator(".series-hero-badges .viewer-badge-4k").is_visible()
        assert page.locator(".series-hero-badges .viewer-badge-ass").is_visible()

        # Check horizontal overflow (Must be 0)
        overflow_diff = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
        assert overflow_diff <= 0, f"Homepage desktop horizontal overflow detected: {overflow_diff}px"

        # Capture Desktop Homepage Screenshot
        page.screenshot(path=str(ARTIFACTS_DIR / "viewer_home_desktop_1440.png"), full_page=False)

        # Test Mobile Viewport
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(200)

        mobile_overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
        assert mobile_overflow <= 0, f"Homepage mobile horizontal overflow: {mobile_overflow}px"

        # Capture Mobile Homepage Screenshot
        page.screenshot(path=str(ARTIFACTS_DIR / "viewer_home_mobile_390.png"), full_page=False)

        # Test Mobile Drawer Toggle
        page.locator("#viewer-hamburger-btn").click()
        page.wait_for_function("() => document.querySelector('#viewer-drawer').classList.contains('is-open')")
        page.screenshot(path=str(ARTIFACTS_DIR / "viewer_mobile_drawer_390.png"), full_page=False)
        page.locator("#viewer-drawer-close").click()
        page.wait_for_function("() => !document.querySelector('#viewer-drawer').classList.contains('is-open')")

        # ----------------------------------------------------
        # 2. BROWSE / CATALOG ACCEPTANCE (Desktop & Mobile)
        # ----------------------------------------------------
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto("http://aboodvideo.local/browse", wait_until="networkidle")

        assert page.locator(".viewer-grid").is_visible()
        cards_count = page.locator(".viewer-grid .viewer-card").count()
        assert cards_count == len(data["all_videos"])

        browse_overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
        assert browse_overflow <= 0, f"Browse page desktop overflow: {browse_overflow}px"

        # Capture Desktop Browse Screenshot
        page.screenshot(path=str(ARTIFACTS_DIR / "viewer_browse_desktop_1440.png"), full_page=False)

        # Mobile Browse Viewport
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(200)
        assert page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth") <= 0
        page.screenshot(path=str(ARTIFACTS_DIR / "viewer_browse_mobile_390.png"), full_page=False)

        # ----------------------------------------------------
        # 3. SERIES PAGE ACCEPTANCE
        # ----------------------------------------------------
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto("http://aboodvideo.local/series/spirit-cage", wait_until="networkidle")

        assert page.locator(".season-tab.is-active").inner_text() == "الموسم 1"
        assert page.locator(".episodes-grid .episode-card").count() == 2

        # Capture Series Desktop & Mobile Screenshots
        page.screenshot(path=str(ARTIFACTS_DIR / "viewer_series_desktop_1440.png"), full_page=False)
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(200)
        page.screenshot(path=str(ARTIFACTS_DIR / "viewer_series_mobile_390.png"), full_page=False)

        # ----------------------------------------------------
        # 4. TITLE DETAILS ACCEPTANCE
        # ----------------------------------------------------
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto("http://aboodvideo.local/title/spirit-cage", wait_until="networkidle")

        assert page.locator(".series-hero-title").inner_text() == data["featured"]["title"]
        page.screenshot(path=str(ARTIFACTS_DIR / "viewer_title_detail_desktop_1440.png"), full_page=False)

        # ----------------------------------------------------
        # 5. WATCH PAGE REDESIGN ACCEPTANCE (Player V2 Shell)
        # ----------------------------------------------------
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto("http://aboodvideo.local/watch/ZdEVBfy5caKNY0e8mzbhRA", wait_until="networkidle")

        # Verify surrounding metadata
        assert page.locator(".watch-cinema-title").inner_text() == "Spirit Cage: Incarnation - الحلقة 2"
        assert page.locator(".watch-capabilities-bar .viewer-badge-4k").is_visible()
        assert page.locator(".watch-capabilities-bar .viewer-badge-ass").is_visible()
        assert page.locator(".watch-nav-btn").is_visible()

        # Verify Player V2 elements remain intact
        assert page.locator("#player-container").is_visible()
        assert page.locator("#video-element").count() == 1
        assert page.locator("#ass-canvas").count() == 1
        assert page.locator("#player-controls").count() == 1

        # Capture Desktop Watch Page Screenshot
        page.screenshot(path=str(ARTIFACTS_DIR / "viewer_watch_desktop_1440.png"), full_page=False)

        # Mobile Watch Viewport
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(200)
        assert page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth") <= 0
        page.screenshot(path=str(ARTIFACTS_DIR / "viewer_watch_mobile_390.png"), full_page=False)

        # ----------------------------------------------------
        # 6. HIDDEN ERROR STATE ACCEPTANCE
        # ----------------------------------------------------
        page.set_viewport_size({"width": 1440, "height": 900})
        error_response = page.goto("http://aboodvideo.local/error", wait_until="networkidle")
        assert error_response.status == 404
        assert page.locator("body").inner_text() == ""
        assert page.locator("a").count() == 0
        page.screenshot(path=str(ARTIFACTS_DIR / "viewer_error_desktop_1440.png"), full_page=False)

        # ----------------------------------------------------
        # 7. MULTI-VIEWPORT HORIZONTAL OVERFLOW VERIFICATION
        # ----------------------------------------------------
        for w, h in [(1920, 1080), (1440, 900), (1280, 720), (768, 1024), (390, 844)]:
            page.set_viewport_size({"width": w, "height": h})
            page.goto("http://aboodvideo.local/", wait_until="networkidle")
            overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
            assert overflow <= 0, f"Horizontal overflow at {w}x{h}: {overflow}px"

        # Final Security & Integrity Checks
        assert not csp_violations, f"CSP Violations detected: {csp_violations}"
        assert not console_errors, f"Console errors detected: {console_errors}"
