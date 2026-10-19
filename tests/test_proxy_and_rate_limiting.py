"""
Tests for:
- Trusted proxy verification & real client IP extraction
- Rate limiting isolation behind Nginx (Client A cannot block Client B)
- Single-account vs IP-level rate limiting boundaries
- Subtitle m3u8 attribute sanitization
- Chunk upload concurrency without lost updates
- Admin transcode count SQL func.count accuracy
"""

import asyncio
import uuid
from datetime import datetime, timezone, timedelta
import pytest
from httpx import AsyncClient, ASGITransport
from starlette.requests import Request

from app.core.config import settings
from app.core.network import get_real_client_ip, is_trusted_proxy
from app.core.redis import get_cache_client
from app.main import create_application
from app.models.user import User, UserRole
from app.models.upload import UploadSession, UploadSessionStatus
from app.models.video import Video, VideoStatus, TranscodeJob, TranscodeJobStatus
from app.services.upload_service import UploadService
from app.api.v1.playback import escape_m3u8_string
from sqlalchemy import select, func


@pytest.fixture
def app():
    return create_application()


def test_is_trusted_proxy_checks():
    # Loopback
    assert is_trusted_proxy("127.0.0.1") is True
    assert is_trusted_proxy("::1") is True
    # Production Docker bridge network (172.28.0.0/16)
    assert is_trusted_proxy("172.28.0.2") is True
    assert is_trusted_proxy("172.28.0.10") is True
    # Arbitrary LAN / private RFC1918 subnets must NOT be trusted proxies
    assert is_trusted_proxy("192.168.1.100") is False
    assert is_trusted_proxy("192.168.141.50") is False
    assert is_trusted_proxy("10.0.1.5") is False
    assert is_trusted_proxy("172.18.0.3") is False
    # Public untrusted IPs
    assert is_trusted_proxy("8.8.8.8") is False
    assert is_trusted_proxy("203.0.113.45") is False
    assert is_trusted_proxy("unknown") is False
    assert is_trusted_proxy(None) is False


def test_get_real_client_ip_untrusted_peer_ignores_headers():
    """An untrusted direct client cannot spoof its IP via X-Real-IP or X-Forwarded-For."""
    scope = {
        "type": "http",
        "client": ("203.0.113.10", 54321),
        "headers": [
            (b"x-real-ip", b"1.1.1.1"),
            (b"x-forwarded-for", b"2.2.2.2, 3.3.3.3"),
        ],
    }
    req = Request(scope)
    assert get_real_client_ip(req) == "203.0.113.10"


def test_get_real_client_ip_lan_client_cannot_spoof_x_real_ip():
    """A client on the local LAN (e.g. 192.168.141.x or 10.x.x.x) cannot spoof X-Real-IP."""
    scope = {
        "type": "http",
        "client": ("192.168.141.50", 54321),
        "headers": [
            (b"x-real-ip", b"8.8.8.8"),
            (b"x-forwarded-for", b"8.8.8.8"),
        ],
    }
    req = Request(scope)
    assert get_real_client_ip(req) == "192.168.141.50"


def test_get_real_client_ip_trusted_proxy_uses_x_real_ip():
    """When connecting via trusted reverse proxy (Nginx on 172.28.0.0/16), X-Real-IP is extracted."""
    scope = {
        "type": "http",
        "client": ("172.28.0.10", 54321),  # Nginx container IP in production docker bridge
        "headers": [
            (b"x-real-ip", b"198.51.100.25"),
        ],
    }
    req = Request(scope)
    assert get_real_client_ip(req) == "198.51.100.25"


def test_get_real_client_ip_trusted_proxy_uses_x_forwarded_for():
    """When X-Real-IP is absent, X-Forwarded-For traverses right-to-left for first untrusted IP."""
    scope = {
        "type": "http",
        "client": ("127.0.0.1", 54321),
        "headers": [
            (b"x-forwarded-for", b"198.51.100.55, 172.28.0.2"),
        ],
    }
    req = Request(scope)
    assert get_real_client_ip(req) == "198.51.100.55"


def test_escape_m3u8_string():
    """Ensure double-quotes, newlines, and carriage returns are sanitized."""
    assert escape_m3u8_string('Arabic "Special" Sub') == 'Arabic \\"Special\\" Sub'
    assert escape_m3u8_string("Line1\nLine2\rLine3") == "Line1Line2Line3"
    assert escape_m3u8_string('Evil\n#EXT-X-DISCONTINUITY\nNAME="hacked"') == 'Evil#EXT-X-DISCONTINUITYNAME=\\"hacked\\"'
    assert escape_m3u8_string(None) == ""


