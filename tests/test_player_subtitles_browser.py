"""Opt-in Chromium playback of a real MP4 with a selectable soft WebVTT cue."""
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.api.watch_routes import render_watch_template


@pytest.mark.skipif(os.getenv("ABOODVIDEO_RUN_BROWSER_TEST") != "1", reason="Chromium opt-in")
def test_playable_subtitle_cue_appears_and_archive_track_is_disabled(tmp_path: Path):
    playwright = pytest.importorskip("playwright.sync_api")
    media = tmp_path / "subtitle-regression.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=320x180:r=25",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(media),
    ], capture_output=True, check=True, timeout=30)
    page_html = render_watch_template(
        title="Subtitle regression", share_id="test", playback_token="test",
        subtitles=[
            {"id": "11111111-1111-4111-8111-111111111111", "lang": "ar", "name": "العربية", "label": "قابلة للتشغيل", "is_default": True, "is_forced": False, "is_playable": True, "format": "vtt"},
            {"id": "22222222-2222-4222-8222-222222222222", "lang": "en", "name": "English", "label": "Archive", "is_default": False, "is_forced": False, "is_playable": False, "format": "ass"},
        ],
    ).body.decode("utf-8")
    app_root = Path(__file__).resolve().parent.parent / "backend" / "app"
    player_js = (app_root / "static" / "js" / "player.js").read_text(encoding="utf-8")
    errors = []

    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route_request(route):
            path = urlsplit(route.request.url).path
            if path.endswith("player.js"):
                route.fulfill(status=200, content_type="application/javascript", body=player_js)
            elif path.endswith("hls.min.js"):
                route.fulfill(status=200, content_type="application/javascript", body="window.Hls={isSupported:()=>false};")
            elif path.endswith(".css"):
                stylesheet = app_root / "static" / "css" / Path(path.split("/static/css/", 1)[-1])
                route.fulfill(status=200, content_type="text/css", body=stylesheet.read_text(encoding="utf-8"))
            elif path.endswith(".vtt"):
                route.fulfill(status=200, content_type="text/vtt", body="WEBVTT\n\n00:00:00.000 --> 00:00:02.500\nمرحبا بالعالم\n")
            elif path.endswith("sample.mp4"):
                route.fulfill(status=200, content_type="video/mp4", body=media.read_bytes())
            elif path.endswith("/admin/watch"):
                route.fulfill(status=200, content_type="text/html", body=page_html)
            else:
                route.fulfill(status=200, content_type="application/json", body="{}")

        page.route("**/*", route_request)
        page.goto("http://player-upgrade.test/admin/watch", wait_until="networkidle")
        assert page.locator("#sub-opt-1").is_disabled()
        assert page.locator("#sub-opt-0").get_attribute("class").find("active") >= 0
        page.evaluate("() => { document.querySelector('#video-element').src='/sample.mp4'; }")
        page.locator("#big-play-btn").click()
        page.wait_for_function("() => { const video=document.querySelector('#video-element'); const track=video.textTracks[0]; return video.currentTime > 0.2 && track && track.mode === 'showing' && track.activeCues && track.activeCues.length > 0; }")
        assert page.evaluate("() => document.querySelector('#video-element').textTracks[0].activeCues[0].text") == "مرحبا بالعالم"
        page.locator("#player-container").hover()
        page.locator("#btn-subs").click()
        page.locator("#sub-opt-off").click()
        assert page.evaluate("() => document.querySelector('#video-element').textTracks[0].mode") == "disabled"
        assert not errors
        browser.close()
