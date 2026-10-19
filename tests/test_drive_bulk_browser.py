"""Opt-in browser regression for visible selection and verified Trash mutations."""
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest


pytestmark = pytest.mark.skipif(os.getenv("ABOODVIDEO_RUN_BROWSER_TEST") != "1", reason="Chromium opt-in")


def test_drive_bulk_trash_restore_and_mobile_controls():
    playwright = pytest.importorskip("playwright.sync_api")
    app_root = Path(__file__).resolve().parent.parent / "backend" / "app"
    from jinja2 import Environment, FileSystemLoader
    template = Environment(loader=FileSystemLoader(app_root / "templates"), autoescape=True).get_template("drive.html").render(active_nav="videos", username="Synthetic owner", user_role="OWNER", csrf_cookie_name="custom-csrf.test")
    script = (app_root / "static" / "js" / "drive.js").read_text(encoding="utf-8")
    test_items = [
        {"id": f"00000000-0000-4000-8000-{index:012d}", "type": "video", "name": f"Test Video {index}", "type_label": "فيديو"}
        for index in range(25)
    ]
    trashed = set()
    requests = []
    errors = []
    scope_trace = []

    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1200, "height": 800})
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route_request(route):
            request = route.request
            parsed = urlsplit(request.url)
            path = parsed.path
            if path.endswith("/static/js/drive.js"):
                route.fulfill(status=200, content_type="application/javascript", body=script)
            elif path.endswith(".js"):
                asset = app_root / "static" / "js" / Path(path).name
                route.fulfill(status=200, content_type="application/javascript", body=asset.read_text(encoding="utf-8") if asset.is_file() else "")
            elif path.endswith(".css"):
                stylesheet = app_root / "static" / "css" / Path(path.split("/static/css/", 1)[-1])
                route.fulfill(status=200, content_type="text/css", body=stylesheet.read_text(encoding="utf-8"))
            elif path.endswith("/api/v1/drive/tree"):
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"tree": [], "trash_count": len(trashed)}))
            elif path.endswith("/api/v1/drive/scope"):
                content_filter = parse_qs(parsed.query).get("filter", ["content"])[0]
                if content_filter == "trash":
                    items = [{**item, "previous_location": "مكتبتي"} for item in test_items if item["id"] in trashed]
                elif content_filter in {"test", "all"}:
                    items = [item for item in test_items if item["id"] not in trashed]
                else:
                    items = []
                scope_trace.append({"url": request.url, "filter": content_filter, "mock_items": len(items)})
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"items": items, "breadcrumbs": [], "total_items": len(items)}))
            elif path.endswith("/api/v1/drive/trash"):
                assert request.headers.get("x-csrf-token") == "synthetic-csrf-token"
                payload = request.post_data_json
                requests.append(payload)
                for item in payload["items"]:
                    trashed.add(item["id"])
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"success": True, "count": len(payload["items"])}))
            elif path.endswith("/api/v1/drive/restore"):
                payload = request.post_data_json
                for item in payload["items"]:
                    trashed.remove(item["id"])
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"success": True, "count": len(payload["items"])}))
            else:
                route.fulfill(status=200, content_type="text/html", body=template)

        page.route("**/*", route_request)
        page.context.add_cookies([{"name": "custom-csrf.test", "value": "synthetic-csrf-token", "url": "http://drive-bulk.test"}])
        page.goto("http://drive-bulk.test/preview", wait_until="networkidle")
        assert page.locator("#btnDetectMedia").count() == 0
        assert "الفلتر الحالي" in page.locator(".empty-state").inner_text()
        page.locator("#showAllEmptyResults").click()
        page.wait_for_function("() => document.querySelectorAll('.drive-row').length === 25")
        assert page.locator("#currentFilterLabel").inner_text() == "الكل"
        page.locator("#btnFilterDropdown").click()
        page.locator('[data-filter="test"]').click()
        try:
            page.wait_for_function("() => document.querySelectorAll('.drive-row').length === 25")
        except Exception:
            import json as _json
            print("DRIVE_PIPELINE=" + _json.dumps({
                "requests": scope_trace,
                "mock_returned": scope_trace[-1]["mock_items"] if scope_trace else 0,
                "rendered_rows": page.locator(".drive-row").count(),
                "item_count_badge": page.locator("#itemCountBadge").inner_text() if page.locator("#itemCountBadge").count() else None,
                "selected_filter": page.locator("#filterDropdownMenu .dropdown-item.is-active").get_attribute("data-filter") if page.locator("#filterDropdownMenu .dropdown-item.is-active").count() else None,
                "page_errors": errors,
                "console": page.locator("body").inner_text()[-800:],
            }, ensure_ascii=True))
            raise
        page.locator(".drive-row").first.wait_for()
        assert page.locator(".drive-row").count() == 25

        page.locator(".drive-row .drive-checkbox").first.check()
        assert page.locator("#selectAllCheckbox").evaluate("el => el.indeterminate")
        page.locator("#selectAllCheckbox").check()
        assert page.locator(".drive-row.is-selected").count() == 25
        assert page.locator("#selectAllCheckbox").is_checked()
        page.locator("#btnMultiTrash").click()
        assert "25" in page.locator("#deleteModalDesc").inner_text()
        page.locator("#btnConfirmDelete").click()
        page.wait_for_function("() => document.querySelectorAll('.drive-row').length === 0 && document.querySelector('#trashCountBadge').textContent === '25'")
        assert len(requests) == 1 and len(requests[0]["items"]) == 25
        assert page.locator("#toastContainer").evaluate("el => {const r=el.getBoundingClientRect(); return getComputedStyle(el).position === 'fixed' && r.left >= 0 && r.right <= innerWidth && r.bottom <= innerHeight;}")

        page.locator("#btnQuickTrash").click()
        try:
            page.wait_for_function("() => document.querySelectorAll('.drive-row').length === 25")
        except Exception:
            import json as _json
            print("DRIVE_TRASH_PIPELINE=" + _json.dumps({
                "scope_requests": scope_trace,
                "mock_returned": scope_trace[-1]["mock_items"] if scope_trace else 0,
                "rendered_rows": page.locator(".drive-row").count(),
                "selected_filter": page.locator("#filterDropdownMenu .dropdown-item.is-active").get_attribute("data-filter") if page.locator("#filterDropdownMenu .dropdown-item.is-active").count() else None,
                "trashed_ids": len(trashed),
                "page_errors": errors,
                "body": page.locator("body").inner_text()[-600:],
            }, ensure_ascii=True))
            raise
        page.locator(".drive-row .drive-checkbox").nth(0).check()
        page.locator("#btnMultiPermanentDelete").click()
        assert not page.locator("#btnConfirmPermanentDelete").is_enabled()
        page.locator("#permanentDeleteConfirmation").fill("حذ")
        assert not page.locator("#btnConfirmPermanentDelete").is_enabled()
        page.locator("#permanentDeleteConfirmation").fill("حذف")
        assert page.locator("#btnConfirmPermanentDelete").is_enabled()
        page.locator("#btnCancelPermanentDelete").click()
        page.locator(".drive-row .drive-checkbox").nth(1).check()
        page.locator("#btnMultiRestore").click()
        page.wait_for_function("() => document.querySelectorAll('.drive-row').length === 23 && document.querySelector('#trashCountBadge').textContent === '23'")
        assert len(trashed) == 23

        page.set_viewport_size({"width": 390, "height": 844})
        page.locator("#sidebarToggleBtn").click()
        assert page.locator("body").evaluate("el => el.classList.contains('sidebar-open')")
        assert page.locator("#adminSidebar").is_visible()
        page.locator(".sidebar-backdrop").click(position={"x":10,"y":10})
        page.locator("#btnFilterDropdown").click()
        page.locator('[data-filter="test"]').click()
        page.wait_for_function("() => document.querySelectorAll('.drive-row').length === 2")
        assert page.locator("#mobileSelectAllCheckbox").is_visible()
        assert page.locator(".drive-row .drive-checkbox").first.is_visible()
        page.locator("#mobileSelectAllCheckbox").check()
        assert page.locator(".drive-row.is-selected").count() == 2
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.locator(".drive-row .action-dots-btn").first.click()
        assert page.locator("#unifiedContextMenu").evaluate("el => { const r=el.getBoundingClientRect(); return r.left >= 0 && r.right <= window.innerWidth; }")
        page.locator("#btnMultiTrash").click()
        assert page.locator("#deleteModal .modal-card").evaluate("el => { const r=el.getBoundingClientRect(); return r.left >= 0 && r.right <= window.innerWidth; }")
        page.locator("#btnCancelDelete").click()
        assert not errors
        browser.close()


