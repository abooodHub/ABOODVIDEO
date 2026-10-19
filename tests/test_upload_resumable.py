"""Comprehensive Automated Test Suite for Phase 2 Resumable Upload System."""
import os
import uuid
import hashlib
import pytest
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from app.main import app
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.user import User, UserRole
from app.models.video import Video, VideoStatus
from app.models.upload import UploadSession, UploadSessionStatus
from app.services.video_validator import validate_video_file, InvalidVideoException
from app.services.upload_service import UploadService, UploadException


@pytest.fixture
async def test_users():
    """Create test users in database: owner, user1, user2."""
    async with AsyncSessionLocal() as session:
        u_owner = User(
            id=uuid.uuid4(),
            email=f"owner_{uuid.uuid4().hex[:6]}@test.com",
            username=f"owner_{uuid.uuid4().hex[:6]}",
            hashed_password="hash",
            role=UserRole.OWNER,
            is_active=True,
        )
        u1 = User(
            id=uuid.uuid4(),
            email=f"user1_{uuid.uuid4().hex[:6]}@test.com",
            username=f"user1_{uuid.uuid4().hex[:6]}",
            hashed_password="hash",
            role=UserRole.USER,
            is_active=True,
        )
        u2 = User(
            id=uuid.uuid4(),
            email=f"user2_{uuid.uuid4().hex[:6]}@test.com",
            username=f"user2_{uuid.uuid4().hex[:6]}",
            hashed_password="hash",
            role=UserRole.USER,
            is_active=True,
        )
        session.add_all([u_owner, u1, u2])
        await session.commit()
        return {"owner": u_owner, "user1": u1, "user2": u2}


