"""HTTP regressions for a hidden root without breaking private platform flows.

Uses the isolated PostgreSQL/Redis services enforced by tests/conftest.py.
Synthetic media is generated in temporary storage; no real media is processed.
"""
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.database import AsyncSessionLocal, get_db
from app.main import create_application
from app.models.video import Video, VideoRendition, VideoStatus, VideoVisibility
from app.models.share import Share
from test_admin_auth_and_management import admin_fixture


@pytest.mark.parametrize("environment", ["development", "test", "production"])
@pytest.mark.parametrize("security_mode", ["lan", "public"])
@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_root_is_empty_404_without_redirect_or_database(
    monkeypatch, environment, security_mode, scheme, method
):
    monkeypatch.setattr(settings, "ENVIRONMENT", environment)
    monkeypatch.setattr(settings, "SECURITY_MODE", security_mode)
    app = create_application()

    async def forbidden_database_access():
        pytest.fail("The hidden root must not access the database")
        yield

    app.dependency_overrides[get_db] = forbidden_database_access
    async with AsyncClient(transport=ASGITransport(app=app), base_url=f"{scheme}://localhost") as client:
        response = await client.request(method, "/?next=/admin", follow_redirects=False)
    assert response.status_code == 404
    assert response.content == b""
    assert response.headers["content-length"] == "0"
    assert response.headers["cache-control"] == "no-store"
    assert "location" not in response.headers
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("prefix", ["", "/preview"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("path", [
    "/home", "/browse", "/browse?type=series", "/catalog", "/series/test-series",
    "/title/test-title", "/watch", "/watch/", "/unknown-page",
    "/error", "/error/404", "/error/403", "/error/video-unavailable", "/upload",
])
async def test_alternative_public_catalog_pages_stay_closed(prefix, method, path):
    app = create_application()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.request(method, prefix + path, follow_redirects=False)
    assert response.status_code == 404
    assert response.content == b""
    assert response.headers["content-length"] == "0"
    assert response.headers["cache-control"] == "no-store"
    assert "location" not in response.headers
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("path", ["/api/v1/unknown-endpoint", "/static/__missing_hidden_home_asset__.css"])
async def test_missing_api_and_static_paths_keep_json_errors(path):
    async with AsyncClient(transport=ASGITransport(app=create_application()), base_url="http://localhost") as client:
        response = await client.get(path)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.parametrize("prefix", ["/watch/", "/preview/watch/", "/embed/"])
async def test_missing_watch_links_do_not_render_public_navigation(prefix):
    async with AsyncClient(transport=ASGITransport(app=create_application()), base_url="http://localhost") as client:
        response = await client.get(prefix + "missing-hidden-video", follow_redirects=False)
    assert response.status_code == 404
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert "location" not in response.headers


class PageLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.extend(value for key, value in attrs if key == "href" and value)


def assert_no_public_navigation(html):
    links = PageLinks()
    links.feed(html)
    assert not any(href in ("/", "/preview/") or href.startswith(
        ("/admin", "/browse", "/preview/browse")
    ) for href in links.hrefs)


async def test_https_redirect_still_applies_to_admin(monkeypatch):
    monkeypatch.setattr(settings, "SECURITY_MODE", "public")
    async with AsyncClient(transport=ASGITransport(app=create_application()), base_url="http://localhost") as client:
        response = await client.get("/admin/upload", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "https://localhost/admin/upload"


@pytest.mark.parametrize("role", ["owner", "admin", "user"])
async def test_admin_and_upload_keep_real_login_and_role_checks(admin_fixture, role):
    app = create_application()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        assert (await client.get("/admin/login")).status_code == 200
        for path in ("/admin", "/admin/upload"):
            anonymous = await client.get(path, follow_redirects=False)
            assert anonymous.status_code == 302
            assert anonymous.headers["location"] == "/admin/login"
        login = await client.post("/api/v1/auth/login", json={
            "username_or_email": admin_fixture[role].username,
            "password": admin_fixture[f"{role}_pass"],
        })
        assert login.status_code == 200
        for path in ("/admin", "/admin/upload"):
            response = await client.get(path, follow_redirects=False)
            assert response.status_code == (302 if role == "user" else 200)
            if role == "user":
                assert response.headers["location"] == "/admin/login"
            else:
                assert "text/html" in response.headers["content-type"]
        root = await client.get("/", follow_redirects=False)
        assert root.status_code == 404 and root.content == b""


@pytest.fixture(scope="session")
def synthetic_media(tmp_path_factory):
    folder = tmp_path_factory.mktemp("hidden-root-media")
    mp4 = folder / "sample.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=10:d=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4),
    ], check=True, capture_output=True, timeout=30)
    subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(mp4), "-c", "copy", "-hls_time", "1",
        "-hls_segment_filename", str(folder / "segment%03d.ts"), str(folder / "index.m3u8"),
    ], check=True, capture_output=True, timeout=30)
    return folder


