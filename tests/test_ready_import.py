"""Canonical ready-host source: real synthetic media, authorization and no conversion."""
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import uuid
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, func

from app.core.config import settings
from app.models.video import Video, TranscodeJob
from app.services.direct_import import DirectImportError, extract_and_verify


@pytest.fixture
def ready_package(tmp_path, request):
    kind = request.param
    source = tmp_path / "ready.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=24",
                    "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
                   check=True, capture_output=True, timeout=30)
    if kind == "mp4":
        files = {source.name: source.read_bytes()}
        entrypoint = source.name
    else:
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-c", "copy", "-f", "hls",
                        "-hls_time", "1", "-hls_playlist_type", "vod", str(tmp_path / "index.m3u8")],
                       check=True, capture_output=True, timeout=30)
        files = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.suffix in {".ts", ".m3u8"}}
        files["master.m3u8"] = b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=200000,RESOLUTION=320x180,CODECS="avc1.42e01e"\nindex.m3u8\n'
        entrypoint = "master.m3u8"
    manifest = {"format": "abv-direct-import-v1", "media_type": kind,
                "title": "Synthetic " + uuid.uuid4().hex, "entrypoint": entrypoint,
                "files": [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                          for name, data in files.items()]}
    archive = tmp_path / "ready.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("manifest.json", json.dumps(manifest))
        for name, data in files.items():
            output.writestr(name, data)
    return kind, archive, entrypoint, files


@pytest.mark.parametrize("ready_package", ["mp4", "hls"], indirect=True)
async def test_ready_import_private_watch_playback_and_conversion_disabled(ready_package, test_app, test_user, db_session, monkeypatch):
    kind, archive, entrypoint, files = ready_package
    monkeypatch.setattr(settings, "ENABLE_DIRECT_IMPORT", True)
    monkeypatch.setattr(settings, "ENABLE_TRANSCODING", False)
    monkeypatch.setattr(settings, "SERVE_MEDIA_VIA_NGINX", False)
    # Below the playback quality ceiling; the media fixture is genuinely 180p.
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://localhost") as client:
        login = await client.post("/api/v1/auth/login", json={"username_or_email": test_user.username, "password": "DriveFixturePassword2026!"})
        assert login.status_code == 200
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        page = await client.get("/admin/upload")
        assert page.status_code == 200
        assert "admin_ready_import.js" in page.text
        assert "admin_upload.js" not in page.text
        before = await db_session.scalar(select(func.count()).select_from(TranscodeJob))
        with archive.open("rb") as stream:
            result = await client.post("/api/v1/admin/videos/direct-import", headers=headers,
                                       files={"package": (archive.name, stream, "application/zip")})
        assert result.status_code == 201, result.text
        identifier = result.json()["id"]
        video = await db_session.get(Video, uuid.UUID(identifier))
        assert video.visibility.value == "PRIVATE"
        assert not video.is_test_content
        assert await db_session.scalar(select(func.count()).select_from(TranscodeJob)) == before
        watch = await client.get(f"/watch/{identifier}")
        assert watch.status_code == 200
        config = json.loads(re.search(r'id="player-config">\s*(.*?)</script>', watch.text, re.S).group(1))
        media_url = config["directMediaUrl"] if kind == "mp4" else f'/api/v1/playback/{config["playbackToken"]}/master.m3u8'
        media = await client.get(media_url, headers={"Range": "bytes=0-31"} if kind == "mp4" else {})
        assert media.status_code == (206 if kind == "mp4" else 200), media.text[:200]
        if kind == "mp4":
            assert media.headers["content-type"].startswith("video/mp4")
            assert media.content == files[entrypoint][:32]
        else:
            assert "index.m3u8" in media.text
        conversion = await client.post(f"/api/v1/admin/videos/{identifier}/transcode", headers=headers)
        assert conversion.status_code == 503
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://localhost") as anonymous:
            assert (await anonymous.get(f"/watch/{identifier}")).status_code == 404
            assert (await anonymous.get(media_url)).status_code in {401, 403, 404}


