"""Opt-in Chromium coverage for Phase 6.0 authentication and XSS regressions."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest


pytestmark = pytest.mark.skipif(
    os.getenv("ABOODVIDEO_RUN_BROWSER_TEST") != "1",
    reason="set ABOODVIDEO_RUN_BROWSER_TEST=1 to run Chromium security regressions",
)


def app_root() -> Path:
    root = Path(__file__).resolve().parent.parent / "backend" / "app"
    return root if root.exists() else Path("/app/app")


def test_login_failures_are_visible_and_button_recovers_in_browser() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    root = app_root()
    template = (root / "templates" / "admin_login.html").read_text(encoding="utf-8")
    script = (root / "static" / "js" / "admin_login.js").read_text(encoding="utf-8")
    css = (root / "static" / "css" / "admin_login.css").read_text(encoding="utf-8")
    requests, page_errors, console_errors = [], [], []
    response_status = [401]

    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error" and not any(f"status of {code}" in message.text for code in (401, 503))
            else None,
        )

        def route(route):
            request = route.request
            path = urlsplit(request.url).path
            if path.endswith("admin_login.js"):
                route.fulfill(status=200, content_type="application/javascript", body=script)
            elif path.endswith("admin_login.css"):
                route.fulfill(status=200, content_type="text/css", body=css)
            elif path.endswith("common.css"):
                route.fulfill(status=200, content_type="text/css", body=".hidden { display: none !important; }")
            elif request.url.endswith("/api/v1/auth/login"):
                requests.append(request)
                status = response_status[0]
                body = {"success": False, "error": {"code": "AUTH_INVALID_CREDENTIALS", "message": "Invalid username or password."}}
                if status == 503:
                    body = {"success": False, "error": {"code": "SYSTEM_UNAVAILABLE", "message": "Authentication service is temporarily unavailable."}}
                route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
            elif path.startswith("/static/"):
                artifact=root/path.lstrip("/")
                route.fulfill(status=200 if artifact.is_file() else 404, content_type="application/javascript" if artifact.suffix==".js" else "text/css", body=artifact.read_text(encoding="utf-8") if artifact.is_file() else "")
            else:
                route.fulfill(status=200, content_type="text/html", body=template)

        page.route("**/*", route)
        page.goto("http://login-regression.test/admin/login")
        page.locator("#username").fill("wrong-user")
        page.locator("#password").fill("wrong-password")
        page.locator("#login-submit").click()
        sync_api.expect(page.locator("#login-error")).to_be_visible()
        assert "اسم المستخدم" in page.locator("#login-error").inner_text()
        assert page.locator("#login-submit").is_enabled()
        assert len(requests) == 1
        response_status[0] = 503
        page.locator("#password").fill("wrong-password")
        page.locator("#login-submit").click()
        sync_api.expect(page.locator("#login-error")).to_contain_text("خدمة تسجيل الدخول")
        assert page.locator("#login-submit").is_enabled()
        assert not page_errors
        assert not console_errors
        browser.close()


def test_video_metadata_is_rendered_as_text_not_markup_in_browser() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    root = app_root()
    template = (root / "templates" / "admin_videos.html").read_text(encoding="utf-8")
    script = (root / "static" / "js" / "admin_videos.js").read_text(encoding="utf-8")
    hostile = '<img src=x onerror="window.__xss = true">'

    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()

        def route(route):
            request = route.request
            path = urlsplit(request.url).path
            if path.endswith("admin_videos.js"):
                route.fulfill(status=200, content_type="application/javascript", body=script)
            elif path.endswith(".js"):
                route.fulfill(status=200, content_type="application/javascript", body="")
            elif path.endswith("/api/v1/admin/videos"):
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"items": [{"id": "safe-id", "title": hostile, "source_filename": hostile, "status": "UPLOADED", "visibility": "PRIVATE", "source_size_bytes": 1, "view_count": 0, "available_renditions": [], "rendition_count": 0, "subtitle_count": 0}], "total": 1}))
            elif path.endswith(".css"):
                route.fulfill(status=200, content_type="text/css", body="")
            else:
                route.fulfill(status=200, content_type="text/html", body=template)

        page.route("**/*", route)
        page.goto("http://videos-regression.test/admin/videos")
        page.wait_for_function("() => document.querySelector('#videos-tbody strong') !== null")
        assert page.locator("#videos-tbody").inner_text().count(hostile) == 2
        assert page.locator("#videos-tbody img").count() == 0
        assert page.evaluate("() => window.__xss === true") is False
        browser.close()