@pytest.mark.asyncio
async def test_login_rate_limiting_nginx_isolation(app):
    """
    Critical Security Acceptance Test:
    When Client A and Client B both access the backend through Nginx (same Nginx IP 172.18.0.2),
    Client A failing 5 times from IP A must NOT lock out Client B at IP B.
    """
    redis_client = get_cache_client()
    ip_a = "198.51.100.1"
    ip_b = "198.51.100.2"

    # Clean test keys
    await redis_client.delete(
        f"rate_limit:login:ip:{ip_a}",
        f"rate_limit:login:ip:{ip_b}",
        f"rate_limit:login:ip_user:{ip_a}:admin_a",
        f"rate_limit:login:ip_user:{ip_b}:admin_b",
        "rate_limit:login:user:admin_a",
        "rate_limit:login:user:admin_b",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        # Client A fails 5 times from IP A behind Nginx
        for _ in range(5):
            res_a = await client.post(
                "/api/v1/auth/login",
                json={"username_or_email": "admin_a", "password": "wrongpassword"},
                headers={"X-Real-IP": ip_a},
            )
            assert res_a.status_code == 401

        # 6th attempt from Client A must be rate limited (429)
        res_a_blocked = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "admin_a", "password": "wrongpassword"},
            headers={"X-Real-IP": ip_a},
        )
        assert res_a_blocked.status_code == 429
        assert "Retry-After" in res_a_blocked.headers

        # Client B from IP B behind the same Nginx attempts login
        # MUST NOT be 429! Must get 401 (invalid credentials) because Client B is NOT locked out.
        res_b = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "admin_b", "password": "wrongpassword"},
            headers={"X-Real-IP": ip_b},
        )
        assert res_b.status_code == 401, f"Expected 401 for Client B, but got {res_b.status_code}: {res_b.text}"


@pytest.mark.asyncio
async def test_login_rate_limiting_same_nat_different_users(app):
    """
    Users behind the same NAT/office IP:
    Failed logins on user A do not block user B on the same IP until aggregate IP limit.
    """
    redis_client = get_cache_client()
    office_ip = "203.0.113.99"

    await redis_client.delete(
        f"rate_limit:login:ip:{office_ip}",
        f"rate_limit:login:ip_user:{office_ip}:alice",
        f"rate_limit:login:ip_user:{office_ip}:bob",
        "rate_limit:login:user:alice",
        "rate_limit:login:user:bob",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        # Alice fails 5 times
        for _ in range(5):
            res = await client.post(
                "/api/v1/auth/login",
                json={"username_or_email": "alice", "password": "wrongpassword"},
                headers={"X-Real-IP": office_ip},
            )
            assert res.status_code == 401

        # Alice is now blocked (429)
        res_alice_blocked = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "alice", "password": "wrongpassword"},
            headers={"X-Real-IP": office_ip},
        )
        assert res_alice_blocked.status_code == 429

        # Bob on the same office IP is NOT blocked (only 5 total failed attempts on IP < 25)
        res_bob = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "bob", "password": "wrongpassword"},
            headers={"X-Real-IP": office_ip},
        )
        assert res_bob.status_code == 401


