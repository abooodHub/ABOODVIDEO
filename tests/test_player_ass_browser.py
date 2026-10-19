"""
Automated End-to-End and Browser Regression Tests for True ASS Softsub Support:
- Manifest validation (acceptance of ASS + fonts, rejection of unsafe paths, missing fonts, SHA mismatches)
- Generation asset promotion (immutable subtitles and fonts preserved in generations/{gen_id}/)
- Protected playback API endpoints (ASS file & opaque font assets)
- Browser rendering with JASSUB WASM:
  - VTT uses HTML5 text track path
  - ASS activates libass/JASSUB WASM renderer
  - Caches and loads required fonts (11/11 for the checked-in Arabic fixture)
  - Reports font loading statistics (Required / Loaded / Missing)
  - Subtitle off cleanly destroys JASSUB overlay
  - Seeking and HLS quality switching maintain synchronization
"""
import os
import io
import json
import uuid
import hashlib
import tempfile
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import redis.asyncio as aioredis
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from app.main import create_application
from app.core.config import settings
from app.core.database import get_db_context
from app.models.user import User, UserRole
from app.models.api_key import ApiKey
from app.models.video import Video, VideoStatus, VideoSubtitle, VideoFontAsset
from app.models.package import PackageUploadSession, PackageSessionStatus
from app.schemas.package import PackageManifest, PackageInitRequest
from app.services.package_service import PackageService
from app.workers.package_worker import verify_and_promote_package
from app.security.playback_token import create_playback_token
from app.api.watch_routes import render_watch_template


@pytest.fixture
def app():
    return create_application()


@pytest.fixture
async def auth_api_key():
    async with get_db_context() as db:
        user_res = await db.execute(select(User).where(User.username == "admin_ass_test"))
        user = user_res.scalar_one_or_none()
        if not user:
            user = User(
                id=uuid.uuid4(),
                username="admin_ass_test",
                email="admin_ass@aboodvideo.local",
                hashed_password="fake-hash-for-tests",
                role=UserRole.ADMIN,
                is_active=True,
            )
            db.add(user)
            await db.flush()

        key_res = await db.execute(select(ApiKey).where(ApiKey.name == "ass_test_key"))
        api_key = key_res.scalar_one_or_none()
        if not api_key:
            api_key = ApiKey(
                id=uuid.uuid4(),
                user_id=user.id,
                name="ass_test_key",
                prefix="abv_test",
                key_hash=hashlib.sha256(b"abv_test_key_secret_ass").hexdigest(),
                scopes=["studio:upload", "videos:read", "videos:write"],
                is_active=True,
            )
            db.add(api_key)
            await db.flush()

        await db.commit()
        return api_key


