"""
Tests for Phase 5 Studio Packages and API Key Authentication:
- API key generation with abv_ prefix and scope enforcement
- Authoritative PostgreSQL revocation (immediate revocation invariant & positive cache race)
- Studio package upload session lifecycle: init, part streaming, idempotent resume, async finalize (202)
- Manifest path sanitization (rejection of directory traversal, absolute paths)
- Background reconciler for stalled sessions
"""

import hashlib
import json
import os
import uuid
import pytest
import secrets
from datetime import datetime, timezone, timedelta
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, update

from app.main import create_application
from app.core.config import settings
from app.core.database import get_db_context
from app.core.redis import get_redis_cache
from app.models.user import User, UserRole
from app.models.session import UserSession
from app.models.video import Video, VideoStatus
from app.models.api_key import ApiKey
from app.models.package import PackageUploadSession, PackageFile, PackageStatus, PackageFileStatus
from app.security.hashing import hash_password
from app.security.tokens import hash_token
from app.workers.reconciler import reconcile_stalled_package_jobs


@pytest.fixture
def app():
    return create_application()


@pytest.fixture(autouse=True)
def worker_heartbeat():
    import time
    from redis import Redis
    sync_redis = Redis.from_url(settings.REDIS_QUEUE_URL)
    w_id = "test-worker-01"
    sync_redis.sadd("worker:registered_workers", w_id)
    sync_redis.set(f"worker:last_seen:{w_id}", str(time.time()))
    sync_redis.set(f"worker:heartbeat:{w_id}", "1", ex=60)
    yield
    sync_redis.srem("worker:registered_workers", w_id)
    sync_redis.delete(f"worker:last_seen:{w_id}", f"worker:heartbeat:{w_id}")