@pytest.mark.asyncio
async def test_chunk_upload_concurrency(tmp_path):
    """
    Verify concurrency safety of process_chunk:
    Sending chunk 0 and chunk 1 concurrently on two async tasks must safely
    record both chunks without lost updates or race conditions.
    """
    from app.services.upload_service import UploadService
    from app.core.database import AsyncSessionLocal

    chunk_size = 1024
    total_chunks = 2
    total_size = chunk_size * total_chunks

    temp_dir = tmp_path / "concurrent_upload"
    temp_dir.mkdir(parents=True, exist_ok=True)

    session_id = uuid.uuid4()
    video_id = uuid.uuid4()
    user_id = uuid.uuid4()

    from datetime import datetime, timezone, timedelta
    async with AsyncSessionLocal() as init_db:
        user = User(
            id=user_id,
            email=f"owner_{uuid.uuid4().hex[:6]}@test.com",
            username=f"owner_{uuid.uuid4().hex[:6]}",
            hashed_password="hash",
            role=UserRole.OWNER,
            is_active=True,
        )
        init_db.add(user)

        video = Video(
            id=video_id,
            title="Concurrency Test Video",
            source_filename="video.mp4",
            status=VideoStatus.UPLOADING,
            user_id=user_id,
        )
        init_db.add(video)

        session = UploadSession(
            id=session_id,
            video_id=video_id,
            user_id=user_id,
            status=UploadSessionStatus.UPLOADING,
            filename="video.mp4",
            file_size_bytes=total_size,
            chunk_size_bytes=chunk_size,
            total_chunks=total_chunks,
            received_chunks=[],
            bytes_received=0,
            temp_directory=str(temp_dir),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        init_db.add(session)
        await init_db.commit()

    chunk_0_data = b"0" * chunk_size
    chunk_1_data = b"1" * chunk_size

    # Simulate two concurrent incoming chunk requests using separate DB sessions
    async def upload_worker(chunk_idx: int, data: bytes):
        async with AsyncSessionLocal() as local_db:
            # Re-fetch session
            sess = (await local_db.execute(
                select(UploadSession).where(UploadSession.id == session_id)
            )).scalar_one()
            return await UploadService.process_chunk(
                db=local_db,
                session=sess,
                chunk_index=chunk_idx,
                chunk_data=data,
            )

    results = await asyncio.gather(
        upload_worker(0, chunk_0_data),
        upload_worker(1, chunk_1_data),
    )

    # Re-fetch from DB to verify final state
    async with AsyncSessionLocal() as verify_db:
        stmt = select(UploadSession).where(UploadSession.id == session_id)
        final_session = (await verify_db.execute(stmt)).scalar_one()

        assert sorted(final_session.received_chunks) == [0, 1]
        assert final_session.bytes_received == total_size


@pytest.mark.asyncio
async def test_admin_transcode_count_with_func_count(app):
    """
    Verify that list_transcode_jobs uses func.count and filters accurately.
    """
    from app.core.database import AsyncSessionLocal
    from datetime import datetime, timezone

    user_id = uuid.uuid4()
    v1_id = uuid.uuid4()
    v2_id = uuid.uuid4()

    async with AsyncSessionLocal() as db:
        user = User(
            id=user_id,
            email=f"owner_{uuid.uuid4().hex[:6]}@test.com",
            username=f"owner_{uuid.uuid4().hex[:6]}",
            hashed_password="hash",
            role=UserRole.OWNER,
            is_active=True,
        )
        db.add(user)

        v1 = Video(
            id=v1_id,
            title="Video 1",
            source_filename="v1.mp4",
            status=VideoStatus.READY,
            user_id=user_id,
        )
        v2 = Video(
            id=v2_id,
            title="Video 2",
            source_filename="v2.mp4",
            status=VideoStatus.READY,
            user_id=user_id,
        )
        db.add_all([v1, v2])

        # Create 2 COMPLETED jobs for v1 and 1 FAILED job for v2
        j1 = TranscodeJob(
            id=uuid.uuid4(),
            video_id=v1_id,
            status=TranscodeJobStatus.COMPLETED,
            priority=1,
            attempt=1,
            max_attempts=3,
        )
        j2 = TranscodeJob(
            id=uuid.uuid4(),
            video_id=v1_id,
            status=TranscodeJobStatus.COMPLETED,
            priority=1,
            attempt=2,
            max_attempts=3,
        )
        j3 = TranscodeJob(
            id=uuid.uuid4(),
            video_id=v2_id,
            status=TranscodeJobStatus.FAILED,
            priority=1,
            attempt=1,
            max_attempts=3,
        )
        db.add_all([j1, j2, j3])
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        # Dev bypass using X-User-ID
        headers = {"X-User-ID": str(user_id)}

        # Filter by status=COMPLETED and video_id=v1_id: must return total = 2
        res_filtered = await client.get(
            f"/api/v1/admin/transcodes?status=COMPLETED&video_id={v1_id}",
            headers=headers,
        )
        assert res_filtered.status_code == 200
        data_filtered = res_filtered.json()
        assert data_filtered["total"] == 2
        assert len(data_filtered["items"]) == 2

        # Filter by status=FAILED and video_id=v1_id: must return total = 0
        res_zero = await client.get(
            f"/api/v1/admin/transcodes?status=FAILED&video_id={v1_id}",
            headers=headers,
        )
        assert res_zero.status_code == 200
        assert res_zero.json()["total"] == 0


@pytest.mark.asyncio
async def test_viewer_password_rate_limiting_with_config_values(app):
    """
    Verify that viewer password endpoint respects settings.RATE_LIMIT_VIEWER_PASSWORD_ATTEMPTS.
    """
    from app.core.database import AsyncSessionLocal
    from app.security.hashing import hash_password
    from app.models.video import VideoVisibility

    redis_client = get_cache_client()
    user_id = uuid.uuid4()
    video_id = uuid.uuid4()
    share_id = f"pw_test_{uuid.uuid4().hex[:8]}"
    client_ip = "198.51.100.99"

    async with AsyncSessionLocal() as db:
        user = User(
            id=user_id,
            email=f"owner_{uuid.uuid4().hex[:6]}@test.com",
            username=f"owner_{uuid.uuid4().hex[:6]}",
            hashed_password="hash",
            role=UserRole.OWNER,
            is_active=True,
        )
        db.add(user)

        video = Video(
            id=video_id,
            title="Protected Video",
            active_generation_id=uuid.uuid4(),
            source_filename="prot.mp4",
            status=VideoStatus.READY,
            visibility=VideoVisibility.PASSWORD_PROTECTED,
            viewer_password_hash=hash_password("correctpass123"),
            share_id=share_id,
            user_id=user_id,
        )
        db.add(video)
        await db.commit()

    # Clear rate limit key
    rate_limit_key = f"ratelimit:pw:{client_ip}:{share_id}"
    await redis_client.delete(rate_limit_key)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        # Fail RATE_LIMIT_VIEWER_PASSWORD_ATTEMPTS times (10 times)
        for i in range(settings.RATE_LIMIT_VIEWER_PASSWORD_ATTEMPTS):
            res = await client.post(
                f"/api/v1/watch/{share_id}/verify-password",
                json={"password": "badpassword"},
                headers={"X-Real-IP": client_ip},
            )
            assert res.status_code == 401

        # Attempt 11 must be rate limited with 429
        res_blocked = await client.post(
            f"/api/v1/watch/{share_id}/verify-password",
            json={"password": "badpassword"},
            headers={"X-Real-IP": client_ip},
        )
        assert res_blocked.status_code == 429
        assert "Retry-After" in res_blocked.headers


@pytest.mark.asyncio
async def test_upload_finalization_internal_error_does_not_leak_traceback(tmp_path):
    """
    Verify that an unexpected internal error during finalization raises a safe generic 500 error
    without leaking internal exception strings, stack traces, or local disk paths.
    """
    from app.core.database import AsyncSessionLocal
    from app.services.upload_service import UploadService, UploadException
    from unittest.mock import patch

    user_id = uuid.uuid4()
    video_id = uuid.uuid4()
    session_id = uuid.uuid4()

    async with AsyncSessionLocal() as db:
        user = User(
            id=user_id,
            email=f"owner_{uuid.uuid4().hex[:6]}@test.com",
            username=f"owner_{uuid.uuid4().hex[:6]}",
            hashed_password="hash",
            role=UserRole.OWNER,
            is_active=True,
        )
        db.add(user)

        video = Video(
            id=video_id,
            title="Video",
            source_filename="test.mp4",
            status=VideoStatus.UPLOADING,
            user_id=user_id,
        )
        db.add(video)

        session = UploadSession(
            id=session_id,
            video_id=video_id,
            user_id=user_id,
            status=UploadSessionStatus.UPLOADING,
            filename="test.mp4",
            file_size_bytes=100,
            chunk_size_bytes=100,
            total_chunks=1,
            received_chunks=[0],
            bytes_received=100,
            temp_directory=str(tmp_path),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        db.add(session)
        await db.commit()

        # Create dummy chunk
        chunk_file = tmp_path / "chunk_0.part"
        chunk_file.write_bytes(b"A" * 100)

        # Mock an unexpected internal exception (e.g. disk driver failure with secret path)
        secret_leak = "C:\\Windows\\System32\\leaked_secret_path.sys: driver fault 0xDEADBEEF"
        with patch("app.services.upload_service.validate_video_file", side_effect=RuntimeError(secret_leak)):
            with pytest.raises(UploadException) as exc_info:
                await UploadService.finalize_upload(db, session)

            assert exc_info.value.status_code == 500
            assert secret_leak not in str(exc_info.value.message)
            assert "Upload finalization failed due to an internal server error." in str(exc_info.value.message)
