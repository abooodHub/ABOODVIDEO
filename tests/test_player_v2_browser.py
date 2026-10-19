"""Opt-in Chromium regression for Player V2 controls and real media timeline."""
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.api.watch_routes import render_watch_template


@pytest.mark.skipif(os.getenv("ABOODVIDEO_RUN_BROWSER_TEST") != "1", reason="Chromium opt-in")
def test_player_v2_quality_caption_timeline_and_settings(tmp_path: Path):
    playwright = pytest.importorskip("playwright.sync_api")
    media = tmp_path / "player-v2.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=320x180:r=25",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-movflags", "+faststart", str(media),
    ], capture_output=True, check=True, timeout=30)
    page_html = render_watch_template(
        title="Player V2 test", share_id="test", playback_token="test",
        subtitles=[{"id": "11111111-1111-4111-8111-111111111111", "lang": "ar", "name": "العربية", "label": "Arabic", "is_default": True, "is_forced": False, "is_playable": True, "format": "vtt"}],
    ).body.decode("utf-8")
    app_root = Path(__file__).resolve().parent.parent / "backend" / "app"
    player_js = (app_root / "static" / "js" / "player.js").read_text(encoding="utf-8")
    hls_mock = """
    window.Hls = class {
      static isSupported() { return true; }
      static Events = { MANIFEST_PARSED: 'manifest', LEVEL_SWITCHED: 'level' };
      constructor() { this.levels = [
        {height: 360, bitrate: 700000}, {height: 1080, bitrate: 4000000},
        {height: 480, bitrate: 1100000}, {height: 2160, bitrate: 13000000},
        {height: 1440, bitrate: 7000000}, {height: 720, bitrate: 2500000},
        {height: 1080, bitrate: 3000000}];
        this.handlers = {}; this.manualLevel = -1; this.subtitleTrack = -1; }
      on(name, handler) { this.handlers[name] = handler; }
      loadSource() {}
      attachMedia() { setTimeout(() => this.handlers.manifest(null, {levels: this.levels}), 0); }
      set nextLevel(index) { this.manualLevel = index; this.handlers.level(null, {level: index < 0 ? 1 : index}); }
      get nextLevel() { return this.manualLevel; }
    };
    """
    errors = []
    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)

        def route_request(route):
            path = urlsplit(route.request.url).path
            if path.endswith("player.js"):
                route.fulfill(status=200, content_type="application/javascript", body=player_js)
            elif path.endswith("hls.min.js"):
                route.fulfill(status=200, content_type="application/javascript", body=hls_mock)
            elif path.endswith(".css"):
                stylesheet = app_root / "static" / "css" / Path(path.split("/static/css/", 1)[-1])
                route.fulfill(status=200, content_type="text/css", body=stylesheet.read_text(encoding="utf-8"))
            elif path.endswith(".vtt"):
                route.fulfill(status=200, content_type="text/vtt", body="WEBVTT\n\n00:00:00.000 --> 00:00:04.000\nمرحبا\n")
            elif path.endswith("sample.mp4"):
                route.fulfill(status=200, content_type="video/mp4", body=media.read_bytes())
            elif path.endswith("/watch"):
                route.fulfill(status=200, content_type="text/html", body=page_html,
                              headers={"Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self'; connect-src 'self'"})
            else:
                route.fulfill(status=200, content_type="application/json", body="{}")

        page.route("**/*", route_request)
        page.goto("http://player-v2.test/watch", wait_until="networkidle")
        page.wait_for_selector("#qual-opt-3", state="attached")
        page.evaluate("() => { document.querySelector('#video-element').src='/sample.mp4'; }")
        page.locator("#big-play-btn").click()
        page.wait_for_function("() => document.querySelector('#video-element').currentTime > 0.2")
        before = page.evaluate("() => document.querySelector('#video-element').currentTime")
        page.locator("#btn-settings").click()
        page.locator('[data-open-screen="quality"]').first.click()
        assert page.locator("#quality-menu .menu-item").all_text_contents() == ["تلقائي (Auto)✓", "4K · 2160p", "1440p", "1080p", "720p", "480p", "360p"]
        page.locator("#qual-opt-1").click()
        assert page.evaluate("() => window.hls.manualLevel") == 1
        assert page.locator("#qual-opt-1").get_attribute("aria-checked") == "true"
        assert page.evaluate("() => document.querySelector('#video-element').currentTime") >= before
        page.locator("#qual-opt-auto").click()
        assert page.evaluate("() => window.hls.manualLevel") == -1
        assert page.locator("#qual-opt-auto").get_attribute("aria-checked") == "true"
        page.evaluate("() => { window.hls.levels=[{height:720, bitrate:2500000},{height:1080, bitrate:4000000}]; window.hls.handlers.manifest(null, {levels:window.hls.levels}); }")
        assert page.locator("#quality-menu .menu-item").all_text_contents() == ["تلقائي (Auto) · الآن 1080p✓", "1080p", "720p"]
        page.locator('[data-open-screen="root"]').first.click()
        page.locator('[data-open-screen="speed"]').first.click()
        page.locator('[data-speed="1.75"]').click()
        assert page.evaluate("() => document.querySelector('#video-element').playbackRate") == 1.75
        page.locator("#btn-subs").click()
        assert page.locator("#sub-opt-0").get_attribute("aria-checked") == "true"
        page.locator('[data-open-screen="caption-style"]').last.click()
        page.locator("#caption-size").select_option("large")
        assert page.evaluate("() => JSON.parse(localStorage.getItem('aboodvideo-caption-prefs')).size") == "large"
        assert page.evaluate("() => document.querySelector('#player-container').classList.contains('caption-size-large')")
        page.locator("#btn-settings").click()
        page.locator("#timeline").click(position={"x": 60, "y": 5})
        page.wait_for_function("() => document.querySelector('#video-element').buffered.length > 0")
        assert page.evaluate("() => { const c=document.querySelector('#buffered-bar'); return [...c.getContext('2d').getImageData(0,0,c.width,c.height).data].some(v => v === 169); }")
        page.wait_for_function("() => Number(document.querySelector('#timeline').getAttribute('aria-valuenow')) > 0")
        assert page.evaluate("""() => {
          const video=document.querySelector('#video-element');
          Object.defineProperty(video, 'buffered', {configurable:true, get: () => ({length:2, start:i=>i===0?0:3, end:i=>i===0?1:4})});
          video.dispatchEvent(new Event('progress'));
          const canvas=document.querySelector('#buffered-bar');
          const ctx=canvas.getContext('2d');
          const sample=f=>ctx.getImageData(Math.floor(f*canvas.width), 2, 1, 1).data[3];
          return sample(.1)>0 && sample(.4)===0 && sample(.7)>0;
        }""")
        page.locator("#timeline").hover(position={"x": 120, "y": 5})
        assert page.locator("#timeline-tooltip-canvas").is_visible()
        page.locator("#btn-fullscreen").click()
        page.wait_for_function("() => document.fullscreenElement !== null")
        page.evaluate("() => document.exitFullscreen()")
        assert page.evaluate("() => { const video=document.querySelector('#video-element'); video.dispatchEvent(new Event('waiting')); return document.querySelector('#buffering-spinner').classList.contains('is-visible'); }")
        assert not page.evaluate("() => { document.querySelector('#video-element').dispatchEvent(new Event('canplay')); return document.querySelector('#buffering-spinner').classList.contains('is-visible'); }")
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator("#btn-settings").click()
        assert page.locator("#player-menu-panel").is_visible()
        assert page.locator("#volume-slider").is_hidden()
        assert not errors
        browser.close()