@pytest.fixture
async def admin_token():
    token = secrets.token_urlsafe(32)
    async with get_db_context() as db:
        res = await db.execute(select(User).where(User.username == "admin_test_p5"))
        user = res.scalar_one_or_none()
        if not user:
            user = User(
                username="admin_test_p5",
                email="admin_test_p5@aboodvideo.local",
                hashed_password=hash_password("AdminPass123!"),
                role=UserRole.ADMIN,
                is_active=True,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)

        user_session = UserSession(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        db.add(user_session)
        await db.commit()

    return token


@pytest.fixture
async def test_video(admin_token):
    async with get_db_context() as db:
        res = await db.execute(select(User).where(User.username == "admin_test_p5"))
        admin = res.scalar_one_or_none()
        video = Video(
            title="Studio Package Test Video",
            source_filename="test.mp4",
            status=VideoStatus.READY,
            user_id=admin.id,
        )
        db.add(video)
        await db.commit()
        await db.refresh(video)
        return video


@pytest.mark.asyncio
async def test_api_key_lifecycle_and_scope(app, admin_token):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        # 1. Create API Key
        res = await client.post(
            "/api/v1/studio/keys",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "name": "Windows Studio Test Key",
                "scopes": ["studio:write", "system:monitor"],
                "expires_days": 30
            }
        )
        assert res.status_code == 201
        data = res.json()
        raw_key = data["raw_key"]
        key_id = data["id"]
        assert raw_key.startswith(("ak_live_", "abv_"))
        assert "studio:write" in data["scopes"]

        # 2. Authenticate using API Key for protected worker health endpoint
        res2 = await client.get(
            "/api/v1/health/worker",
            headers={"Authorization": f"Bearer {raw_key}"}
        )
        assert res2.status_code == 200
        assert res2.json()["status"] in ("ok", "healthy")

        # Active keys cannot be permanently deleted.
        res3 = await client.delete(
            f"/api/v1/studio/keys/{key_id}",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert res3.status_code == 409
        assert res3.json()["detail"]["code"] == "KEY_MUST_BE_REVOKED_FIRST"

        revoked = await client.post(
            f"/api/v1/studio/keys/{key_id}/revoke",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert revoked.status_code == 200

        # 4. Immediate rejection after revocation
        res4 = await client.get(
            "/api/v1/health/worker",
            headers={"Authorization": f"Bearer {raw_key}"}
        )
        assert res4.status_code == 401

        deleted = await client.delete(
            f"/api/v1/studio/keys/{key_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert deleted.status_code == 204
        async with get_db_context() as db:
            assert await db.get(ApiKey, uuid.UUID(key_id)) is None
        deleted_again = await client.delete(
            f"/api/v1/studio/keys/{key_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert deleted_again.status_code == 404


@pytest.mark.asyncio
async def test_api_key_authoritative_postgres_race_rejection(app, admin_token):
    """
    Mandatory Phase 5 invariant test:
    Even if Redis tombstone fails/missing and a stale positive cache exists,
    PostgreSQL is authoritative and rejects the revoked key with 401.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        res = await client.post(
            "/api/v1/studio/keys",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "name": "Race Test Key",
                "scopes": ["system:monitor"]
            }
        )
        assert res.status_code == 201
        raw_key = res.json()["raw_key"]
        key_id = res.json()["id"]

        # Directly simulate database deactivation without setting Redis tombstone
        async with get_db_context() as db:
            await db.execute(
                update(ApiKey).where(ApiKey.id == uuid.UUID(key_id)).values(is_active=False)
            )
            await db.commit()

        # The authentication must consult PostgreSQL and immediately reject with 401
        res2 = await client.get(
            "/api/v1/health/worker",
            headers={"Authorization": f"Bearer {raw_key}"}
        )
        assert res2.status_code == 401


@pytest.mark.asyncio
async def test_manifest_validation_rejects_directory_traversal(app, admin_token, test_video):
    # Create key with studio:write
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        res_key = await client.post(
            "/api/v1/studio/keys",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"name": "Traversal Test Key", "scopes": ["studio:write"]}
        )
        raw_key = res_key.json()["raw_key"]

        # Attempt to submit manifest with directory traversal
        bad_manifest = {
            "video_id": str(test_video.id),
            "manifest": {
                "manifest_version": "1.0",
                "video": {
                    "title": "Traversal Test",
                    "duration_seconds": 10.0
                },
                "renditions": [
                    {
                        "quality_label": "720p",
                        "width": 1280,
                        "height": 720,
                        "video_bitrate_bps": 2500000,
                        "playlist_path": "../../../etc/passwd",
                        "segment_count": 1
                    }
                ],
                "files": [
                    {
                        "path": "../../../etc/passwd",
                        "size_bytes": 100,
                        "sha256": "0" * 64
                    }
                ]
            }
        }
        res = await client.post(
            "/api/v1/studio/packages/init",
            headers={"Authorization": f"Bearer {raw_key}"},
            json=bad_manifest
        )
        assert res.status_code in (400, 422)


@pytest.mark.asyncio
async def test_studio_package_upload_and_async_finalize(app, admin_token, test_video):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        # Create key
        res_key = await client.post(
            "/api/v1/studio/keys",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"name": "Upload Package Test Key", "scopes": ["studio:write"]}
        )
        raw_key = res_key.json()["raw_key"]

        # Valid manifest
        dummy_content = b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\nseg_000.ts\n#EXT-X-ENDLIST\n"
        dummy_sha = hashlib.sha256(dummy_content).hexdigest()

        manifest = {
            "video_id": str(test_video.id),
            "manifest": {
                "manifest_version": "1.0",
                "video": {
                    "title": "Studio Package Test",
                    "duration_seconds": 6.0
                },
                "renditions": [
                    {
                        "quality_label": "720p",
                        "width": 1280,
                        "height": 720,
                        "video_bitrate_bps": 2500000,
                        "playlist_path": "index.m3u8",
                        "segment_count": 1
                    }
                ],
                "subtitles": [],
                "files": [
                    {
                        "path": "index.m3u8",
                        "size_bytes": len(dummy_content),
                        "sha256": dummy_sha
                    }
                ]
            }
        }

        # 1. Init Session
        res_init = await client.post(
            "/api/v1/studio/packages/init",
            headers={"Authorization": f"Bearer {raw_key}"},
            json=manifest
        )
        assert res_init.status_code == 201
        session_data = res_init.json()
        session_id = session_data["session_id"]
        assert session_data["status"] == "INITIALIZED"
        assert len(session_data["files"]) == 1
        file_id = session_data["files"][0]["file_id"]

        # 2. Upload File Part
        res_file = await client.put(
            f"/api/v1/studio/packages/{session_id}/files/{file_id}",
            headers={
                "Authorization": f"Bearer {raw_key}",
                "Content-Type": "application/octet-stream"
            },
            content=dummy_content
        )
        assert res_file.status_code == 200
        assert res_file.json()["status"] == "COMPLETED"

        # 3. Idempotent re-upload of same file
        res_file_repeat = await client.put(
            f"/api/v1/studio/packages/{session_id}/files/{file_id}",
            headers={
                "Authorization": f"Bearer {raw_key}",
                "Content-Type": "application/octet-stream"
            },
            content=dummy_content
        )
        assert res_file_repeat.status_code == 200
        assert res_file_repeat.json()["already_completed"] is True

        # 4. Finalize Session (Must return HTTP 202 Accepted)
        res_finalize = await client.post(
            f"/api/v1/studio/packages/{session_id}/finalize",
            headers={"Authorization": f"Bearer {raw_key}"}
        )
        assert res_finalize.status_code == 202
        finalize_data = res_finalize.json()
        assert finalize_data["status"] == "QUEUED_FOR_VERIFICATION"


@pytest.mark.asyncio
async def test_reconciler_marks_stalled_sessions_failed():
    # Insert a fake session that has been in VERIFYING for 2 hours
    stale_time = datetime.now(timezone.utc) - timedelta(hours=2)
    async with get_db_context() as db:
        res = await db.execute(select(User).where(User.role == UserRole.ADMIN))
        admin = res.scalars().first()
        res_v = await db.execute(select(Video))
        video = res_v.scalars().first()

        session = PackageUploadSession(
            video_id=video.id,
            status=PackageStatus.VERIFYING.value,
            target_generation_id=uuid.uuid4(),
            total_bytes=1000,
            completed_bytes=1000,
            total_files=1,
            manifest_data={"version": 1},
            staging_path="/tmp/fake_staging",
            created_at=stale_time,
            updated_at=stale_time,
            expires_at=datetime.now(timezone.utc) + timedelta(days=2)
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        stale_session_id = session.id

    # Run reconciler
    recovered = await reconcile_stalled_package_jobs()
    assert recovered >= 1

    # Verify session status is now FAILED
    async with get_db_context() as db:
        res_check = await db.execute(
            select(PackageUploadSession).where(PackageUploadSession.id == stale_session_id)
        )
        updated_session = res_check.scalar_one()
        assert updated_session.status == PackageStatus.FAILED.value
        assert "timeout" in (updated_session.error_message or "").lower()


@pytest.mark.asyncio
async def test_reconciler_preserves_verifying_session_when_rq_job_is_active(monkeypatch):
    """
    Item 3 Invariant:
    A session in VERIFYING state beyond the cutoff window MUST NOT be failed
    if its corresponding RQ job is still alive and actively running in Redis.
    """
    from rq.job import Job
    stale_time = datetime.now(timezone.utc) - timedelta(hours=2)

    async with get_db_context() as db:
        res_v = await db.execute(select(Video))
        video = res_v.scalars().first()

        active_job_id = f"pkg_verify_active_{uuid.uuid4().hex[:8]}"
        session = PackageUploadSession(
            video_id=video.id,
            status=PackageStatus.VERIFYING.value,
            target_generation_id=uuid.uuid4(),
            verification_job_id=active_job_id,
            total_bytes=1000,
            completed_bytes=1000,
            total_files=1,
            manifest_data={"version": 1},
            staging_path="/tmp/fake_staging_active",
            created_at=stale_time,
            updated_at=stale_time,
            expires_at=datetime.now(timezone.utc) + timedelta(days=2),
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        active_session_id = session.id

    # Mock Job.fetch to simulate an actively running job (status="started")
    class MockActiveJob:
        def get_status(self):
            return "started"

    orig_fetch = Job.fetch

    def mock_fetch(job_id, connection=None):
        if job_id == active_job_id:
            return MockActiveJob()
        return orig_fetch(job_id, connection=connection)

    monkeypatch.setattr(Job, "fetch", mock_fetch)

    # Run reconciler
    recovered = await reconcile_stalled_package_jobs()

    # Verify session status is STILL VERIFYING (not failed!)
    async with get_db_context() as db:
        res_check = await db.execute(
            select(PackageUploadSession).where(PackageUploadSession.id == active_session_id)
        )
        kept_session = res_check.scalar_one()
        assert kept_session.status == PackageStatus.VERIFYING.value
        assert kept_session.error_message is None


@pytest.mark.asyncio
async def test_api_key_granular_least_privilege_scope_boundaries(app, admin_token, test_video):
    """
    Item 4 Invariant:
    Granular scopes enforce strict least-privilege:
    - packages:create cannot call finalize (403)
    - packages:write cannot access /health/worker (403)
    - system:monitor cannot init package (403)
    - aggregate studio:write cannot access /health/worker without system:monitor (403)
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        # 1. Key with packages:create only
        res_create_only = await client.post(
            "/api/v1/studio/keys",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"name": "Create Only Key", "scopes": ["packages:create"]}
        )
        key_create_only = res_create_only.json()["raw_key"]

        # Can init package
        dummy_manifest = {
            "video_id": str(test_video.id),
            "manifest": {
                "manifest_version": "1.0",
                "video": {"title": "Scope Test", "duration_seconds": 5.0},
                "renditions": [
                    {
                        "quality_label": "720p",
                        "width": 1280,
                        "height": 720,
                        "video_bitrate_bps": 2000000,
                        "playlist_path": "index.m3u8",
                        "segment_count": 1
                    }
                ],
                "files": [{"path": "index.m3u8", "size_bytes": 10, "sha256": "a" * 64}]
            }
        }
        res_init = await client.post(
            "/api/v1/studio/packages/init",
            headers={"Authorization": f"Bearer {key_create_only}"},
            json=dummy_manifest
        )
        assert res_init.status_code == 201
        session_id = res_init.json()["session_id"]
        file_id = res_init.json()["files"][0]["file_id"]

        # But CANNOT finalize (requires packages:publish)
        res_finalize = await client.post(
            f"/api/v1/studio/packages/{session_id}/finalize",
            headers={"Authorization": f"Bearer {key_create_only}"}
        )
        assert res_finalize.status_code == 403

        # And CANNOT access worker health (requires system:monitor)
        res_worker = await client.get(
            "/api/v1/health/worker",
            headers={"Authorization": f"Bearer {key_create_only}"}
        )
        assert res_worker.status_code == 403

        # 2. Key with packages:write only
        res_write_only = await client.post(
            "/api/v1/studio/keys",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"name": "Write Only Key", "scopes": ["packages:write"]}
        )
        key_write_only = res_write_only.json()["raw_key"]

        # CANNOT init package (requires packages:create)
        res_init_fail = await client.post(
            "/api/v1/studio/packages/init",
            headers={"Authorization": f"Bearer {key_write_only}"},
            json=dummy_manifest
        )
        assert res_init_fail.status_code == 403

        # 3. Key with system:monitor only
        res_monitor_only = await client.post(
            "/api/v1/studio/keys",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"name": "Monitor Only Key", "scopes": ["system:monitor"]}
        )
        key_monitor_only = res_monitor_only.json()["raw_key"]

        # CAN inspect worker health
        res_worker_ok = await client.get(
            "/api/v1/health/worker",
            headers={"Authorization": f"Bearer {key_monitor_only}"}
        )
        assert res_worker_ok.status_code == 200

        # But CANNOT init package
        res_init_monitor = await client.post(
            "/api/v1/studio/packages/init",
            headers={"Authorization": f"Bearer {key_monitor_only}"},
            json=dummy_manifest
        )
        assert res_init_monitor.status_code == 403

        # 4. Key with aggregate studio:write
        res_studio = await client.post(
            "/api/v1/studio/keys",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"name": "Studio Write Key", "scopes": ["studio:write"]}
        )
        key_studio = res_studio.json()["raw_key"]

        # Can init package
        res_studio_init = await client.post(
            "/api/v1/studio/packages/init",
            headers={"Authorization": f"Bearer {key_studio}"},
            json=dummy_manifest
        )
        assert res_studio_init.status_code == 201

        # But studio:write does NOT grant system:monitor
        res_studio_worker = await client.get(
            "/api/v1/health/worker",
            headers={"Authorization": f"Bearer {key_studio}"}
        )
        assert res_studio_worker.status_code == 403


