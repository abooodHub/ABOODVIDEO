"""Studio queue/import browser regressions with every network request intercepted."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.skipif(os.getenv("ABOODVIDEO_RUN_BROWSER_TEST") != "1", reason="Chromium opt-in")


@pytest.mark.parametrize("kind", ["file", "season", "series"])
def test_studio_import_and_bulk_queue_delete_in_browser(kind):
    playwright = pytest.importorskip("playwright.sync_api")
    root = Path(__file__).resolve().parents[1] / "studio" / "web"
    html = (root / "templates" / "index_v3.html").read_text(encoding="utf-8").replace("{{ version }}", "audit")
    config = {"converted_output_folder": "D:\\audit-output", "first_run_complete": True, "default_encoder": "cpu", "default_preset": "balanced", "processing_mode": "local"}
    source = "D:\\audit-source\\Season 01\\01.mkv" if kind == "file" else "D:\\audit-source"
    episode_path = source if kind == "file" else source + "\\Season 01\\01.mkv"
    rows, imports, deletions, uploads, actions, errors = [], [], [], [], [], []
    def batch():
        return {"active": bool(rows), "episodes": list(rows), "runner": {"is_running": False}, "summary": {}, "batch": {"id": "audit-batch", "status": "PAUSED"} if rows else None}
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("window.WebSocket = class { constructor() {} close() {} };")
        def route(route):
            request = route.request
            path = urlsplit(request.url).path
            if path.startswith("/static/"):
                artifact = root / path.lstrip("/")
                if artifact.is_file():
                    route.fulfill(status=200, content_type="application/javascript" if artifact.suffix == ".js" else "text/css", body=artifact.read_text(encoding="utf-8"))
                else:
                    route.fulfill(status=404, body="")
                return
            payload = {}
            if path == "/api/config":
                payload = {"config": config} if request.method == "POST" else config
            elif path == "/api/batch/current":
                payload = batch()
            elif path in {"/api/batch/import", "/api/batch/import-series", "/api/batch/import-file"}:
                imports.append(request.post_data_json)
                if len(imports) == 1:
                    page.evaluate("() => {renderSelection();window.auditButtonDisabled=document.querySelector('#queue-selection').disabled;void queueSelection();}")
                rows.append({"id": "audit-episode", "filename": "01.mkv", "source_path": episode_path, "state": "PENDING", "episode_number": 1, "order_index": 1, "settings": {"series_name": "Audit", "season_number": "01"}})
                payload = {"ok": True, **batch(), "new_episode_count": 1}
            elif path == "/api/batch/episodes/delete-bulk":
                deletions.append(request.post_data_json)
                rows.clear()
                payload = {"ok": True, "removed": 1}
            elif path == "/api/batch/upload-ready":
                uploads.append(request.post_data_json)
                payload = {"ok": True, "status": "STARTED"}
            elif path == "/api/batch/stop-after-current":
                actions.append(path)
                payload = {"ok": True}
            elif path == "/api/encoders":
                payload = {"encoders": []}
            elif path == "/api/telemetry":
                payload = {"hardware": {}}
            elif path.startswith("/api/"):
                payload = {"ok": True}
            else:
                route.fulfill(status=200, content_type="text/html", body=html)
                return
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
        page.route("**/*", route)
        page.goto("http://127.0.0.1:18555/", wait_until="networkidle")
        page.evaluate("([type,path]) => {state.selection={type,path,discovery:{episode_count:1,seasons:[]}};renderSelection();showPage('import');}", [kind, source])
        page.locator("#queue-selection").click()
        page.wait_for_function("() => !state.importBusy && state.selection === null")
        assert len(imports) == 1
        assert page.evaluate("window.auditButtonDisabled")
        assert "تمت إضافة 1" in page.locator("#toasts").inner_text()
        assert page.locator("#page-queue").evaluate("el=>el.classList.contains('active')")
        page.locator("#select-all-queue").check()
        assert page.locator("#delete-selected-queue").is_enabled()
        page.on("dialog", lambda dialog: dialog.accept())
        page.locator("#delete-selected-queue").click()
        page.wait_for_function("() => state.batch.episodes.length === 0")
        assert len(deletions) == 1
        assert deletions[0]["episode_ids"] == ["audit-episode"]
        rows.append({"id": "upload-episode", "filename": "ready.mkv", "source_path": episode_path, "work_dir": "D:\\audit-output\\ready", "state": "READY_TO_UPLOAD", "episode_number": 2, "order_index": 2, "settings": {"series_name": "Audit", "season_number": "01"}})
        page.evaluate("() => refreshBatch()")
        page.wait_for_function("() => !document.querySelector('#upload-ready').disabled")
        page.locator("#upload-tab").click()
        page.locator("#upload-ready").click()
        page.wait_for_function("() => document.querySelector('#upload-ready').textContent === 'رفع الجاهز الآن'")
        assert len(uploads) == 1
        page.locator("#processing-tab").click()
        page.locator(".queue-more-actions summary").click()
        page.locator('[data-action="stop-after-current"]').click()
        page.wait_for_function('() => !document.querySelector("[data-action=stop-after-current]").disabled')
        assert actions == ["/api/batch/stop-after-current"]
        assert not errors
        browser.close()
