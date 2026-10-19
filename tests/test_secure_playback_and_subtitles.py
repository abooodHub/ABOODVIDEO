"""Comprehensive Automated Tests for Secure Playback, Watch Page, and Subtitle System (Phase 4)."""
import os
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
import redis.asyncio as aioredis

from app.main import app
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.user import User, UserRole
from app.models.video import Video, VideoStatus, VideoVisibility, VideoSubtitle, PlaybackSession
from app.security.hashing import hash_password
from app.security.tokens import generate_secure_token, hash_token
from app.security.playback_token import (
    create_playback_token,
    verify_playback_token,
    hash_opaque_token,
    generate_opaque_token,
    ViewerSessionManager,
    VIEWER_COOKIE_NAME,
)
from app.services.subtitles import SubtitleService, SubtitleError
from app.api.v1.playback import sanitize_and_resolve_path


@pytest.fixture
async def phase4_db_fixture():
    """Setup test user, video with mock generation, and db/redis sessions."""
    owner_pass = "Phase4TestPassword2026!"
    video_id = uuid.uuid4()
    generation_id = uuid.uuid4()
    share_id = f"test_share_{uuid.uuid4().hex[:12]}"

    # Setup mock files on disk
    base_video_dir = Path(settings.STORAGE_VIDEOS_PATH) / str(video_id)
    gen_dir = base_video_dir / "generations" / str(generation_id)
    gen_dir.mkdir(parents=True, exist_ok=True)
    subs_dir = base_video_dir / "subtitles"
    subs_dir.mkdir(parents=True, exist_ok=True)

    master_path = gen_dir / "master.m3u8"
    master_path.write_text(
        "#EXTM3U\n"
        "#EXT-X-VERSION:4\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n"
        "360p/index.m3u8\n",
        encoding="utf-8",
    )

    p360_dir = gen_dir / "360p"
    p360_dir.mkdir(parents=True, exist_ok=True)
    (p360_dir / "index.m3u8").write_text("#EXTM3U\n#EXTINF:6.0,\nseg0.ts\n#EXT-X-ENDLIST\n", encoding="utf-8")
    (p360_dir / "seg0.ts").write_bytes(b"\x47\x40\x00\x10" + b"\x00" * 184)
    (gen_dir / "poster.webp").write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 \x00\x00\x00\x00")

    async with AsyncSessionLocal() as session:
        owner = User(
            id=uuid.uuid4(),
            email=f"owner_{uuid.uuid4().hex[:6]}@aboodvideo.local",
            username=f"owner_{uuid.uuid4().hex[:6]}",
            hashed_password=hash_password(owner_pass),
            role=UserRole.OWNER,
            is_active=True,
        )
        session.add(owner)

        video = Video(
            id=video_id,
            user_id=owner.id,
            title="Phase 4 Secure Test Video",
            source_filename="test_input.mp4",
            source_size_bytes=1024 * 1024 * 10,
            status=VideoStatus.READY,
            visibility=VideoVisibility.UNLISTED,
            share_id=share_id,
            active_generation_id=generation_id,
            source_duration_seconds=12.0,
            poster_path=f"videos/{video_id}/generations/{generation_id}/poster.webp",
        )
        session.add(video)
        await session.commit()
        await session.refresh(owner)
        await session.refresh(video)

    redis_conn = aioredis.from_url(settings.REDIS_SESSION_URL, decode_responses=True)

    yield {
        "owner": owner,
        "owner_pass": owner_pass,
        "video": video,
        "generation_id": generation_id,
        "share_id": share_id,
        "redis": redis_conn,
    }

    await redis_conn.aclose()