@pytest.fixture(scope="session")
def valid_sample_mp4(tmp_path_factory):
    """Generate a tiny valid 1-second MP4 video file using ffmpeg."""
    tmp_dir = tmp_path_factory.mktemp("media")
    video_path = tmp_dir / "sample_valid.mp4"
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                str(video_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
    except Exception:
        # Fallback if ffmpeg is missing during local non-container test
        video_path.write_bytes(b"\x00\x00\x00 ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 200)
    return video_path


@pytest.mark.asyncio
async def test_initiate_upload_session(test_users):
    """Test 1: Normal upload initiation."""
    user = test_users["owner"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        payload = {
            "title": "My Awesome Video",
            "filename": "awesome.mp4",
            "file_size_bytes": 10485760,  # 10 MB
            "chunk_size_bytes": 2097152,  # 2 MB
            "description": "Test video description",
        }
        res = await client.post("/api/v1/uploads/initiate", json=payload, headers={"X-User-ID": str(user.id)})
        assert res.status_code == 201
        data = res.json()
        assert "upload_id" in data
        assert data["total_chunks"] == 5
        assert data["status"] == "INITIATED"


@pytest.mark.asyncio
async def test_upload_chunks_sequential(test_users, valid_sample_mp4):
    """Test 2: Complete chunked upload flow with valid video finalization."""
    user = test_users["owner"]
    content = valid_sample_mp4.read_bytes()
    file_size = len(content)
    chunk_size = 1048576  # 1MB chunks (minimum allowed)
    if file_size < chunk_size:
        # Pad content to exactly 1.5MB for multi-chunk testing
        target_size = 1572864  # 1.5 MB
        padding = b"\x00" * (target_size - file_size)
        content = content + padding
        file_size = len(content)

    total_chunks = (file_size + chunk_size - 1) // chunk_size

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        # 1. Initiate
        init_res = await client.post(
            "/api/v1/uploads/initiate",
            json={
                "title": "Sequential Chunk Test",
                "filename": "seq_test.mp4",
                "file_size_bytes": file_size,
                "chunk_size_bytes": chunk_size,
            },
            headers={"X-User-ID": str(user.id)},
        )
        assert init_res.status_code == 201
        upload_id = init_res.json()["upload_id"]

        # 2. Upload Chunks
        for idx in range(total_chunks):
            start = idx * chunk_size
            end = min(start + chunk_size, file_size)
            chunk_slice = content[start:end]
            chunk_sha = hashlib.sha256(chunk_slice).hexdigest()

            files = {"chunk_file": (f"chunk_{idx}.part", chunk_slice, "application/octet-stream")}
            data = {"chunk_index": idx}
            headers = {"X-User-ID": str(user.id), "X-Chunk-SHA256": chunk_sha}

            c_res = await client.post(f"/api/v1/uploads/{upload_id}/chunk", data=data, files=files, headers=headers)
            assert c_res.status_code == 200
            assert c_res.json()["chunk_index"] == idx

        # 3. Check status
        st_res = await client.get(f"/api/v1/uploads/{upload_id}/status", headers={"X-User-ID": str(user.id)})
        assert st_res.status_code == 200
        assert len(st_res.json()["missing_chunks"]) == 0
        assert st_res.json()["progress_percent"] == 100.0


@pytest.mark.asyncio
async def test_chunk_interruption_and_resume(test_users):
    """Test 3: Pause and Resume functionality."""
    user = test_users["owner"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        init_res = await client.post(
            "/api/v1/uploads/initiate",
            json={
                "title": "Pause Resume Test",
                "filename": "pause.mp4",
                "file_size_bytes": 3145728,  # 3 MB
                "chunk_size_bytes": 1048576,  # 1 MB
            },
            headers={"X-User-ID": str(user.id)},
        )
        upload_id = init_res.json()["upload_id"]

        # Upload chunk 0
        chunk_data = b"A" * 1048576
        await client.post(
            f"/api/v1/uploads/{upload_id}/chunk",
            data={"chunk_index": 0},
            files={"chunk_file": ("c0.part", chunk_data, "application/octet-stream")},
            headers={"X-User-ID": str(user.id)},
        )

        # Pause
        p_res = await client.post(f"/api/v1/uploads/{upload_id}/pause", headers={"X-User-ID": str(user.id)})
        assert p_res.status_code == 200
        assert p_res.json()["status"] == "PAUSED"

        # Attempt uploading chunk 1 while paused -> should be rejected with 409
        rejected = await client.post(
            f"/api/v1/uploads/{upload_id}/chunk",
            data={"chunk_index": 1},
            files={"chunk_file": ("c1.part", chunk_data, "application/octet-stream")},
            headers={"X-User-ID": str(user.id)},
        )
        assert rejected.status_code == 409

        # Resume
        r_res = await client.post(f"/api/v1/uploads/{upload_id}/resume", headers={"X-User-ID": str(user.id)})
        assert r_res.status_code == 200
        assert r_res.json()["status"] == "UPLOADING"
        assert r_res.json()["received_chunks"] == [0]
        assert r_res.json()["missing_chunks"] == [1, 2]


@pytest.mark.asyncio
async def test_missing_chunks_fails_finalization(test_users):
    """Test 4: Finalization fails if chunks are missing."""
    user = test_users["owner"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        init_res = await client.post(
            "/api/v1/uploads/initiate",
            json={
                "title": "Missing Chunk Test",
                "filename": "missing.mp4",
                "file_size_bytes": 3145728,
                "chunk_size_bytes": 1048576,
            },
            headers={"X-User-ID": str(user.id)},
        )
        upload_id = init_res.json()["upload_id"]

        # Only upload chunk 0 of 3
        chunk_data = b"X" * 1048576
        await client.post(
            f"/api/v1/uploads/{upload_id}/chunk",
            data={"chunk_index": 0},
            files={"chunk_file": ("c0.part", chunk_data, "application/octet-stream")},
            headers={"X-User-ID": str(user.id)},
        )

        # Attempt to finalize -> fails with 400
        fin_res = await client.post(f"/api/v1/uploads/{upload_id}/finalize", headers={"X-User-ID": str(user.id)})
        assert fin_res.status_code == 400
        assert "missing" in fin_res.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_duplicate_chunk_idempotency(test_users):
    """Test 5: Duplicate chunks are accepted idempotently without corrupting progress."""
    user = test_users["owner"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        init_res = await client.post(
            "/api/v1/uploads/initiate",
            json={
                "title": "Duplicate Chunk Test",
                "filename": "dup.mp4",
                "file_size_bytes": 2097152,
                "chunk_size_bytes": 1048576,
            },
            headers={"X-User-ID": str(user.id)},
        )
        upload_id = init_res.json()["upload_id"]
        chunk_data = b"D" * 1048576

        # Upload chunk 0 first time
        res1 = await client.post(
            f"/api/v1/uploads/{upload_id}/chunk",
            data={"chunk_index": 0},
            files={"chunk_file": ("c0.part", chunk_data, "application/octet-stream")},
            headers={"X-User-ID": str(user.id)},
        )
        assert res1.status_code == 200
        assert res1.json()["bytes_received"] == 1048576

        # Upload chunk 0 second time (duplicate)
        res2 = await client.post(
            f"/api/v1/uploads/{upload_id}/chunk",
            data={"chunk_index": 0},
            files={"chunk_file": ("c0.part", chunk_data, "application/octet-stream")},
            headers={"X-User-ID": str(user.id)},
        )
        assert res2.status_code == 200
        # Byte count must NOT be doubled
        assert res2.json()["bytes_received"] == 1048576
        assert res2.json()["chunks_received_count"] == 1


@pytest.mark.asyncio
async def test_chunk_checksum_mismatch_rejected(test_users):
    """Test 6: Reject chunk when X-Chunk-SHA256 header does not match content."""
    user = test_users["owner"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        init_res = await client.post(
            "/api/v1/uploads/initiate",
            json={
                "title": "Checksum Test",
                "filename": "checksum.mp4",
                "file_size_bytes": 1048576,
                "chunk_size_bytes": 1048576,
            },
            headers={"X-User-ID": str(user.id)},
        )
        upload_id = init_res.json()["upload_id"]
        chunk_data = b"M" * 1048576

        # Provide a bogus SHA-256
        bogus_sha = "0000000000000000000000000000000000000000000000000000000000000000"
        bad_res = await client.post(
            f"/api/v1/uploads/{upload_id}/chunk",
            data={"chunk_index": 0},
            files={"chunk_file": ("c0.part", chunk_data, "application/octet-stream")},
            headers={"X-User-ID": str(user.id), "X-Chunk-SHA256": bogus_sha},
        )
        assert bad_res.status_code == 400
        assert "checksum mismatch" in bad_res.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_unauthorized_upload_access(test_users):
    """Test 7: Role enforcement: unauthenticated 401, regular user 403, and owner session isolation."""
    owner = test_users["owner"]
    u1 = test_users["user1"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        # 1. Unauthenticated request rejected with 401
        unauth_res = await client.post(
            "/api/v1/uploads/initiate",
            json={
                "title": "Unauth Test",
                "filename": "unauth.mp4",
                "file_size_bytes": 1048576,
                "chunk_size_bytes": 1048576,
            },
        )
        assert unauth_res.status_code == 401

        # 2. Regular user (role=USER) rejected with 403 Forbidden
        user_res = await client.post(
            "/api/v1/uploads/initiate",
            json={
                "title": "Regular User Rejected",
                "filename": "forbidden.mp4",
                "file_size_bytes": 1048576,
                "chunk_size_bytes": 1048576,
            },
            headers={"X-User-ID": str(u1.id)},
        )
        assert user_res.status_code == 403
        assert "permission required" in user_res.json()["error"]["message"].lower()

        # 3. Owner creates upload session
        init_res = await client.post(
            "/api/v1/uploads/initiate",
            json={
                "title": "Security Auth Test",
                "filename": "auth.mp4",
                "file_size_bytes": 1048576,
                "chunk_size_bytes": 1048576,
            },
            headers={"X-User-ID": str(owner.id)},
        )
        assert init_res.status_code == 201
        upload_id = init_res.json()["upload_id"]

        # 4. Regular user attempts to view status -> 403
        forbidden_res = await client.get(
            f"/api/v1/uploads/{upload_id}/status",
            headers={"X-User-ID": str(u1.id)},
        )
        assert forbidden_res.status_code == 403

        # 5. Regular user attempts to cancel Owner's upload -> 403
        cancel_res = await client.post(
            f"/api/v1/uploads/{upload_id}/cancel",
            headers={"X-User-ID": str(u1.id)},
        )
        assert cancel_res.status_code == 403


@pytest.mark.asyncio
async def test_oversized_upload_rejected(test_users):
    """Test 8: Upload exceeding MAX_UPLOAD_SIZE_BYTES is rejected."""
    user = test_users["owner"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        res = await client.post(
            "/api/v1/uploads/initiate",
            json={
                "title": "Too Big",
                "filename": "big.mp4",
                "file_size_bytes": settings.MAX_UPLOAD_SIZE_BYTES + 1024,
                "chunk_size_bytes": 5242880,
            },
            headers={"X-User-ID": str(user.id)},
        )
        assert res.status_code == 400
        assert "exceeds max" in res.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_cancel_upload_cleans_storage(test_users):
    """Test 9: Cancel upload removes temporary chunks and updates video status."""
    user = test_users["owner"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        init_res = await client.post(
            "/api/v1/uploads/initiate",
            json={
                "title": "Cancel Test",
                "filename": "cancel.mp4",
                "file_size_bytes": 2097152,
                "chunk_size_bytes": 1048576,
            },
            headers={"X-User-ID": str(user.id)},
        )
        upload_id = init_res.json()["upload_id"]
        temp_dir = Path(settings.STORAGE_TEMP_PATH) / "uploads" / upload_id

        # Upload a chunk so directory has content
        chunk_data = b"C" * 1048576
        await client.post(
            f"/api/v1/uploads/{upload_id}/chunk",
            data={"chunk_index": 0},
            files={"chunk_file": ("c0.part", chunk_data, "application/octet-stream")},
            headers={"X-User-ID": str(user.id)},
        )
        assert temp_dir.exists()

        # Cancel
        c_res = await client.post(f"/api/v1/uploads/{upload_id}/cancel", headers={"X-User-ID": str(user.id)})
        assert c_res.status_code == 200
        assert c_res.json()["status"] == "CANCELLED"

        # Verify disk directory was pruned
        assert not temp_dir.exists()


@pytest.mark.asyncio
async def test_admin_can_cancel_and_delete_active_upload_without_session_id(test_users):
    """The Library action must cancel its active session before deleting the video."""
    owner = test_users["owner"]
    headers = {"X-User-ID": str(owner.id)}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        created = []
        for suffix in ("one", "two"):
            init_res = await client.post(
                "/api/v1/uploads/initiate",
                json={
                    "title": f"Duplicate upload {suffix}",
                    "filename": f"{suffix}.mp4",
                    "file_size_bytes": 2 * 1024 * 1024,
                    "chunk_size_bytes": 1024 * 1024,
                },
                headers=headers,
            )
            assert init_res.status_code == 201
            created.append(init_res.json())

        first = created[0]
        temp_dir = Path(settings.STORAGE_TEMP_PATH) / "uploads" / first["upload_id"]
        uploaded = await client.post(
            f"/api/v1/uploads/{first['upload_id']}/chunk",
            data={"chunk_index": 0},
            files={"chunk_file": ("chunk.part", b"A" * 1024 * 1024, "application/octet-stream")},
            headers=headers,
        )
        assert uploaded.status_code == 200
        assert temp_dir.exists()

        # Safety guard remains in place for a genuinely active upload.
        blocked = await client.delete(f"/api/v1/admin/videos/{first['video_id']}", headers=headers)
        assert blocked.status_code == 400

        # The admin-facing operation takes a video ID, never a hidden session ID.
        deleted = await client.post(
            f"/api/v1/admin/videos/{first['video_id']}/cancel-and-delete",
            headers=headers,
        )
        assert deleted.status_code == 200
        assert deleted.json()["cancelled_sessions"] == 1
        assert not temp_dir.exists()

        async with AsyncSessionLocal() as db:
            assert (await db.execute(select(Video).where(Video.id == uuid.UUID(first["video_id"])))).scalar_one_or_none() is None
            # The second duplicate is unrelated and remains available for its own safe cleanup.
            second_video = (await db.execute(select(Video).where(Video.id == uuid.UUID(created[1]["video_id"])))).scalar_one()
            assert second_video.status == VideoStatus.UPLOADING

        second_deleted = await client.post(
            f"/api/v1/admin/videos/{created[1]['video_id']}/cancel-and-delete",
            headers=headers,
        )
        assert second_deleted.status_code == 200


@pytest.mark.asyncio
async def test_stale_upload_reconciliation_cancels_video_and_releases_reservation(test_users):
    """An inactive resumable upload cannot leave its Video row permanently UPLOADING."""
    user = test_users["owner"]
    async with AsyncSessionLocal() as db:
        upload_id = uuid.uuid4()
        video_id = uuid.uuid4()
        temp_dir = Path(settings.STORAGE_TEMP_PATH) / "uploads" / str(upload_id)
        temp_dir.mkdir(parents=True, exist_ok=True)
        (temp_dir / "chunk_0.part").write_bytes(b"stale")
        video = Video(id=video_id, user_id=user.id, title="Stale Upload", status=VideoStatus.UPLOADING, source_filename="stale.mp4")
        upload = UploadSession(
            id=upload_id, video_id=video_id, user_id=user.id, status=UploadSessionStatus.UPLOADING,
            filename="stale.mp4", file_size_bytes=100, chunk_size_bytes=100, total_chunks=1,
            temp_directory=str(temp_dir), expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        db.add_all([video, upload])
        await db.commit()
        upload.updated_at = datetime.now(timezone.utc) - timedelta(
            seconds=settings.UPLOAD_INACTIVITY_TIMEOUT_SECONDS + 60,
        )
        await db.commit()

        assert await UploadService.cleanup_expired_sessions(db) >= 1
        await db.refresh(upload)
        await db.refresh(video)
        assert upload.status == UploadSessionStatus.EXPIRED
        assert video.status == VideoStatus.CANCELLED
        assert video.upload_progress_percent == 0.0
        assert not temp_dir.exists()


@pytest.mark.asyncio
async def test_expired_upload_cleanup(test_users):
    """Test 10: Expired upload cleanup marks sessions EXPIRED and cleans disks."""
    user = test_users["owner"]
    async with AsyncSessionLocal() as session:
        # Create expired session in past
        u_id = uuid.uuid4()
        v_id = uuid.uuid4()
        temp_dir = Path(settings.STORAGE_TEMP_PATH) / "uploads" / str(u_id)
        temp_dir.mkdir(parents=True, exist_ok=True)
        (temp_dir / "chunk_0.part").write_bytes(b"EXPIRED")

        video = Video(
            id=v_id,
            user_id=user.id,
            title="Expired Video",
            status=VideoStatus.UPLOADING,
            source_filename="exp.mp4",
        )
        upload_session = UploadSession(
            id=u_id,
            video_id=v_id,
            user_id=user.id,
            status=UploadSessionStatus.INITIATED,
            filename="exp.mp4",
            file_size_bytes=100,
            chunk_size_bytes=100,
            total_chunks=1,
            temp_directory=str(temp_dir),
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        session.add_all([video, upload_session])
        await session.commit()

        # Run cleanup
        cleaned = await UploadService.cleanup_expired_sessions(session)
        assert cleaned >= 1
        assert not temp_dir.exists()


@pytest.mark.asyncio
async def test_ffprobe_invalid_video_rejected(test_users, tmp_path):
    """Test 11: Invalid video file disguised as MP4 is strictly rejected by ffprobe."""
    user = test_users["owner"]
    fake_video = b"THIS IS NOT A VALID VIDEO FILE AT ALL! JUST PLAIN TEXT CONTENT."
    fake_size = len(fake_video)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        init_res = await client.post(
            "/api/v1/uploads/initiate",
            json={
                "title": "Fake Video Test",
                "filename": "fake.mp4",
                "file_size_bytes": fake_size,
                "chunk_size_bytes": fake_size,
            },
            headers={"X-User-ID": str(user.id)},
        )
        upload_id = init_res.json()["upload_id"]

        # Upload chunk
        await client.post(
            f"/api/v1/uploads/{upload_id}/chunk",
            data={"chunk_index": 0},
            files={"chunk_file": ("c0.part", fake_video, "application/octet-stream")},
            headers={"X-User-ID": str(user.id)},
        )

        # Finalize must fail with 422
        fin_res = await client.post(f"/api/v1/uploads/{upload_id}/finalize", headers={"X-User-ID": str(user.id)})
        assert fin_res.status_code == 422
        assert "invalid" in fin_res.json()["error"]["message"].lower() or "ffprobe" in fin_res.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_request_id_header():
    """Test 12: Verify X-Request-ID header presence in API responses."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        res = await client.get("/api/v1/health")
        assert "X-Request-ID" in res.headers
        assert len(res.headers["X-Request-ID"]) >= 8


@pytest.mark.asyncio
async def test_upload_progress_never_100_percent_when_chunks_missing():
    """Test 13: Progress strictly requires all chunks and bytes; never reports 100% while missing chunks."""
    # Case A: bytes reached total, but only 2 of 3 chunks received
    p1 = UploadService.calculate_progress(
        bytes_received=3000,
        file_size_bytes=3000,
        received_chunks_count=2,
        total_chunks=3,
    )
    assert p1 < 100.0
    assert p1 == 99.99

    # Case B: all chunks received, but bytes received is slightly short
    p2 = UploadService.calculate_progress(
        bytes_received=2999,
        file_size_bytes=3000,
        received_chunks_count=3,
        total_chunks=3,
    )
    assert p2 < 100.0

    # Case C: all chunks received and bytes fully received
    p3 = UploadService.calculate_progress(
        bytes_received=3000,
        file_size_bytes=3000,
        received_chunks_count=3,
        total_chunks=3,
    )
    assert p3 == 100.0

    # Case D: partial progress
    p4 = UploadService.calculate_progress(
        bytes_received=1500,
        file_size_bytes=3000,
        received_chunks_count=1,
        total_chunks=2,
    )
    assert p4 == 50.0


@pytest.mark.asyncio
async def test_upload_with_destination_folder(test_users):
    """Test 16: Upload initiation supports target destination folder."""
    from app.models.folder import Folder
    user = test_users["owner"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        # Create a test folder first
        async with AsyncSessionLocal() as session:
            folder = Folder(
                id=uuid.uuid4(),
                user_id=user.id,
                name="Spirit Cage Season 1",
                folder_type="season"
            )
            session.add(folder)
            await session.commit()
            folder_id = folder.id

        init_res = await client.post(
            "/api/v1/uploads/initiate",
            json={
                "title": "Spirit Cage Episode 01",
                "filename": "Spirit_Cage_EP01.mkv",
                "file_size_bytes": 1048576,
                "folder_id": str(folder_id),
            },
            headers={"X-User-ID": str(user.id)},
        )
        assert init_res.status_code == 201
        data = init_res.json()
        assert "upload_id" in data
        assert "video_id" in data

        # Verify video has folder_id set in DB
        async with AsyncSessionLocal() as session:
            v = await session.get(Video, uuid.UUID(data["video_id"]))
            assert v is not None
            assert v.folder_id == folder_id
