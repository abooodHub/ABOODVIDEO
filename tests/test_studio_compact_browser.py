"""Exercise the actual Studio HTTP UI with an isolated local profile."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest


pytestmark = pytest.mark.skipif(os.getenv("ABOODVIDEO_RUN_BROWSER_TEST") != "1", reason="Chromium opt-in")


def test_compact_studio_http_workflow(tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    from studio.config import save_studio_config

    output = tmp_path / "converted"
    output.mkdir()
    save_studio_config({"server_url": "http://127.0.0.1:1/api/v1", "converted_output_folder": str(output)})
    source = tmp_path / "Sample Series" / "Season 01" / "S01E01.mp4"
    source.parent.mkdir(parents=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=320x180:r=24", "-t", "1", "-c:v", "mpeg4", "-y", str(source)],
        check=True, capture_output=True, timeout=30,
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "studio.web.app:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "error"],
        cwd=Path(__file__).resolve().parents[1], env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        for _ in range(80):
            if server.poll() is not None:
                raise AssertionError("Studio server exited before serving the interface")
            try:
                with urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
                    assert response.status == 200
                break
            except OSError:
                time.sleep(.1)
        else:
            raise AssertionError("Studio server did not become ready")
        with sync_api.sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{port}/")
            assert page.locator("#main-nav .nav-item").count() == 5
            assert page.locator("#page-dashboard").count() == 0
            page.wait_for_function("() => document.querySelector('#cpu-value').textContent !== '—'", timeout=15000)
            cpu_before = page.locator("#cpu-value").inner_text()
            assert page.locator("#source-path,#source-kind,#use-source-path,.manual-source").count() == 0
            page.route("**/api/system/select-video", lambda route: route.fulfill(json={"path":str(source), "cancelled":False}))
            page.locator("#add-file").click()
            page.wait_for_function("() => state.selection && state.selection.type === 'file'", timeout=15000)
            page.locator("#import-encoder-cpu").check()
            assert page.locator("#queue-selection").is_enabled()
            assert page.locator("#import-config").evaluate("el=>el.tagName==='SECTION'")
            assert page.locator("#import-mode,[data-import-mode],[data-mode]").count() == 0
            assert page.locator("#import-encoder-cpu").is_visible()
            assert page.locator(".review-season").count() == 1
            assert page.locator(".review-season").evaluate("el=>el.tagName==='SECTION'")
            assert "S01E01.mp4" in page.locator(".review-row").inner_text()
            if os.getenv("ABV_STUDIO_CAPTURE_UI") == "1":
                capture = Path(os.environ["ABV_STUDIO_CAPTURE_DIR"])
                capture.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(capture / "import-hierarchy-preview.png"),full_page=True)
            page.locator("#queue-selection").click()
            page.wait_for_function("() => state.batch.episodes.length === 1 && !state.importBusy", timeout=15000)
            assert page.locator("#page-queue").is_visible()
            assert page.locator("#page-upload").is_hidden()
            assert page.locator("#processing-workspace").is_visible()
            assert page.locator("#cpu-value").inner_text() != "—"
            assert page.locator("#queue-body .episode-row").count() == 1
            assert page.locator("#queue-body .episode-row").bounding_box()["height"] <= 68
            assert page.locator("#queue-body .episode-row .row-actions").evaluate("el => getComputedStyle(el).flexWrap") == "nowrap"
            assert page.evaluate("() => state.batch.episodes[0].state") == "PENDING"
            page.locator(".connection-quick-settings summary").click()
            assert page.locator("#queue-server-url-input").input_value() == "http://127.0.0.1:1/api/v1"
            page.locator("#queue-test-connection").click()
            page.wait_for_function("() => document.querySelector('#queue-connection-result').textContent !== 'جارٍ الاختبار…'", timeout=10000)
            if os.getenv("ABV_STUDIO_CAPTURE_UI") == "1":
                capture = Path(os.environ["ABV_STUDIO_CAPTURE_DIR"])
                capture.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(capture / "queue-1920.png"), full_page=True)
                page.locator('[data-page="import"]').click()
                page.screenshot(path=str(capture / "import-1920.png"), full_page=True)
                page.locator('[data-page="queue"]').click()
            for name in ("history", "library", "settings", "import", "queue"):
                page.locator(f'[data-page="{name}"]').click()
                assert page.locator(f"#page-{name}").is_visible()
                assert page.locator("#cpu-value").is_visible()
            page.locator('[data-page="settings"]').click()
            for tab in ("general", "connection", "processing", "fonts", "advanced"):
                page.locator(f'[data-settings-tab="{tab}"]').click()
                assert page.locator(f'[data-tab-panel="{tab}"]:not(.hidden)').count() > 0
            page.locator('[data-settings-tab="advanced"]').press("ArrowLeft")
            assert page.locator('[data-settings-tab="general"]').get_attribute("aria-selected") == "true"
            if os.getenv("ABV_STUDIO_CAPTURE_UI") == "1":
                page.screenshot(path=str(capture / "settings-1920.png"), full_page=True)
            page.locator('[data-settings-tab="processing"]').click()
            page.locator("#cpu-jobs").fill("2")
            page.locator("#save-settings").click()
            page.wait_for_function("() => state.config.cpu_concurrent_jobs === 2")
            page.reload()
            page.locator('[data-page="settings"]').click()
            page.locator('[data-settings-tab="processing"]').click()
            page.wait_for_function("() => document.querySelector('#cpu-jobs').value === '2'", timeout=10000)
            assert page.locator("#cpu-jobs").input_value() == "2"
            page.locator('[data-page="library"]').click()
            page.locator("#library-search").fill("unmatched")
            assert "لا توجد حزم" in page.locator("#library-list").inner_text()
            page.locator("#library-list-view").click()
            assert page.locator("#library-list").evaluate("el => el.classList.contains('list-view')")
            page.locator('[data-page="history"]').click()
            page.locator("#history-search").fill("unmatched")
            page.set_viewport_size({"width": 1100, "height": 760})
            assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
            if os.getenv("ABV_STUDIO_CAPTURE_UI") == "1":
                page.screenshot(path=str(capture / "history-1100.png"), full_page=True)
                page.evaluate("""() => {
                    const original=state.batch.episodes[0];
                    state.batch.episodes=[
                        {...original,id:'ready-visual',filename:'[SILVER] Spirit_Cage_Incarnation_S1_01_4K_Arabic_Subtitles_Long_Name.mkv',state:'READY',updated_at:'2026-10-16T11:00:00',work_dir:'D:/Converted/Spirit Cage/Season 01/Episode 01'},
                        {...original,id:'failed-visual',filename:'Spirit_Cage_Episode_02_Long_Name.mkv',state:'FAILED',updated_at:'2026-10-16T11:15:00',error:'تعذر قراءة ملف الترجمة',work_dir:'D:/Converted/Spirit Cage/Season 01/Episode 02'}
                    ];
                    state.historySearch='';document.querySelector('#history-search').value='';
                    renderBatch();
                }""")
                assert page.locator("#history-list .history-row").count() == 2
                page.screenshot(path=str(capture / "history-populated-1100.png"), full_page=True)
                page.locator('[data-page="library"]').click()
                page.locator("#library-search").fill("")
                assert page.locator("#library-list .library-item").count() == 1
                page.screenshot(path=str(capture / "library-populated-1100.png"), full_page=True)
                page.evaluate("""() => {
                    const seed=state.batch.episodes[0];
                    state.batch.episodes=Array.from({length:9},(_,i)=>({...seed,id:'upload-visual-'+i,
                      filename:'[SILVER] Spirit Cage S2E'+String(i+1).padStart(2,'0')+' 4K HEVC AAC.mkv',
                      state:i===0?'UPLOADING':'READY_TO_UPLOAD',progress:i===0?42:100,
                      package_bytes:1234567890,uploaded_bytes:518518513,upload_speed:'85.2 MB/s',upload_eta:'00:00:09',
                      upload_elapsed_seconds:6,updated_at:'2026-10-16T10:32:00',
                      work_dir:'D:/Converted/Ling Cage (2019)/Season 02/Episode '+String(i+1).padStart(3,'0'),
                      settings:{...seed.settings,series_name:'Ling Cage (2019)',season_name:'Season 02',season_number:'02'}}));
                    state.server={connected:true,authenticated:true,health:200,readiness:200,scopes:['packages:create','packages:write','packages:publish']};
                    renderServer();renderBatch();showPage('upload');
                }""")
                assert page.locator("#page-upload").is_visible()
                assert page.locator("#processing-workspace").is_hidden()
                assert page.locator(".upload-row").count() == 9
                assert "85.2 MB/s" in page.locator(".upload-row.is-uploading").inner_text()
                assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path=str(capture / "uploads-1100.png"), full_page=True)
                page.locator(".upload-file-details summary").first.click()
                page.evaluate("() => renderResultLists(state.batch.episodes)")
                assert page.locator(".upload-file-details").first.evaluate("el=>el.tagName==='SECTION'")
                page.locator(".upload-file-details summary").first.click()
                page.set_viewport_size({"width":1920,"height":1080})
                assert page.locator(".upload-row:not(.is-uploading)").first.bounding_box()["height"] <= 90
                page.screenshot(path=str(capture / "uploads-1920.png"), full_page=True)
                page.locator("#processing-tab").click()
                assert page.locator("#page-upload").is_hidden()
                page.locator("#processing-tab").press("ArrowLeft")
                assert page.locator("#page-upload").is_visible()

            assert cpu_before.endswith("%")
            assert not errors
            browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