async def test_authenticated_upload_accepts_real_media_and_enforces_csrf(admin_fixture, synthetic_media):
    content = (synthetic_media / "sample.mp4").read_bytes()
    app = create_application()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        login = await client.post("/api/v1/auth/login", json={
            "username_or_email": admin_fixture["owner"].username,
            "password": admin_fixture["owner_pass"],
        })
        assert login.status_code == 200
        payload = {"title": "Hidden root upload fixture", "filename": "sample.mp4",
                   "file_size_bytes": len(content), "chunk_size_bytes": 1048576}
        assert (await client.post("/api/v1/uploads/initiate", json=payload)).status_code == 403
        headers = {settings.CSRF_HEADER_NAME: login.json()["csrf_token"]}
        initiated = await client.post("/api/v1/uploads/initiate", json=payload, headers=headers)
        assert initiated.status_code == 201, initiated.text
        upload_id = initiated.json()["upload_id"]
        uploaded = await client.post(f"/api/v1/uploads/{upload_id}/chunk", headers=headers,
                                    data={"chunk_index": 0},
                                    files={"chunk_file": ("sample.mp4", content, "video/mp4")})
        assert uploaded.status_code == 200, uploaded.text
        assert uploaded.json()["bytes_received"] == len(content)
        status = await client.get(f"/api/v1/uploads/{upload_id}/status")
        assert status.status_code == 200, status.text
        assert status.json()["missing_chunks"] == []
        assert status.json()["bytes_received"] == len(content)


@pytest.fixture
async def playable_video(admin_fixture, synthetic_media):
    video_id, generation_id = uuid.uuid4(), uuid.uuid4()
    generation = Path(settings.STORAGE_VIDEOS_PATH) / str(video_id) / "generations" / str(generation_id)
    (generation / "360p").mkdir(parents=True)
    for filename in ("index.m3u8", "segment000.ts", "sample.mp4"):
        shutil.copyfile(synthetic_media / filename, generation / "360p" / filename)
    (generation / "master.m3u8").write_text(
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000,RESOLUTION=160x90\n360p/index.m3u8\n",
        encoding="utf-8",
    )
    video = Video(id=video_id, user_id=admin_fixture["owner"].id, title="Hidden root media fixture",
                  source_filename="sample.mp4", status=VideoStatus.READY,
                  visibility=VideoVisibility.UNLISTED, share_id="hiddenroot" + uuid.uuid4().hex,
                  active_generation_id=generation_id, source_duration_seconds=1)
    async with AsyncSessionLocal() as db:
        db.add(video)
        db.add(VideoRendition(video_id=video_id, quality_label="360p", resolution_width=160,
                              resolution_height=90, is_ready=True))
        await db.commit()
    return video