async def test_ready_import_requires_login_csrf_and_enabled_mode(test_app, test_user, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_DIRECT_IMPORT", True)
    path = "/api/v1/admin/videos/direct-import/resumable"
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://localhost") as client:
        assert (await client.post(path, json={})).status_code == 401
        login = await client.post("/api/v1/auth/login", json={"username_or_email": test_user.username, "password": "DriveFixturePassword2026!"})
        assert login.status_code == 200
        assert (await client.post(path, json={})).status_code == 403
        monkeypatch.setattr(settings, "ENABLE_DIRECT_IMPORT", False)
        assert (await client.post(path, json={}, headers={"X-CSRF-Token": login.json()["csrf_token"]})).status_code == 503


async def test_resumable_import_owner_offsets_and_portable_cancellation(test_app, test_user, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_DIRECT_IMPORT", True)
    root = "/api/v1/admin/videos/direct-import/resumable"
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://localhost") as client:
        login = await client.post("/api/v1/auth/login", json={"username_or_email": test_user.username, "password": "DriveFixturePassword2026!"})
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        started = await client.post(root, json={"filename": "fixture.zip", "size": 6}, headers=headers)
        assert started.status_code == 200, started.text
        upload = root + "/" + started.json()["upload_id"]
        first = await client.put(upload + "?offset=0", content=b"abc", headers=headers)
        assert first.status_code == 200 and first.json()["offset"] == 3
        assert (await client.put(upload + "?offset=0", content=b"abc", headers=headers)).status_code == 409
        assert (await client.get(upload)).json()["offset"] == 3
        assert (await client.post(upload + "/finalize", headers=headers)).status_code == 409
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://localhost") as anonymous:
            assert (await anonymous.get(upload)).status_code == 401
        assert (await client.delete(upload, headers=headers)).status_code == 204
        assert (await client.get(upload)).status_code == 404


async def test_configured_source_upload_reservation_cap_preserves_paused_slots(test_app, test_user, monkeypatch):
    monkeypatch.setattr(settings, "MAX_ACTIVE_UPLOADS_PER_USER", 2)
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://localhost") as client:
        login = await client.post("/api/v1/auth/login", json={"username_or_email": test_user.username, "password": "DriveFixturePassword2026!"})
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        payload = dict(title="Synthetic", filename="fixture.mp4", file_size_bytes=1024, chunk_size_bytes=1048576)
        one = await client.post("/api/v1/uploads/initiate", json=payload, headers=headers)
        two = await client.post("/api/v1/uploads/initiate", json=payload, headers=headers)
        assert one.status_code == two.status_code == 201
        session = "/api/v1/uploads/" + one.json()["upload_id"]
        assert (await client.post(session + "/pause", headers=headers)).status_code == 200
        assert (await client.post("/api/v1/uploads/initiate", json=payload, headers=headers)).status_code == 429
        assert (await client.post(session + "/cancel", headers=headers)).status_code == 200
        assert (await client.post("/api/v1/uploads/initiate", json=payload, headers=headers)).status_code == 201


@pytest.mark.parametrize("entry", ["../escape.mp4", "/escape.mp4", "safe/../../escape.mp4"])
def test_ready_archive_rejects_traversal_and_cleans_workspace(tmp_path, entry):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(entry, b"untrusted")
    destination = tmp_path / "extract"
    with pytest.raises(DirectImportError):
        extract_and_verify(archive, destination)
    assert not destination.exists()
    assert not (tmp_path / "escape.mp4").exists()


def test_wordpress_signature_resource_and_expiry(monkeypatch):
    import base64
    import hmac
    import time
    from fastapi import HTTPException
    from app.api.wp_private_media import _verify, TOKEN_DOMAIN
    key = secrets.token_bytes(32)
    monkeypatch.delenv("WP_PRIVATE_MEDIA_HMAC_KEYS_JSON", raising=False)
    monkeypatch.setenv("WP_PRIVATE_MEDIA_KEY_ID", "test")
    monkeypatch.setenv("WP_PRIVATE_MEDIA_HMAC_KEY", key.hex())
    resource = f'e1/{secrets.token_hex(16)}/{uuid.uuid4()}/ready.mp4'
    now = int(time.time())
    claims = dict(v=1, kid="test", ep=1, sid=resource.split('/')[1], res=resource, scope="file",
                  sub=secrets.token_hex(32), iat=now, exp=now + 60)
    def token():
        payload = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).decode().rstrip("=")
        return payload + "." + hmac.new(key, TOKEN_DOMAIN + payload.encode(), hashlib.sha256).hexdigest()
    assert _verify(token(), resource)["sub"] == claims["sub"]
    with pytest.raises(HTTPException):
        _verify(token(), resource.replace("ready.mp4", "other.mp4"))
    claims.update(iat=now - 100, exp=now - 1)
    with pytest.raises(HTTPException):
        _verify(token(), resource)


async def test_wordpress_private_media_requires_matching_session_for_every_range(test_app, test_user, db_session, tmp_path, monkeypatch):
    import base64
    import hmac
    import time
    from app.api.wp_private_media import TOKEN_DOMAIN
    from app.models.video import VideoStatus, VideoVisibility
    key = secrets.token_bytes(32)
    monkeypatch.delenv("WP_PRIVATE_MEDIA_HMAC_KEYS_JSON", raising=False)
    monkeypatch.setenv("WP_PRIVATE_MEDIA_KEY_ID", "test")
    monkeypatch.setenv("WP_PRIVATE_MEDIA_HMAC_KEY", key.hex())
    monkeypatch.setenv("WP_PRIVATE_MEDIA_ALLOWED_ORIGIN", "https://wordpress.example.test")
    monkeypatch.setattr(settings, "STORAGE_VIDEOS_PATH", str(tmp_path))
    video_id, generation_id = uuid.uuid4(), uuid.uuid4()
    video = Video(id=video_id, user_id=test_user.id, title="Synthetic bridge fixture", source_filename="clip.mp4",
                  status=VideoStatus.READY, visibility=VideoVisibility.PRIVATE, active_generation_id=generation_id)
    db_session.add(video)
    await db_session.commit()
    media = tmp_path / str(video_id) / "generations" / str(generation_id) / "clip.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"private synthetic range bytes")
    session = secrets.token_hex(32)
    sid = secrets.token_hex(16)
    resource = f"e1/{sid}/{video_id}/clip.mp4"
    now = int(time.time())
    claims = dict(v=1, kid="test", ep=1, sid=sid, res=resource, scope="file",
                  sub=hmac.new(key, ("session|" + session).encode(), hashlib.sha256).hexdigest(), iat=now, exp=now + 60)
    payload = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).decode().rstrip("=")
    token = payload + "." + hmac.new(key, TOKEN_DOMAIN + payload.encode(), hashlib.sha256).hexdigest()
    path = f"/private-media/{token}/{resource}"
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://localhost") as client:
        assert (await client.get(path, headers={"Range": "bytes=0-6"})).status_code == 404
        client.cookies.set("abv_media_session", secrets.token_hex(32))
        assert (await client.get(path, headers={"Range": "bytes=0-6"})).status_code == 404
        client.cookies.set("abv_media_session", session)
        result = await client.get(path, headers={"Range": "bytes=0-6"})
        assert result.status_code == 206 and result.content == b"private"
        assert result.headers["content-type"].startswith("video/mp4")
        assert "no-store" in result.headers["cache-control"]
        assert (await client.get(path, headers={"Origin": "https://untrusted.example.test"})).status_code == 403