@pytest.mark.asyncio
async def test_package_session_ownership_cross_key_idor_rejection(app, admin_token):
    """
    IDOR Prevention Regression Test:
    Proves that Key B cannot inspect, list files, upload into, finalize, or delete Key A's package session.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        # Create Key A with full studio scopes
        res_a = await client.post(
            "/api/v1/studio/keys",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"name": "Studio Key A", "scopes": ["*"]}
        )
        key_a = res_a.json()["raw_key"]

        # Create Key B with full studio scopes
        res_b = await client.post(
            "/api/v1/studio/keys",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"name": "Studio Key B", "scopes": ["*"]}
        )
        key_b = res_b.json()["raw_key"]

        # Manifest with 1 file
        content = b"#EXTM3U\n#EXT-X-VERSION:3\n"
        manifest = {
            "manifest_version": "1.0",
            "video": {"title": "IDOR Test Video", "duration_seconds": 10.0},
            "renditions": [
                {
                    "quality_label": "360p",
                    "width": 640,
                    "height": 360,
                    "video_bitrate_bps": 800000,
                    "playlist_path": "360p.m3u8",
                    "segment_count": 1,
                }
            ],
            "subtitles": [],
            "files": [
                {
                    "path": "360p.m3u8",
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "media_type": "application/x-mpegURL",
                }
            ],
        }

        # Key A initializes a package session
        res_init = await client.post(
            "/api/v1/studio/packages/init",
            headers={"Authorization": f"Bearer {key_a}"},
            json={"manifest": manifest}
        )
        assert res_init.status_code == 201
        session_id = res_init.json()["session_id"]
        file_id = res_init.json()["files"][0]["file_id"]

        # 1. Key B attempts GET /{session_id} -> 403
        res_get = await client.get(
            f"/api/v1/studio/packages/{session_id}",
            headers={"Authorization": f"Bearer {key_b}"}
        )
        assert res_get.status_code == 403

        # 2. Key B attempts GET /{session_id}/files -> 403
        res_files = await client.get(
            f"/api/v1/studio/packages/{session_id}/files",
            headers={"Authorization": f"Bearer {key_b}"}
        )
        assert res_files.status_code == 403

        # 3. Key B attempts PUT /{session_id}/files/{file_id} -> 403
        res_put = await client.put(
            f"/api/v1/studio/packages/{session_id}/files/{file_id}",
            headers={"Authorization": f"Bearer {key_b}", "Content-Type": "application/octet-stream"},
            content=content
        )
        assert res_put.status_code == 403

        # 4. Key B attempts POST /{session_id}/finalize -> 403
        res_fin = await client.post(
            f"/api/v1/studio/packages/{session_id}/finalize",
            headers={"Authorization": f"Bearer {key_b}"}
        )
        assert res_fin.status_code == 403

        # 5. Key B attempts DELETE /{session_id} -> 403
        res_del = await client.delete(
            f"/api/v1/studio/packages/{session_id}",
            headers={"Authorization": f"Bearer {key_b}"}
        )
        assert res_del.status_code == 403

        # 6. Key A CAN access and cancel their own session
        res_get_a = await client.get(
            f"/api/v1/studio/packages/{session_id}",
            headers={"Authorization": f"Bearer {key_a}"}
        )
        assert res_get_a.status_code == 200

        res_del_a = await client.delete(
            f"/api/v1/studio/packages/{session_id}",
            headers={"Authorization": f"Bearer {key_a}"}
        )
        assert res_del_a.status_code == 200


@pytest.mark.asyncio
async def test_package_session_init_cross_user_video_authorization(app, admin_token):
    """
    Video Authorization Invariant:
    A user cannot attach a package upload session to another user's video without administrative privileges.
    """
    # Create two regular users
    async with get_db_context() as db:
        u1 = User(
            username=f"user_pkg_{uuid.uuid4().hex[:6]}",
            email=f"u1_{uuid.uuid4().hex[:6]}@test.com",
            hashed_password=hash_password("Pass123!"),
            role=UserRole.USER,
            is_active=True,
        )
        u2 = User(
            username=f"user_pkg_{uuid.uuid4().hex[:6]}",
            email=f"u2_{uuid.uuid4().hex[:6]}@test.com",
            hashed_password=hash_password("Pass123!"),
            role=UserRole.USER,
            is_active=True,
        )
        db.add_all([u1, u2])
        await db.commit()
        await db.refresh(u1)
        await db.refresh(u2)

        # Video owned by u2
        v2 = Video(
            user_id=u2.id,
            title="User 2 Video",
            source_filename="test2.mp4",
            status=VideoStatus.READY.value,
        )
        db.add(v2)
        await db.commit()
        await db.refresh(v2)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        # Create an API key belonging to u1
        # Seeding session for u1
        token_u1 = secrets.token_urlsafe(32)
        async with get_db_context() as db:
            s_u1 = UserSession(
                user_id=u1.id,
                token_hash=hash_token(token_u1),
                expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            )
            db.add(s_u1)
            await db.commit()

        from app.security.api_key import generate_raw_api_key, hash_api_key
        raw_key_u1 = generate_raw_api_key()
        async with get_db_context() as db:
            k1 = ApiKey(
                user_id=u1.id,
                name="U1 Key",
                prefix=raw_key_u1[:12],
                key_hash=hash_api_key(raw_key_u1),
                scopes=["*"],
                is_active=True,
            )
            db.add(k1)
            await db.commit()

        content = b"#EXTM3U\n#EXT-X-VERSION:3\n"
        manifest = {
            "manifest_version": "1.0",
            "video": {"title": "Target User 2 Video", "duration_seconds": 10.0},
            "renditions": [
                {
                    "quality_label": "360p",
                    "width": 640,
                    "height": 360,
                    "video_bitrate_bps": 800000,
                    "playlist_path": "360p.m3u8",
                    "segment_count": 1,
                }
            ],
            "subtitles": [],
            "files": [
                {
                    "path": "360p.m3u8",
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "media_type": "application/x-mpegURL",
                }
            ],
        }

        # u1's API key attempts to init package on u2's video
        res = await client.post(
            "/api/v1/studio/packages/init",
            headers={"Authorization": f"Bearer {raw_key_u1}"},
            json={"video_id": str(v2.id), "manifest": manifest}
        )
        assert res.status_code == 403