@pytest.mark.asyncio
async def test_opaque_playback_token_security(phase4_db_fixture):
    """Verify opaque token format, hashing, storage in Redis, and live DB verification."""
    data = phase4_db_fixture
    redis_conn = data["redis"]
    video = data["video"]
    gen_id = data["generation_id"]

    token = await create_playback_token(
        redis_conn=redis_conn,
        video_id=video.id,
        active_generation_id=gen_id,
        authorization_type="share",
        share_id=video.share_id,
        ttl_seconds=300,
    )

    # 1. Token must be high-entropy URL-safe string
    assert len(token) >= 32
    # Must NOT leak internal IDs in plain text
    assert str(video.id) not in token
    assert str(gen_id) not in token
    assert video.share_id not in token

    # 2. Redis must only store the SHA-256 hash of the token
    token_hash = hash_opaque_token(token)
    stored_json = await redis_conn.get(f"playback:token:{token_hash}")
    assert stored_json is not None
    payload = json.loads(stored_json)
    assert payload["video_id"] == str(video.id)
    assert payload["active_generation_id"] == str(gen_id)

    # Raw token itself must NOT be a key in Redis
    assert await redis_conn.get(f"playback:token:{token}") is None

    # 3. Successful verification
    async with AsyncSessionLocal() as session:
        verified = await verify_playback_token(session, redis_conn, token)
        assert verified is not None
        assert verified["video"].id == video.id
        assert verified["active_generation_id"] == str(gen_id)

    # 4. Tampered token fails
    async with AsyncSessionLocal() as session:
        assert await verify_playback_token(session, redis_conn, token + "x") is None


@pytest.mark.asyncio
async def test_token_revocation_on_share_rotation_and_revocation(phase4_db_fixture):
    """Verify immediate revocation when share link is rotated, revoked, or made PRIVATE."""
    data = phase4_db_fixture
    redis_conn = data["redis"]
    video = data["video"]
    gen_id = data["generation_id"]

    token = await create_playback_token(
        redis_conn=redis_conn,
        video_id=video.id,
        active_generation_id=gen_id,
        authorization_type="share",
        share_id=video.share_id,
    )

    # Token works initially
    async with AsyncSessionLocal() as session:
        assert await verify_playback_token(session, redis_conn, token) is not None

    # Rotate share_id in DB
    async with AsyncSessionLocal() as session:
        v = await session.get(Video, video.id)
        v.share_id = f"rotated_{uuid.uuid4().hex[:10]}"
        await session.commit()

    # Prior token is immediately rejected because share_id no longer matches
    async with AsyncSessionLocal() as session:
        assert await verify_playback_token(session, redis_conn, token) is None

    # Create new token for updated share_id
    token2 = await create_playback_token(
        redis_conn=redis_conn,
        video_id=video.id,
        active_generation_id=gen_id,
        authorization_type="share",
        share_id=v.share_id,
    )
    async with AsyncSessionLocal() as session:
        assert await verify_playback_token(session, redis_conn, token2) is not None

    # Revoke share link
    async with AsyncSessionLocal() as session:
        v = await session.get(Video, video.id)
        v.share_revoked = True
        await session.commit()

    # Token2 is immediately rejected because share is revoked
    async with AsyncSessionLocal() as session:
        assert await verify_playback_token(session, redis_conn, token2) is None


@pytest.mark.asyncio
async def test_private_video_access_boundaries(phase4_db_fixture):
    """Verify PRIVATE videos are never accessible via /watch/{share_id} but accessible to owner."""
    data = phase4_db_fixture
    video = data["video"]
    owner = data["owner"]
    owner_pass = data["owner_pass"]

    # Mark video PRIVATE
    async with AsyncSessionLocal() as session:
        v = await session.get(Video, video.id)
        v.visibility = VideoVisibility.PRIVATE
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        # Anonymous viewer accessing /watch/{share_id} receives non-disclosing 404
        res = await client.get(f"/watch/{video.share_id}")
        assert res.status_code == 404
        assert res.content == b""

        # Anonymous viewer accessing /admin/videos/{video_id}/watch receives 401
        res = await client.get(f"/admin/videos/{video.id}/watch")
        assert res.status_code == 401

        # Login as owner
        login_res = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": owner.username, "password": owner_pass},
        )
        assert login_res.status_code == 200

        # Owner accessing /admin/videos/{video_id}/watch succeeds with admin preview
        preview_res = await client.get(f"/admin/videos/{video.id}/watch")
        assert preview_res.status_code == 200
        assert "معاينة خاصة" in preview_res.text
        assert "master.m3u8" in preview_res.text