@pytest.mark.parametrize("visibility", [VideoVisibility.PUBLIC, VideoVisibility.UNLISTED, VideoVisibility.PRIVATE])
async def test_watch_hls_mp4_and_private_access_are_preserved(
    playable_video, admin_fixture, synthetic_media, visibility, monkeypatch
):
    monkeypatch.setattr(settings, "SERVE_MEDIA_VIA_NGINX", False)
    video = playable_video
    async with AsyncSessionLocal() as db:
        stored = await db.get(Video, video.id)
        stored.visibility = visibility
        await db.commit()
    app = create_application()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        watch = await client.get(f"/watch/{video.share_id}")
        if visibility == VideoVisibility.PRIVATE:
            assert watch.status_code == 404
            assert watch.content == b""
            login = await client.post("/api/v1/auth/login", json={
                "username_or_email": admin_fixture["owner"].username,
                "password": admin_fixture["owner_pass"],
            })
            assert login.status_code == 200
            watch = await client.get(f"/watch/{video.share_id}")
        assert watch.status_code == 200
        assert 'id="video-element"' in watch.text
        assert_no_public_navigation(watch.text)
        playback_url = re.search(r'"playbackUrl":\s*"([^"]+)"', watch.text).group(1)
        master = await client.get(playback_url)
        assert master.status_code == 200, master.text
        assert "360p/index.m3u8" in master.text
        base = playback_url.rsplit("/", 1)[0]
        playlist = await client.get(base + "/360p/index.m3u8")
        assert playlist.status_code == 200 and "#EXTINF:" in playlist.text
        segment = await client.get(base + "/360p/segment000.ts")
        assert segment.status_code == 200 and segment.content[0] == 0x47
        mp4 = await client.get(base + "/360p/sample.mp4")
        assert mp4.status_code == 200
        assert mp4.headers["content-type"] in ("video/mp4", "application/octet-stream")
        assert mp4.content == (synthetic_media / "sample.mp4").read_bytes()
        for media_bytes in (segment.content, mp4.content):
            subprocess.run(
                ["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "null", "-"],
                input=media_bytes, check=True, capture_output=True, timeout=30,
            )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as stranger:
            for path in (playback_url, base + "/360p/segment000.ts", base + "/360p/sample.mp4"):
                assert (await stranger.get(path)).status_code in (401, 403)
        async with AsyncSessionLocal() as db:
            stored = await db.get(Video, video.id)
            stored.visibility = VideoVisibility.PRIVATE
            await db.commit()
        if visibility != VideoVisibility.PRIVATE:
            assert (await client.get(playback_url)).status_code == 403


@pytest.mark.parametrize("prefix", ["/watch/", "/preview/watch/", "/embed/"])
@pytest.mark.parametrize("state", ["processing", "trashed", "revoked"])
async def test_unavailable_watch_links_are_empty(playable_video, prefix, state):
    video = playable_video
    async with AsyncSessionLocal() as db:
        stored = await db.get(Video, video.id)
        if state == "processing":
            stored.status = VideoStatus.PROCESSING
        elif state == "trashed":
            stored.trashed_at = datetime.now(timezone.utc)
        else:
            stored.share_revoked = True
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=create_application()), base_url="http://localhost") as client:
        response = await client.get(prefix + video.share_id, follow_redirects=False)
    assert response.status_code == 404
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert "location" not in response.headers


async def test_shares_keep_valid_content_and_hide_invalid_links(playable_video, monkeypatch):
    video = playable_video
    now = datetime.now(timezone.utc)
    shares = {
        label: Share(owner_id=video.user_id, target_type="video", target_id=video.id,
                     token=uuid.uuid4().hex)
        for label in ("valid", "expired", "revoked", "orphan")
    }
    shares["expired"].expires_at = now - timedelta(seconds=1)
    shares["revoked"].revoked_at = now
    shares["orphan"].target_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add_all(shares.values())
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=create_application()), base_url="http://localhost") as client:
        valid = await client.get(f"/preview/share/{shares['valid'].token}")
        assert valid.status_code == 200
        assert video.title in valid.text
        assert_no_public_navigation(valid.text)
        for token in ("missing-hidden-share", *(shares[label].token for label in ("expired", "revoked", "orphan"))):
            response = await client.get(f"/preview/share/{token}", follow_redirects=False)
            assert response.status_code == 404
            assert response.content == b""
            assert response.headers["cache-control"] == "no-store"
            assert "location" not in response.headers

        from app.services.drive_service import DriveService

        async def unavailable_shared_scope(*args, **kwargs):
            raise ValueError("Shared scope became unavailable")

        monkeypatch.setattr(DriveService, "get_shared_scope_contents", unavailable_shared_scope)
        unavailable = await client.get(f"/preview/share/{shares['valid'].token}")
        assert unavailable.status_code == 404
        assert unavailable.content == b""



@pytest.mark.parametrize("path", ["/preview", "/preview/", "/preview/?next=/admin"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_preview_root_stays_empty_without_database(path, method):
    app = create_application()
    async def forbidden_database_access():
        pytest.fail("Hidden preview root must not query the database")
        yield
    app.dependency_overrides[get_db] = forbidden_database_access
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://localhost") as client:
        response = await client.request(method, path, follow_redirects=False)
    assert response.status_code == 404
    assert response.content == b""
    assert response.headers["content-length"] == "0"
    assert response.headers["cache-control"] == "no-store"
    assert "location" not in response.headers


@pytest.mark.parametrize("status_code,expected", [(200,404),(404,404),(403,403),(500,500)])
def test_legacy_error_template_never_exposes_page_or_navigation(status_code, expected):
    from app.api.viewer_routes import render_viewer_template
    response = render_viewer_template("viewer_error.html", status_code=status_code,
                                      error_title="Test", prefix="/preview")
    assert response.status_code == expected
    assert response.body == b""
    assert response.headers["cache-control"] == "no-store"
