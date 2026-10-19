"""Opt-in Chromium coverage for the actual Drive password controls and payloads."""
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.skipif(os.getenv("ABOODVIDEO_RUN_BROWSER_TEST") != "1", reason="Chromium opt-in")


def test_share_password_controls_create_preserve_and_remove_protection():
    browser_api = pytest.importorskip("playwright.sync_api")
    from app.api.admin_routes import render_admin_template

    root = Path(__file__).resolve().parents[1] / "backend/app"
    html = render_admin_template("drive.html", active_nav="content", page_id="content").body.decode("utf-8")
    script = (root / "static/js/drive.js").read_text(encoding="utf-8")
    item = {"id": "00000000-0000-4000-8000-000000000001", "type": "video", "name": "Synthetic shared video",
            "type_label": "فيديو", "status": "READY", "visibility": "UNLISTED"}
    payloads, errors = [], []
    current = {"exists": False}
    with browser_api.sync_playwright() as runner:
        browser = runner.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route_request(route):
            request = route.request
            path = urlsplit(request.url).path
            if path == "/static/js/drive.js":
                route.fulfill(status=200, content_type="application/javascript", body=script)
            elif path.endswith(".js"):
                route.fulfill(status=200, content_type="application/javascript", body="")
            elif path.endswith(".css"):
                file = root / "static" / path.removeprefix("/static/")
                route.fulfill(status=200, content_type="text/css", body=file.read_text(encoding="utf-8") if file.is_file() else "")
            elif path == "/api/v1/drive/tree":
                route.fulfill(status=200, json={"tree": [], "trash_count": 0})
            elif path == "/api/v1/drive/scope":
                route.fulfill(status=200, json={"items": [item], "breadcrumbs": [], "total_items": 1})
            elif path.startswith("/api/v1/drive/shares/target/"):
                route.fulfill(status=200, json=current)
            elif path == "/api/v1/drive/shares" and request.method == "POST":
                payload = request.post_data_json
                payloads.append(payload)
                current.update(exists=True, token="synthetic-share", share_url="/preview/share/synthetic-share",
                               allow_download=payload["allow_download"])
                if "password" in payload:
                    current["password_required"] = payload["password"] is not None
                route.fulfill(status=200, json={"success": True, **current})
            elif path.startswith("/api/"):
                route.fulfill(status=200, json={})
            elif path.endswith(".svg"):
                route.fulfill(status=200, content_type="image/svg+xml", body='<svg xmlns="http://www.w3.org/2000/svg"/>')
            else:
                route.fulfill(status=200, content_type="text/html", body=html)

        page.route("**/*", route_request)
        page.goto("http://share-security.test/admin/videos", wait_until="networkidle")

        def open_modal():
            page.locator(".drive-row").first.click(button="right")
            page.locator("#ctxMenuShare").click()
            page.locator("#shareModal.is-open").wait_for(state="visible")

        open_modal()
        assert page.locator("#sharePasswordInput").is_disabled()
        page.locator("label:has(#sharePasswordToggle)").click()
        page.locator("#btnGenerateShare").click()
        assert payloads == []
        page.locator("#sharePasswordInput").fill("BrowserSharePassword!")
        page.locator("#btnGenerateShare").click()
        page.wait_for_function("() => document.querySelector('#sharePasswordInput').value === ''")
        assert payloads[0]["password"] == "BrowserSharePassword!"
        page.locator("#btnCancelShare").click()
        open_modal()
        assert page.locator("#sharePasswordToggle").is_checked()
        page.locator("#btnGenerateShare").click()
        page.wait_for_timeout(150)
        assert "password" not in payloads[1]
        page.locator("label:has(#sharePasswordToggle)").click()
        page.locator("#btnGenerateShare").click()
        page.wait_for_timeout(150)
        assert payloads[2]["password"] is None
        assert errors == []
        browser.close()