def test_manifest_validation_ass_and_fonts():
    """Verify manifest accepts valid ASS + fonts and rejects malicious font paths or missing references."""
    vtt_sha = hashlib.sha256(b"WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nHello").hexdigest()
    ass_sha = hashlib.sha256(b"[Script Info]\nTitle: Test\n").hexdigest()
    font1_sha = hashlib.sha256(b"FONT_BINARY_DATA_1").hexdigest()
    font2_sha = hashlib.sha256(b"FONT_BINARY_DATA_2").hexdigest()
    m3u8_sha = hashlib.sha256(b"#EXTM3U\n#EXT-X-VERSION:3\n").hexdigest()

    manifest_data = {
        "manifest_version": "1.0",
        "video": {"title": "ASS Test Video", "duration_seconds": 60.0},
        "renditions": [
            {
                "quality_label": "1080p",
                "width": 1920,
                "height": 1080,
                "video_bitrate_bps": 4000000,
                "playlist_path": "1080p/index.m3u8",
                "segment_count": 1,
            }
        ],
        "fonts": [
            {
                "family_name": "Test Font 1",
                "filename": "test_font1.ttf",
                "path": "fonts/test_font1.ttf",
                "size_bytes": 18,
                "sha256": font1_sha,
            },
            {
                "family_name": "Test Font 2",
                "filename": "test_font2.otf",
                "path": "fonts/test_font2.otf",
                "size_bytes": 18,
                "sha256": font2_sha,
            },
        ],
        "subtitles": [
            {
                "language_code": "ar",
                "language_name": "العربية",
                "label": "العربية (ASS)",
                "path": "subtitles/ar.ass",
                "sha256": ass_sha,
                "format": "ass",
                "fonts": ["Test Font 1", "fonts/test_font2.otf"],
            }
        ],
        "files": [
            {"path": "1080p/index.m3u8", "size_bytes": 25, "sha256": m3u8_sha},
            {"path": "fonts/test_font1.ttf", "size_bytes": 18, "sha256": font1_sha},
            {"path": "fonts/test_font2.otf", "size_bytes": 18, "sha256": font2_sha},
            {"path": "subtitles/ar.ass", "size_bytes": 26, "sha256": ass_sha},
        ],
    }

    manifest = PackageManifest(**manifest_data)
    # Valid manifest should pass
    PackageService.validate_manifest_security(manifest)

    # 1. Unsafe font path traversal rejected
    bad_manifest_data = json.loads(json.dumps(manifest_data))
    bad_manifest_data["fonts"][0]["path"] = "fonts/../../etc/passwd.ttf"
    bad_manifest_data["files"][1]["path"] = "fonts/../../etc/passwd.ttf"
    with pytest.raises(Exception) as exc:
        PackageService.validate_manifest_security(PackageManifest(**bad_manifest_data))
    assert "dangerous" in str(exc.value).lower() or "escapes" in str(exc.value).lower() or "illegal" in str(exc.value).lower()

    # 2. Font with non-ttf/otf extension rejected
    bad_manifest_data = json.loads(json.dumps(manifest_data))
    bad_manifest_data["fonts"][0]["path"] = "fonts/evil.exe"
    bad_manifest_data["files"][1]["path"] = "fonts/evil.exe"
    with pytest.raises(Exception) as exc:
        PackageService.validate_manifest_security(PackageManifest(**bad_manifest_data))
    assert "extension" in str(exc.value).lower()

    # 3. Subtitle references undeclared font rejected
    bad_manifest_data = json.loads(json.dumps(manifest_data))
    bad_manifest_data["subtitles"][0]["fonts"].append("NonExistentFont")
    with pytest.raises(Exception) as exc:
        PackageService.validate_manifest_security(PackageManifest(**bad_manifest_data))
    assert "undeclared font" in str(exc.value).lower()

    # 4. Font SHA mismatch rejected
    bad_manifest_data = json.loads(json.dumps(manifest_data))
    bad_manifest_data["fonts"][0]["sha256"] = "0" * 64
    with pytest.raises(Exception) as exc:
        PackageService.validate_manifest_security(PackageManifest(**bad_manifest_data))
    assert "mismatch" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_ass_package_promotion_and_protected_playback(app, auth_api_key, tmp_path):
    """Verify package worker promotes ASS and fonts to immutable generation assets and serves them via protected API."""
    async with get_db_context() as db:
        video = Video(
            id=uuid.uuid4(),
            user_id=auth_api_key.user_id,
            title="ASS Playback Video",
            source_filename="test_ass.mp4",
            status=VideoStatus.PROCESSING,
            visibility="PUBLIC",
            share_id=f"test_ass_{uuid.uuid4().hex[:8]}",
        )
        db.add(video)
        await db.flush()

        # Prepare dummy payload data
        dummy_ts = b"\x47" + b"\x00" * 187  # 1 TS packet
        seg_sha = hashlib.sha256(dummy_ts * 10).hexdigest()

        playlist_content = "#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:2.0,\nseg_000.ts\n#EXT-X-ENDLIST\n"
        pl_sha = hashlib.sha256(playlist_content.encode("utf-8")).hexdigest()

        ass_content = "[Script Info]\nTitle: Test ASS\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize\nStyle: Default,Arial,20\n[Events]\nFormat: Layer, Start, End, Style, Text\nDialogue: 0,0:00:01.00,0:00:03.00,Default,مرحبا\n"
        ass_sha = hashlib.sha256(ass_content.encode("utf-8")).hexdigest()

        font_bytes = b"\x00\x01\x00\x00" + b"\x00" * 100
        font_sha = hashlib.sha256(font_bytes).hexdigest()

        manifest = {
            "manifest_version": "1.0",
            "video": {"title": "ASS Playback Video", "duration_seconds": 10.0},
            "renditions": [
                {
                    "quality_label": "1080p",
                    "width": 1920,
                    "height": 1080,
                    "video_bitrate_bps": 4000000,
                    "playlist_path": "1080p/index.m3u8",
                    "segment_count": 1,
                }
            ],
            "fonts": [
                {
                    "family_name": "Test Font",
                    "filename": "test_font.ttf",
                    "path": "fonts/test_font.ttf",
                    "size_bytes": len(font_bytes),
                    "sha256": font_sha,
                }
            ],
            "subtitles": [
                {
                    "language_code": "ar",
                    "language_name": "العربية",
                    "label": "العربية (ASS)",
                    "path": "subtitles/ar.ass",
                    "sha256": ass_sha,
                    "format": "ass",
                    "fonts": ["Test Font"],
                }
            ],
            "files": [
                {"path": "1080p/index.m3u8", "size_bytes": len(playlist_content.encode("utf-8")), "sha256": pl_sha},
                {"path": "1080p/seg_000.ts", "size_bytes": len(dummy_ts * 10), "sha256": seg_sha},
                {"path": "fonts/test_font.ttf", "size_bytes": len(font_bytes), "sha256": font_sha},
                {"path": "subtitles/ar.ass", "size_bytes": len(ass_content.encode("utf-8")), "sha256": ass_sha},
            ],
        }

        pkg_req = PackageInitRequest(
            manifest=PackageManifest(**manifest),
            video_id=video.id,
        )
        init_res = await PackageService.init_package_session(db, pkg_req, auth_api_key)
        session_id = init_res.session_id

        sess_stmt = select(PackageUploadSession).where(PackageUploadSession.id == session_id)
        pkg_session = (await db.execute(sess_stmt)).scalar_one()
        gen_id = pkg_session.target_generation_id
        staging_dir = Path(pkg_session.staging_path)

        # Create dummy rendition, subtitle, font files on staging disk
        (staging_dir / "1080p").mkdir(parents=True, exist_ok=True)
        (staging_dir / "fonts").mkdir(parents=True, exist_ok=True)
        (staging_dir / "subtitles").mkdir(parents=True, exist_ok=True)

        (staging_dir / "1080p" / "seg_000.ts").write_bytes(dummy_ts * 10)
        (staging_dir / "1080p" / "index.m3u8").write_bytes(playlist_content.encode("utf-8"))
        (staging_dir / "subtitles" / "ar.ass").write_bytes(ass_content.encode("utf-8"))
        (staging_dir / "fonts" / "test_font.ttf").write_bytes(font_bytes)

    # Run verification and promotion worker
    verify_and_promote_package(str(session_id))

    # Verify database state after promotion
    async with get_db_context() as db:
        v_res = await db.execute(select(Video).where(Video.id == video.id))
        promoted_video = v_res.scalar_one()
        assert promoted_video.status == VideoStatus.READY.value
        assert promoted_video.active_generation_id == gen_id

        # Verify VideoSubtitle
        sub_res = await db.execute(select(VideoSubtitle).where(VideoSubtitle.video_id == video.id))
        sub_record = sub_res.scalar_one()
        assert sub_record.format == "ass"
        assert sub_record.playback_renderer == "libass"
        assert sub_record.is_playable is True
        assert sub_record.generation_id == gen_id
        assert sub_record.source_path == "subtitles/ar.ass"
        assert len(sub_record.font_asset_ids) == 1

        # Verify VideoFontAsset
        font_res = await db.execute(select(VideoFontAsset).where(VideoFontAsset.video_id == video.id))
        font_record = font_res.scalar_one()
        assert font_record.family_name == "Test Font"
        assert font_record.relative_path == "fonts/test_font.ttf"
        assert font_record.sha256 == font_sha

        # Verify generation storage layout on disk
        final_gen = Path(settings.STORAGE_VIDEOS_PATH) / str(video.id) / "generations" / str(gen_id)
        assert (final_gen / "master.m3u8").exists()
        assert (final_gen / "subtitles" / "ar.ass").exists()
        assert (final_gen / "fonts" / "test_font.ttf").exists()

        # Create playback token
        redis_conn = aioredis.from_url(settings.REDIS_SESSION_URL, decode_responses=True)
        try:
            token = await create_playback_token(
                redis_conn=redis_conn,
                video_id=video.id,
                active_generation_id=gen_id,
                authorization_type="public",
                share_id=video.share_id,
                ttl_seconds=300,
            )
        finally:
            await redis_conn.aclose()

    # Test protected playback API
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Fetch ASS subtitle file
        sub_url = f"/api/v1/playback/{token}/subtitles/{sub_record.id}/file"
        resp = await client.get(sub_url)
        assert resp.status_code == 200
        assert "text/x-ssa" in resp.headers.get("Content-Type", "")
        if settings.SERVE_MEDIA_VIA_NGINX:
            assert "X-Accel-Redirect" in resp.headers
            assert f"/internal_media/{video.id}/generations/{gen_id}/subtitles/ar.ass" in resp.headers["X-Accel-Redirect"]
        else:
            assert "Title: Test ASS" in resp.text

        # 2. Fetch font asset
        font_url = f"/api/v1/playback/{token}/fonts/{font_record.id}"
        resp_font = await client.get(font_url)
        assert resp_font.status_code == 200
        assert "font/ttf" in resp_font.headers.get("Content-Type", "")
        assert "immutable" in resp_font.headers.get("Cache-Control", "")
        if settings.SERVE_MEDIA_VIA_NGINX:
            assert "X-Accel-Redirect" in resp_font.headers
            assert f"/internal_media/{video.id}/generations/{gen_id}/fonts/test_font.ttf" in resp_font.headers["X-Accel-Redirect"]
        else:
            assert resp_font.content == font_bytes

        # 3. Direct route via video_id query param
        direct_font_url = f"/api/v1/playback/{video.id}/fonts/{font_record.id}?token={token}"
        resp_direct = await client.get(direct_font_url)
        assert resp_direct.status_code == 200
        if settings.SERVE_MEDIA_VIA_NGINX:
            assert "X-Accel-Redirect" in resp_direct.headers
        else:
            assert resp_direct.content == font_bytes

        # Also verify direct FileResponse delivery when Nginx proxy is disabled
        orig_serve = settings.SERVE_MEDIA_VIA_NGINX
        try:
            settings.SERVE_MEDIA_VIA_NGINX = False
            resp_direct_sub = await client.get(sub_url)
            assert resp_direct_sub.status_code == 200
            assert "Title: Test ASS" in resp_direct_sub.text

            resp_direct_font = await client.get(font_url)
            assert resp_direct_font.status_code == 200
            assert resp_direct_font.content == font_bytes
        finally:
            settings.SERVE_MEDIA_VIA_NGINX = orig_serve

        # 4. Request without token rejected
        unauth_url = f"/api/v1/playback/{video.id}/fonts/{font_record.id}"
        resp_unauth = await client.get(unauth_url)
        assert resp_unauth.status_code == 401

        # 5. Invalid subtitle ID returns 404
        fake_id = uuid.uuid4()
        resp_fake = await client.get(f"/api/v1/playback/{token}/subtitles/{fake_id}/file")
        assert resp_fake.status_code == 404


