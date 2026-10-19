"""Opt-in Chromium coverage for the real library controls, not only page load."""
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest


@pytest.mark.skipif(os.getenv("ABOODVIDEO_RUN_BROWSER_TEST") != "1", reason="Chromium opt-in")
def test_library_filters_card_view_details_and_bulk_action():
    playwright = pytest.importorskip("playwright.sync_api")
    app_root = Path(__file__).resolve().parent.parent / "backend" / "app"
    template = (app_root / "templates" / "admin_videos.html").read_text(encoding="utf-8")
    script = (app_root / "static" / "js" / "admin_videos.js").read_text(encoding="utf-8")
    video = {
        "id": "11111111-1111-4111-8111-111111111111", "title": "4K Sample", "source_filename": "sample.mp4",
        "description": None, "status": "READY", "visibility": "PRIVATE", "share_id": None,
        "source_size_bytes": 1024, "source_resolution_width": 3840, "source_resolution_height": 2160,
        "source_codec": "h264", "view_count": 5, "subtitle_count": 1, "rendition_count": 1,
        "available_renditions": [{"quality": "2160p", "width": 3840, "height": 2160, "codec": "h264", "bitrate": 16000000}],
        "category": "Animation", "series_name": "SILVER", "season_number": 1, "episode_number": 2,
    }
    search_terms = []
    bulk_payloads = []
    errors = []
    api_trace = []

    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(headless=True)
        context = browser.new_context()
        context.add_cookies([{"name": "aboodvideo_csrf", "value": "test-csrf", "url": "http://library-upgrade.test/"}])
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append("console: " + message.text) if message.type == "error" else None)

        def route_request(route):
            request = route.request
            parsed = urlsplit(request.url)
            if parsed.path.endswith("/static/js/admin_videos.js"):
                route.fulfill(status=200, content_type="application/javascript; charset=utf-8", body=script)
            elif parsed.path.endswith(".js"):
                route.fulfill(status=200, content_type="application/javascript", body="")
            elif parsed.path.endswith(".css"):
                stylesheet = app_root / "static" / "css" / Path(parsed.path.split("/static/css/", 1)[-1])
                route.fulfill(status=200, content_type="text/css", body=stylesheet.read_text(encoding="utf-8"))
            elif parsed.path.endswith("/api/v1/admin/videos/bulk"):
                bulk_payloads.append(request.post_data_json)
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"updated": 1, "results": [{"id": video["id"], "ok": True}]}))
            elif parsed.path.endswith("/api/v1/admin/videos"):
                search_terms.append(parse_qs(parsed.query).get("q", [""])[0])
                api_trace.append({"url": request.url, "mock_items": 1, "quality": video["available_renditions"][-1]["quality"], "status": video["status"]})
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"items": [video], "total": 1}))
            else:
                route.fulfill(status=200, content_type="text/html", body=template)

        page.route("**/*", route_request)
        page.goto("http://library-upgrade.test/admin/videos", wait_until="networkidle")
        print("LIBRARY_PIPELINE=" + json.dumps({
            "api": api_trace,
            "frontend_quality": page.evaluate("typeof topQuality === 'function' ? topQuality({available_renditions:[{quality:'2160p'}]}) : 'not-loaded'"),
            "frontend_status": page.evaluate("typeof renderTable === 'function' ? 'loaded' : 'not-loaded'"),
            "rendered_row_text": page.locator("#videos-tbody").inner_text(),
            "ready_badge_text": page.locator("#videos-tbody .badge.status-ready").all_text_contents(),
            "page_errors": errors,
        }, ensure_ascii=True))
        ready_badge = page.locator("#videos-tbody .badge.status-ready")
        assert ready_badge.count() == 1
        assert ready_badge.text_content() == "4K جاهز"
        page.locator("#view-cards").click()
        assert page.locator("#videos-cards .video-card").is_visible()
        page.locator("#videos-cards [data-action=details-video]").click()
        assert page.locator("#details-content").get_by_text("4K · 2160p", exact=False).is_visible()
        page.locator("#close-details").click()
        page.locator("#search-input").fill("Sample")
        page.wait_for_function("() => document.querySelector('#search-input').value === 'Sample'")
        page.wait_for_timeout(350)
        assert "Sample" in search_terms
        page.locator("#videos-cards .video-select").check()
        page.locator("#bulk-action").select_option("visibility")
        page.locator("#bulk-visibility").select_option("PUBLIC")
        page.locator("#bulk-apply").click()
        page.locator("#confirm-yes").click()
        page.wait_for_function("() => document.querySelector('#library-feedback').textContent.includes('اكتمل الإجراء')")
        assert bulk_payloads == [{"ids": [video["id"]], "action": "visibility", "visibility": "PUBLIC"}]
        assert not errors
        browser.close()