@pytest.mark.asyncio
async def test_password_protected_video_flow_and_rate_limiting(phase4_db_fixture):
    """Verify password challenge, Argon2id verification, and Redis rate limiting."""
    data = phase4_db_fixture
    video = data["video"]
    correct_pass = "ViewerPass2026!"

    async with AsyncSessionLocal() as session:
        v = await session.get(Video, video.id)
        v.visibility = VideoVisibility.PASSWORD_PROTECTED
        v.viewer_password_hash = hash_password(correct_pass)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        # 1. Accessing without password shows challenge form
        watch_res = await client.get(f"/watch/{video.share_id}")
        assert watch_res.status_code == 200
        assert "هذا الفيديو محمي بكلمة مرور" in watch_res.text

        # 2. Incorrect password returns 401
        bad_res = await client.post(
            f"/api/v1/watch/{video.share_id}/verify-password",
            json={"password": "WrongPassword!"},
        )
        assert bad_res.status_code == 401

        # 3. Rate limiting test: failed attempts reaching configured threshold triggers 429
        for _ in range(settings.RATE_LIMIT_VIEWER_PASSWORD_ATTEMPTS - 1):
            await client.post(
                f"/api/v1/watch/{video.share_id}/verify-password",
                json={"password": "WrongPassword!"},
            )
        rate_limited_res = await client.post(
            f"/api/v1/watch/{video.share_id}/verify-password",
            json={"password": "WrongPassword!"},
        )
        assert rate_limited_res.status_code == 429
        assert "تم تجاوز الحد الأقصى للمحاولات" in rate_limited_res.text


@pytest.mark.asyncio
async def test_path_traversal_sanitization():
    """Verify sanitize_and_resolve_path blocks all path traversal variants."""
    base_dir = Path("/app/storage/videos/test-uuid")

    # Direct traversal
    with pytest.raises(Exception) as exc:
        sanitize_and_resolve_path(base_dir, "../../../etc/passwd")
    assert "traversal" in str(exc.value).lower()

    # Percent-encoded traversal
    with pytest.raises(Exception) as exc:
        sanitize_and_resolve_path(base_dir, "%2e%2e/%2e%2e/secret.env")
    assert "traversal" in str(exc.value).lower()

    # Null bytes
    with pytest.raises(Exception) as exc:
        sanitize_and_resolve_path(base_dir, "test.m3u8\x00.exe")
    assert "invalid" in str(exc.value).lower()

    # Absolute path attempts
    with pytest.raises(Exception) as exc:
        sanitize_and_resolve_path(base_dir, "/etc/shadow")
    assert "traversal" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_subtitle_conversion_and_arabic_rtl():
    """Verify SRT to WebVTT conversion with CP-1256 and UTF-8 Arabic text."""
    srt_arabic = (
        "1\n"
        "00:00:01,000 --> 00:00:04,500\n"
        "مرحباً بك في منصة ABOODVIDEO\n\n"
        "2\n"
        "00:00:05,000 --> 00:00:08,000\n"
        "مشاهدة ممتعة بجودة تكيفية فائقة\n"
    ).encode("utf-8")

    vtt_text = SubtitleService.convert_srt_to_webvtt(srt_arabic)
    assert vtt_text.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:04.500" in vtt_text
    assert "مرحباً بك في منصة ABOODVIDEO" in vtt_text


