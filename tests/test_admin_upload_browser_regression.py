"""Opt-in Chromium regression coverage for the admin upload interaction."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest


@pytest.mark.skipif(
    os.getenv("ABOODVIDEO_RUN_BROWSER_TEST") != "1",
    reason="set ABOODVIDEO_RUN_BROWSER_TEST=1 to run the Chromium upload regression",
)
def test_upload_start_click_creates_resumable_session_in_browser() -> None:
    """A selected file must expose Start and make the real initiate request."""
    sync_api = pytest.importorskip("playwright.sync_api")
    project_root = Path(__file__).resolve().parent.parent
    app_root = project_root / "backend" / "app"
    if not app_root.exists():
        app_root = project_root / "app"
    template = (app_root / "templates" / "admin_upload.html").read_text(encoding="utf-8")
    script = (app_root / "static" / "js" / "admin_upload.js").read_text(encoding="utf-8")
    requests: list[tuple[str, str, dict]] = []
    conversions: list[dict] = []
    page_errors: list[str] = []
    console_errors: list[str] = []

    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.add_cookies([
            {
                "name": "aboodvideo_csrf",
                "value": "browser-test-csrf",
                "url": "http://upload-regression.test/",
            }
        ])
        page = context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)

        def route_request(route):
            request = route.request
            path = urlsplit(request.url).path
            if path.endswith("/static/js/admin_upload.js"):
                route.fulfill(status=200, content_type="application/javascript", body=script)
            elif path.endswith(".js"):
                route.fulfill(status=200, content_type="application/javascript", body="")
            elif path == "/api/v1/admin/quality-settings/encoder":
                route.fulfill(status=200,content_type="application/json",body=json.dumps({"preset":"veryfast","video_bitrate_percent":100,"available_qualities":["360p","480p"],"enabled_qualities":["360p","480p"],"four_k_allowed":False}))
            elif request.url.endswith("/api/v1/uploads/initiate"):
                requests.append((request.method, request.url, request.post_data_json))
                route.fulfill(
                    status=201,
                    content_type="application/json",
                    body=json.dumps({"upload_id": "browser-regression-upload", "video_id": "browser-regression-video", "total_chunks": 1}),
                )
            elif request.url.endswith("/chunk"):
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"status": "UPLOADING"}))
            elif request.url.endswith("/finalize"):
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"video_id": "browser-regression-video", "resolution_width": 320, "resolution_height": 240, "duration_seconds": 1, "codec": "h264"}),
                )
            elif path.endswith("/transcode"):
                conversions.append(request.post_data_json)
                route.fulfill(status=202,content_type="application/json",body=json.dumps({"job_id":"browser-conversion"}))
            elif path.endswith(".css"):
                route.fulfill(status=200, content_type="text/css", body=".hidden { display: none !important; }")
            else:
                route.fulfill(status=200, content_type="text/html", body=template)

        page.route("**/*", route_request)
        page.goto("http://upload-regression.test/admin/upload", wait_until="networkidle")
        assert page.locator("#upload-processing-controls").is_hidden()
        assert page.locator("#upload-processing-mode").count() == 0
        page.locator("#file-input-files").set_input_files({
            "name": "browser-regression.mp4",
            "mimeType": "video/mp4",
            "buffer": b"browser-regression-video",
        })
        assert page.locator("#queue-section").is_visible()
        assert page.locator(".upload-item").count() == 1
        assert page.locator("#btn-start-batch").is_visible()

        assert page.locator("#upload-processing-controls").is_visible()
        page.wait_for_function("!document.getElementById('upload-encoder-profile').disabled")
        assert conversions == []
        page.locator("#btn-start-batch").click()
        page.locator(".item-msg-success").wait_for()

        assert len(requests) == 1
        method, _, payload = requests[0]
        assert method == "POST"
        assert payload["filename"] == "browser-regression.mp4"
        assert payload["file_size_bytes"] == len(b"browser-regression-video")
        assert payload["visibility"] == "PRIVATE"
        assert page.locator(".item-msg-success").is_visible()
        assert len(requests) == 1  # Success must not silently initiate the same file again.
        assert conversions == [{"preset":"veryfast","video_bitrate_percent":100,"enabled_qualities":["360p","480p"]}]
        assert not page_errors
        assert not console_errors
        browser.close()