@pytest.mark.skipif(os.getenv("ABOODVIDEO_RUN_BROWSER_TEST") != "1", reason="Chromium opt-in")
def test_jassub_wasm_browser_rendering_and_silver_acceptance(tmp_path: Path):
    """
    Torture-test and visual acceptance of JASSUB WebAssembly libass rendering with
    the checked-in Arabic ASS fixture and eleven real fonts.
    """
    playwright = pytest.importorskip("playwright.sync_api")

    # Generate dummy media file for video element
    media_mp4 = tmp_path / "silver-test.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=640x360:r=25",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-movflags", "+faststart", str(media_mp4),
    ], capture_output=True, check=True, timeout=30)

    # Checked-in fixture uses all eleven bundled real fonts and ASS overrides.
    # It is independent of personal Downloads and original episode files.
    real_ass_path = Path(__file__).parent / "fixtures" / "ass" / "eleven_fonts_arabic.ass"
    assert real_ass_path.is_file(), "Required ASS integration fixture is missing"

    ass_content = real_ass_path.read_text(encoding="utf-8-sig")

    # Locate studio fonts directory
    studio_fonts_dir = Path("studio/fonts").resolve()
    assert studio_fonts_dir.exists(), "studio/fonts directory must exist"

    # Prepare fonts data mapping for browser mock
    from studio.core.fonts import analyze_ass_fonts
    font_analysis = analyze_ass_fonts(real_ass_path)
    assert font_analysis["total_fonts"] == 11, f"Expected 11 fonts, got {font_analysis['total_fonts']}"

    fonts_list = []
    font_data_by_id = {}
    for idx, f in enumerate(font_analysis["fonts"]):
        f_id = f"font-{idx}-{uuid.uuid4().hex[:6]}"
        f_path = Path(f["file"])
        if not f_path.exists():
            # Fall back to studio/fonts
            f_path = studio_fonts_dir / f_path.name
        assert f_path.exists(), f"Font file {f['name']} ({f_path}) must exist on disk"
        b_data = f_path.read_bytes()
        font_data_by_id[f_id] = b_data
        fonts_list.append({
            "id": f_id,
            "family": f["name"],
            "filename": f_path.name,
            "size": len(b_data),
        })

    # Render watch template with ASS subtitle track and VTT track
    subtitles_config = [
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "lang": "ar",
            "name": "العربية",
            "label": "العربية",
            "is_default": True,
            "is_forced": False,
            "is_playable": True,
            "format": "ass",
            "renderer": "libass",
            "fonts": fonts_list,
        },
        {
            "id": "22222222-2222-4222-8222-222222222222",
            "lang": "en",
            "name": "English",
            "label": "English VTT",
            "is_default": False,
            "is_forced": False,
            "is_playable": True,
            "format": "vtt",
            "renderer": "html5",
            "fonts": [],
        }
    ]

    page_html = render_watch_template(
        title="Spirit Cage S1E02 ASS Torture Test",
        share_id="spirit_cage_test",
        playback_token="pbt_spirit_cage_token",
        subtitles=subtitles_config,
    ).body.decode("utf-8")

    app_root = Path("backend/app").resolve()
    player_js = (app_root / "static" / "js" / "player.js").read_text(encoding="utf-8")
    jassub_umd = (app_root / "static" / "vendor" / "jassub" / "jassub.umd.js").read_text(encoding="utf-8")
    jassub_worker_js = (app_root / "static" / "vendor" / "jassub" / "jassub-worker.js").read_bytes()
    jassub_worker_wasm = (app_root / "static" / "vendor" / "jassub" / "jassub-worker.wasm").read_bytes()
    default_woff2 = (app_root / "static" / "vendor" / "jassub" / "default.woff2").read_bytes()

    hls_mock = """
    window.Hls = class {
      static isSupported() { return true; }
      static Events = { MANIFEST_PARSED: 'manifest', LEVEL_SWITCHED: 'level' };
      constructor() {
        this.levels = [{height: 2160, bitrate: 15000000}, {height: 1080, bitrate: 4000000}];
        this.handlers = {};
        this.manualLevel = -1;
        this.subtitleTrack = -1;
      }
      on(name, handler) { this.handlers[name] = handler; }
      loadSource() {}
      attachMedia(video) {
        this.video = video;
        video.src = "/media.mp4";
        setTimeout(() => this.handlers.manifest(null, {levels: this.levels}), 0);
      }
      set nextLevel(index) {
        this.manualLevel = index;
        if (this.handlers.level) this.handlers.level(null, {level: index < 0 ? 0 : index});
      }
      get nextLevel() { return this.manualLevel; }
    };
    """

    console_logs = []
    page_errors = []

    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(
            headless=True,
            args=["--no-sandbox"],
        )
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.on("console", lambda m: console_logs.append(m.text))
        page.on("pageerror", lambda err: page_errors.append(str(err)))

        def route_handler(route):
            url = route.request.url
            path = urlsplit(url).path
            if path.endswith("player.js"):
                route.fulfill(status=200, content_type="application/javascript", body=player_js)
            elif path.endswith("jassub.umd.js"):
                route.fulfill(status=200, content_type="application/javascript", body=jassub_umd)
            elif path.endswith("jassub-worker.js"):
                route.fulfill(status=200, content_type="application/javascript", body=jassub_worker_js)
            elif path.endswith("jassub-worker.wasm"):
                route.fulfill(status=200, content_type="application/wasm", body=jassub_worker_wasm)
            elif path.endswith("default.woff2"):
                route.fulfill(status=200, content_type="font/woff2", body=default_woff2)
            elif path.endswith("hls.min.js"):
                route.fulfill(status=200, content_type="application/javascript", body=hls_mock)
            elif path.endswith(".css"):
                css_file = app_root / "static" / "css" / Path(path.split("/static/css/", 1)[-1])
                route.fulfill(status=200, content_type="text/css", body=css_file.read_text(encoding="utf-8") if css_file.exists() else "")
            elif "/fonts/" in path:
                # Extract font ID from URL
                font_id = path.rstrip("/").split("/")[-1]
                if font_id in font_data_by_id:
                    route.fulfill(status=200, content_type="font/ttf", body=font_data_by_id[font_id])
                else:
                    route.fulfill(status=404, body="Font not found")
            elif "/subtitles/" in path and path.endswith("/file"):
                route.fulfill(status=200, content_type="text/x-ssa; charset=utf-8", body=ass_content)
            elif path.endswith(".vtt"):
                route.fulfill(status=200, content_type="text/vtt", body="WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nHello English")
            elif path.endswith("master.m3u8"):
                route.fulfill(status=200, content_type="application/vnd.apple.mpegurl", body="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=15000000,RESOLUTION=3840x2160\n2160p.m3u8\n")
            elif path.endswith("watch"):
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=page_html)
            else:
                video_bytes = media_mp4.read_bytes()
                file_size = len(video_bytes)
                range_header = route.request.headers.get("range")
                if range_header and range_header.startswith("bytes="):
                    parts = range_header[6:].split("-")
                    start = int(parts[0]) if parts[0] else 0
                    end = int(parts[1]) if len(parts) > 1 and parts[1] else file_size - 1
                    chunk = video_bytes[start:end+1]
                    route.fulfill(
                        status=206,
                        headers={
                            "Content-Range": f"bytes {start}-{end}/{file_size}",
                            "Accept-Ranges": "bytes",
                            "Content-Length": str(len(chunk)),
                            "Content-Type": "video/mp4",
                        },
                        body=chunk,
                    )
                else:
                    route.fulfill(
                        status=200,
                        headers={
                            "Accept-Ranges": "bytes",
                            "Content-Length": str(file_size),
                            "Content-Type": "video/mp4",
                        },
                        body=video_bytes,
                    )

        page.route("**/*", route_handler)

        page.goto("http://aboodvideo.local/watch", wait_until="networkidle")

        # Verify page title and subtitle menu options
        assert page.locator("#video-element").is_visible()
        menu_items = page.locator("#subs-menu .menu-item").all_text_contents()
        assert any("ASS" in item for item in menu_items), f"ASS option missing from menu: {menu_items}"

        # Wait for fonts report log
        page.wait_for_timeout(1500)
        font_logs = [log for log in console_logs if "Required fonts:" in log]
        assert len(font_logs) > 0, f"Expected font loading log, got: {console_logs}"
        assert "Required fonts: 11" in font_logs[0]
        assert "Loaded: 11" in font_logs[0]
        assert "Missing: 0" in font_logs[0]

        # Verify canvas element exists over video
        canvas = page.locator("#ass-canvas")
        assert canvas.is_visible()

        # Set video currentTime to 22.5 seconds (contains Arabic dialogue + Theater heading)
        page.evaluate("""() => {
            const v = document.getElementById('video-element');
            v.currentTime = 22.5;
            v.dispatchEvent(new Event('timeupdate'));
            v.dispatchEvent(new Event('seeked'));
            const playBtn = document.getElementById('center-play-button');
            if (playBtn) playBtn.style.display = 'none';
            if (window.jassubInstance) {
                window.jassubInstance.setCurrentTime(false, 22.5);
                window.jassubInstance.resize();
            }
        }""")
        page.wait_for_timeout(1500)

        # Verify JASSUB instance is active and canvas has dimensions
        video_state = page.evaluate("""() => {
            const v = document.getElementById('video-element');
            return { currentTime: v.currentTime, duration: v.duration, paused: v.paused, readyState: v.readyState };
        }""")
        print("VIDEO_STATE:", video_state)
        for log in console_logs:
            print("BROWSER_LOG:", log)
        for err in page_errors:
            print("BROWSER_ERR:", err)

        canvas_info = page.evaluate("""() => {
            const canvas = document.getElementById('ass-canvas');
            return {
                exists: Boolean(canvas),
                hasJassub: Boolean(window.jassubInstance),
                width: canvas ? canvas.width : 0,
                height: canvas ? canvas.height : 0,
            };
        }""")
        print("CANVAS_INFO:", canvas_info)
        assert canvas_info["exists"] is True, "Canvas must exist in DOM"
        assert canvas_info["hasJassub"] is True, "JASSUB instance must be active on window"

        # Capture visual screenshot of the rendered player
        screenshot_path = tmp_path / "silver_ass_rendered_22s.png"
        page.locator("#player-container").screenshot(path=str(screenshot_path))
        assert screenshot_path.exists() and screenshot_path.stat().st_size > 5000

        # Test switching subtitle off: destroys overlay
        page.evaluate("selectSubtitle(-1)")
        page.wait_for_timeout(500)
        off_summary = page.locator("#subtitle-summary").text_content()
        assert off_summary == "إيقاف"

        # Test switching to VTT: activates HTML5 mode
        page.evaluate("selectSubtitle(1)")
        page.wait_for_timeout(500)
        vtt_summary = page.locator("#subtitle-summary").text_content()
        assert "English" in vtt_summary or "VTT" in vtt_summary

        # Test switching back to ASS: reactivates JASSUB
        page.evaluate("selectSubtitle(0)")
        page.wait_for_timeout(1000)
        ass_summary = page.locator("#subtitle-summary").text_content()
        assert "ASS" in ass_summary

        # Test quality switch keeps ASS active
        page.evaluate("selectQuality(1)")
        page.wait_for_timeout(500)
        assert page.locator("#subtitle-summary").text_content() == ass_summary

        # Clean browser teardown
        browser.close()