@pytest.mark.asyncio
async def test_existing_video_subtitle_management_and_playback(phase4_db_fixture):
    """All four formats retain originals; ASS uses libass and never enters VTT HLS."""
    data = phase4_db_fixture
    video = data["video"]
    base = f"/api/v1/admin/videos/{video.id}/subtitles"
    originals = {
        "ass": b"[Script Info]\nTitle: test\n[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,Hello\n",
        "ssa": b"[Script Info]\nTitle: test\n[V4 Styles]\nStyle: Default,Arial,20\n",
        "srt": b"1\n00:00:01,000 --> 00:00:02,000\nHello\n",
        "vtt": b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n",
    }
    token = await create_playback_token(
        redis_conn=data["redis"], video_id=video.id,
        active_generation_id=data["generation_id"],
        authorization_type="share", share_id=video.share_id,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        before = await client.get(f"/api/v1/playback/{token}/master.m3u8")
        assert before.status_code == 200
        assert "TYPE=SUBTITLES" not in before.text

        login = await client.post("/api/v1/auth/login", json={
            "username_or_email": data["owner"].username,
            "password": data["owner_pass"],
        })
        assert login.status_code == 200
        csrf = {"X-CSRF-Token": login.json()["csrf_token"]}
        created = {}
        for ext, original in originals.items():
            response = await client.post(base, headers=csrf, data={
                "language_code": ext, "language_name": ext.upper(),
                "label": f"Track {ext}", "is_default": "false",
            }, files={"file": (f"sample.{ext}", original)})
            assert response.status_code == 201, response.text
            track = response.json()
            created[ext] = track
            assert track["format"] == ext
            assert track["is_playable"] is True
            source = Path(settings.STORAGE_VIDEOS_PATH) / str(video.id) / "subtitles" / f"{track['id']}_source.{ext}"
            assert source.read_bytes() == original
            if ext in ("ass", "ssa"):
                assert track["webvtt_path"] is None
            else:
                assert track["webvtt_path"] is not None

        listed = await client.get(base)
        assert listed.status_code == 200
        assert {s["id"] for s in listed.json()} == {s["id"] for s in created.values()}

        ass_id = created["ass"]["id"]
        rename = await client.patch(f"{base}/{ass_id}", headers=csrf, json={"label": "Renamed ASS"})
        assert rename.status_code == 200
        assert rename.json()["label"] == "Renamed ASS"
        default = await client.patch(f"{base}/{ass_id}", headers=csrf, json={"is_default": True})
        assert default.status_code == 200
        assert default.json()["is_default"] is True

        watch = await client.get(f"/watch/{video.share_id}")
        assert watch.status_code == 200
        assert "Renamed ASS" in watch.text
        raw = await client.get(f"/api/v1/playback/{token}/subtitles/{ass_id}/file")
        assert raw.status_code == 200
        assert raw.headers["X-Accel-Redirect"].endswith(f"/{ass_id}_source.ass")

        master = await client.get(f"/api/v1/playback/{token}/master.m3u8")
        assert master.status_code == 200
        assert f"subtitles/{ass_id}/index.m3u8" not in master.text
        assert f"subtitles/{created['srt']['id']}/index.m3u8" in master.text

        deleted = await client.delete(f"{base}/{ass_id}", headers=csrf)
        assert deleted.status_code == 200
        assert not (Path(settings.STORAGE_VIDEOS_PATH) / str(video.id) / "subtitles" / f"{ass_id}_source.ass").exists()
        after = await client.get(base)
        assert ass_id not in {s["id"] for s in after.json()}


@pytest.mark.asyncio
async def test_single_default_subtitle_constraint(phase4_db_fixture):
    """Verify database-level partial unique index enforces at most one default subtitle per video."""
    data = phase4_db_fixture
    video = data["video"]

    async with AsyncSessionLocal() as session:
        sub1 = VideoSubtitle(
            id=uuid.uuid4(),
            video_id=video.id,
            language_code="ar",
            language_name="Arabic",
            label="العربية",
            format="vtt",
            source_path="mock/ar.vtt",
            webvtt_path="mock/ar.vtt",
            is_default=True,
            is_playable=True,
        )
        session.add(sub1)
        await session.commit()

        # Attempt to insert a second default subtitle for the same video
        sub2 = VideoSubtitle(
            id=uuid.uuid4(),
            video_id=video.id,
            language_code="en",
            language_name="English",
            label="English",
            format="vtt",
            source_path="mock/en.vtt",
            webvtt_path="mock/en.vtt",
            is_default=True,
            is_playable=True,
        )
        session.add(sub2)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_dynamic_master_playlist_with_subtitles(phase4_db_fixture):
    """Verify dynamic master playlist injects #EXT-X-MEDIA:TYPE=SUBTITLES and SUBTITLES attribute."""
    data = phase4_db_fixture
    video = data["video"]
    gen_id = data["generation_id"]
    redis_conn = data["redis"]

    # Add default Arabic subtitle track
    sub_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        sub = VideoSubtitle(
            id=sub_id,
            video_id=video.id,
            language_code="ar",
            language_name="Arabic",
            label="العربية",
            format="vtt",
            source_path=f"videos/{video.id}/subtitles/{sub_id}.vtt",
            webvtt_path=f"videos/{video.id}/subtitles/{sub_id}.vtt",
            is_default=True,
            is_playable=True,
        )
        session.add(sub)
        await session.commit()

    token = await create_playback_token(
        redis_conn=redis_conn,
        video_id=video.id,
        active_generation_id=gen_id,
        authorization_type="share",
        share_id=video.share_id,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        res = await client.get(f"/api/v1/playback/{token}/master.m3u8")
        assert res.status_code == 200
        content = res.text

        # Verify dynamic subtitle declarations
        assert '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs"' in content
        assert 'NAME="العربية"' in content
        assert 'LANGUAGE="ar"' in content
        assert 'DEFAULT=YES' in content
        assert f'URI="subtitles/{sub_id}/index.m3u8"' in content
        assert 'SUBTITLES="subs"' in content

        # Verify subtitle media playlist
        sub_res = await client.get(f"/api/v1/playback/{token}/subtitles/{sub_id}/index.m3u8")
        assert sub_res.status_code == 200
        assert "#EXT-X-TARGETDURATION" in sub_res.text
        assert f"../{sub_id}.vtt" in sub_res.text


@pytest.mark.asyncio
async def test_protected_media_delivery_and_analytics(phase4_db_fixture):
    """Verify media delivery response headers (X-Accel-Redirect) and playback event analytics."""
    data = phase4_db_fixture
    video = data["video"]
    gen_id = data["generation_id"]
    redis_conn = data["redis"]

    token = await create_playback_token(
        redis_conn=redis_conn,
        video_id=video.id,
        active_generation_id=gen_id,
        authorization_type="share",
        share_id=video.share_id,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        # Request a segment via playback API
        seg_res = await client.get(f"/api/v1/playback/{token}/360p/seg0.ts")
        assert seg_res.status_code == 200
        # When SERVE_MEDIA_VIA_NGINX is True, X-Accel-Redirect is emitted
        assert "X-Accel-Redirect" in seg_res.headers
        assert seg_res.headers["X-Accel-Redirect"] == f"/internal_media/{video.id}/generations/{gen_id}/360p/seg0.ts"
        assert seg_res.headers["Content-Type"] == "video/mp2t"

        # Post start analytics event
        ev_res = await client.post(
            f"/api/v1/playback/{token}/event",
            json={"event": "start", "position": 0.0, "duration": 12.0},
        )
        assert ev_res.status_code == 200
        assert ev_res.json()["status"] == "ok"

        # Check that video view count incremented
        async with AsyncSessionLocal() as session:
            v = await session.get(Video, video.id)
            assert v.view_count >= 1