def test_drive_partial_trash_keeps_failed_row_selected():
    playwright = pytest.importorskip("playwright.sync_api")
    app_root = Path(__file__).resolve().parent.parent / "backend" / "app"
    from jinja2 import Environment, FileSystemLoader
    template = Environment(loader=FileSystemLoader(app_root / "templates"), autoescape=True).get_template("drive.html").render(active_nav="videos", username="Synthetic owner", user_role="OWNER")
    script = (app_root / "static" / "js" / "drive.js").read_text(encoding="utf-8")
    items = [{"id": f"00000000-0000-4000-8000-{index:012d}", "type": "video", "name": f"Test {index}"} for index in range(3)]
    trashed = set()

    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(headless=True)
        page = browser.new_page()

        def route_request(route):
            request = route.request
            path = urlsplit(request.url).path
            if path.endswith("/static/js/drive.js"):
                route.fulfill(status=200, content_type="application/javascript", body=script)
            elif path.endswith(".js"):
                route.fulfill(status=200, content_type="application/javascript", body="")
            elif path.endswith(".css"):
                stylesheet = app_root / "static" / "css" / Path(path.split("/static/css/", 1)[-1])
                route.fulfill(status=200, content_type="text/css", body=stylesheet.read_text(encoding="utf-8"))
            elif path.endswith("/api/v1/drive/tree"):
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"tree": [], "trash_count": len(trashed)}))
            elif path.endswith("/api/v1/drive/scope"):
                visible = [item for item in items if item["id"] not in trashed]
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"items": visible, "breadcrumbs": []}))
            elif path.endswith("/api/v1/drive/trash"):
                trashed.update(item["id"] for item in request.post_data_json["items"][:2])
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"success": True, "count": 2}))
            else:
                route.fulfill(status=200, content_type="text/html", body=template)

        page.route("**/*", route_request)
        page.goto("http://drive-partial.test/preview", wait_until="networkidle")
        page.locator("#selectAllCheckbox").check()
        page.locator("#btnMultiTrash").click()
        page.locator("#btnConfirmDelete").click()
        page.wait_for_function("() => document.querySelectorAll('.drive-row').length === 1")
        assert page.locator(".drive-row.is-selected").count() == 1
        assert "تعذر نقل 1" in page.locator("#toastContainer").inner_text()
        browser.close()
