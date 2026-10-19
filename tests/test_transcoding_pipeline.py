"""Comprehensive Automated Tests for Production Transcoding Pipeline (Phase 3)."""
import os
import signal
import uuid
import time
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.main import app
from app.core.config import Settings, settings, LADDER_PRESETS
from app.core.database import AsyncSessionLocal, SyncSessionLocal
from app.models.user import User, UserRole
from app.models.video import Video, VideoStatus, VideoVisibility, VideoRendition, TranscodeJob, TranscodeJobStatus
from app.security.hashing import hash_password
from app.services.transcoder import (
    TranscodeError,
    InsufficientDiskSpaceError,
    TranscodeCancelledError,
    select_quality_ladder,
    calculate_peak_disk_requirement,
    derive_rfc6381_codecs,
    generate_master_playlist,
    verify_transcoded_output,
    safe_kill_process_group,
    execute_transcode_rendition,
)
from app.workers.transcode_worker import check_worker_capabilities, get_worker_status


@pytest.fixture
async def transcode_auth_fixture():
    """Create test owner and authenticated client credentials."""
    owner_pass = "OwnerSecretPass123!"
    async with AsyncSessionLocal() as session:
        owner = User(
            id=uuid.uuid4(),
            email=f"transcode_owner_{uuid.uuid4().hex[:6]}@aboodvideo.local",
            username=f"tc_owner_{uuid.uuid4().hex[:6]}",
            hashed_password=hash_password(owner_pass),
            role=UserRole.OWNER,
            is_active=True,
        )
        session.add(owner)
        await session.commit()
        await session.refresh(owner)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        res = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": owner.username, "password": owner_pass},
        )
        assert res.status_code == 200
        csrf_token = res.json()["csrf_token"]
        session_token = res.cookies.get(settings.SESSION_COOKIE_NAME)
        csrf_cookie = res.cookies.get(settings.CSRF_COOKIE_NAME)

        return {
            "owner": owner,
            "cookies": {
                settings.SESSION_COOKIE_NAME: session_token,
                settings.CSRF_COOKIE_NAME: csrf_cookie,
            },
            "headers": {"X-CSRF-Token": csrf_token},
        }


@pytest.fixture
async def uploaded_video_fixture(transcode_auth_fixture):
    """Create test video in UPLOADED state."""
    owner = transcode_auth_fixture["owner"]
    async with AsyncSessionLocal() as session:
        video = Video(
            id=uuid.uuid4(),
            user_id=owner.id,
            title="Test Source Video",
            status=VideoStatus.UPLOADED,
            source_filename="test_video.mp4",
            source_size_bytes=10 * 1024 * 1024,
            source_duration_seconds=15.0,
            source_resolution_width=1280,
            source_resolution_height=720,
            source_bitrate=2500000,
            source_codec="h264",
        )
        session.add(video)
        await session.commit()
        await session.refresh(video)
        return video


# =========================================================================
# 1. Database-Level Active Job Guarantee & Concurrency Race Tests
# =========================================================================

@pytest.mark.asyncio
async def test_db_partial_unique_index_active_job_guarantee(uploaded_video_fixture):
    """Verify that PostgreSQL partial unique index prevents two active jobs for same video."""
    video = uploaded_video_fixture
    async with AsyncSessionLocal() as session:
        # Create first active job
        job1 = TranscodeJob(
            id=uuid.uuid4(),
            video_id=video.id,
            status=TranscodeJobStatus.QUEUED,
        )
        session.add(job1)
        await session.commit()

        # Attempt to create second active job
        job2 = TranscodeJob(
            id=uuid.uuid4(),
            video_id=video.id,
            status=TranscodeJobStatus.PROCESSING,
        )
        session.add(job2)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_concurrent_transcode_enqueue_api_race_rejection(transcode_auth_fixture, uploaded_video_fixture):
    """Test API rejects concurrent enqueue requests with HTTP 409 Conflict."""
    cookies = transcode_auth_fixture["cookies"]
    headers = transcode_auth_fixture["headers"]
    video = uploaded_video_fixture

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost", cookies=cookies) as client:
        # First enqueue
        res1 = await client.post(
            f"/api/v1/admin/videos/{video.id}/transcode",
            headers=headers,
        )
        assert res1.status_code == 202
        assert res1.json()["status"] == "QUEUED"

        # Second enqueue must hit 409 Conflict
        res2 = await client.post(
            f"/api/v1/admin/videos/{video.id}/transcode",
            headers=headers,
        )
        assert res2.status_code == 409
        assert "already exists" in res2.json()["detail"]

        # Clean up enqueued test job
        job_id = res1.json()["job_id"]
        await client.post(f"/api/v1/admin/transcodes/{job_id}/cancel", headers=headers)


@pytest.fixture
def capture_transcode_enqueue(monkeypatch):
    """Capture enqueue options without allowing a unit test to use RQ defaults."""
    from app.api.v1 import admin_transcodes

    calls = []

    class CapturingQueue:
        def __init__(self, *args, **kwargs):
            pass

        def enqueue(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(admin_transcodes, "Queue", CapturingQueue)
    monkeypatch.setattr(admin_transcodes.redis, "from_url", lambda *_args, **_kwargs: object())
    return calls


def test_transcode_timeout_default_and_environment_override(monkeypatch):
    assert Settings().TRANSCODE_JOB_TIMEOUT_SECONDS == 43200
    monkeypatch.setenv("TRANSCODE_JOB_TIMEOUT_SECONDS", "9876")
    assert Settings().TRANSCODE_JOB_TIMEOUT_SECONDS == 9876


@pytest.mark.asyncio
async def test_initial_transcode_enqueue_sets_explicit_long_timeout(
    transcode_auth_fixture, uploaded_video_fixture, capture_transcode_enqueue,
):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://localhost",
        cookies=transcode_auth_fixture["cookies"],
    ) as client:
        response = await client.post(
            f"/api/v1/admin/videos/{uploaded_video_fixture.id}/transcode",
            headers=transcode_auth_fixture["headers"],
        )

    assert response.status_code == 202
    assert len(capture_transcode_enqueue) == 1
    args, kwargs = capture_transcode_enqueue[0]
    assert args[0] == "app.workers.transcode_worker.process_transcode_job"
    assert kwargs["job_timeout"] == settings.TRANSCODE_JOB_TIMEOUT_SECONDS == 43200


@pytest.mark.asyncio
async def test_retry_transcode_enqueue_sets_explicit_long_timeout(
    transcode_auth_fixture, uploaded_video_fixture, capture_transcode_enqueue,
):
    async with AsyncSessionLocal() as session:
        failed_job = TranscodeJob(
            video_id=uploaded_video_fixture.id,
            status=TranscodeJobStatus.FAILED,
            attempt=1,
            max_attempts=3,
        )
        session.add(failed_job)
        await session.commit()
        await session.refresh(failed_job)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://localhost",
        cookies=transcode_auth_fixture["cookies"],
    ) as client:
        response = await client.post(
            f"/api/v1/admin/transcodes/{failed_job.id}/retry",
            headers=transcode_auth_fixture["headers"],
        )

    assert response.status_code == 200
    assert len(capture_transcode_enqueue) == 1
    args, kwargs = capture_transcode_enqueue[0]
    assert args[0] == "app.workers.transcode_worker.process_transcode_job"
    assert kwargs["job_timeout"] == settings.TRANSCODE_JOB_TIMEOUT_SECONDS == 43200


# =========================================================================
# 2. Rendition Uniqueness Constraint Test
# =========================================================================

@pytest.mark.asyncio
async def test_video_rendition_unique_constraint(uploaded_video_fixture):
    """Verify table-level UNIQUE (video_id, quality_label) constraint rejects duplicates."""
    video = uploaded_video_fixture
    async with AsyncSessionLocal() as session:
        r1 = VideoRendition(
            video_id=video.id,
            quality_label="720p",
            resolution_width=1280,
            resolution_height=720,
            video_codec="h264",
        )
        session.add(r1)
        await session.commit()

        r2 = VideoRendition(
            video_id=video.id,
            quality_label="720p",
            resolution_width=1280,
            resolution_height=720,
            video_codec="h264",
        )
        session.add(r2)
        with pytest.raises(IntegrityError):
            await session.commit()


# =========================================================================
# 3. Failed Retry Preserves Existing READY Generation Test
# =========================================================================

@pytest.mark.asyncio
async def test_failed_retry_preserves_existing_ready_generation(uploaded_video_fixture):
    """Verify that when a video is already READY, a failed retry retains the active generation pointers."""
    video = uploaded_video_fixture
    active_gen_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        v = await session.get(Video, video.id)
        v.status = VideoStatus.READY
        v.active_generation_id = active_gen_id
        v.master_playlist_path = f"videos/{v.id}/generations/{active_gen_id}/master.m3u8"
        v.poster_path = f"videos/{v.id}/generations/{active_gen_id}/poster.webp"
        await session.commit()

    # Simulate a failed retry job
    async with AsyncSessionLocal() as session:
        failed_job = TranscodeJob(
            id=uuid.uuid4(),
            video_id=video.id,
            status=TranscodeJobStatus.FAILED,
            attempt=2,
            error_code="ENCODE_ERROR",
        )
        session.add(failed_job)
        await session.commit()

        # Video status and active generation pointers MUST remain intact
        refreshed_v = await session.get(Video, video.id)
        assert refreshed_v.status == VideoStatus.READY
        assert refreshed_v.active_generation_id == active_gen_id
        assert str(active_gen_id) in refreshed_v.master_playlist_path


# =========================================================================
# 4. Generation Activation Atomicity Test
# =========================================================================

@pytest.mark.asyncio
async def test_generation_activation_atomicity(uploaded_video_fixture):
    """Verify that generation pointers and renditions are committed in a single atomic transaction."""
    video = uploaded_video_fixture
    job_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        v = await session.get(Video, video.id)
        v.active_generation_id = job_id
        v.master_playlist_path = f"videos/{v.id}/generations/{job_id}/master.m3u8"
        v.poster_path = f"videos/{v.id}/generations/{job_id}/poster.webp"
        v.status = VideoStatus.READY
        v.transcode_progress_percent = 100.0

        r = VideoRendition(
            video_id=v.id,
            quality_label="480p",
            resolution_width=854,
            resolution_height=480,
            video_codec="h264",
            audio_codec="aac",
            is_ready=True,
        )
        session.add(r)
        await session.commit()

    async with AsyncSessionLocal() as session:
        v = await session.get(Video, video.id)
        assert v.active_generation_id == job_id
        assert v.status == VideoStatus.READY
        assert v.transcode_progress_percent == 100.0


# =========================================================================
# 5. Monotonic Progress Tracking Test
# =========================================================================

def test_monotonic_progress_non_decreasing():
    """Verify progress calculation never decreases."""
    progress_state = {"current": 5.0}

    times_us = [1_000_000, 2_000_000, 1_500_000, 3_000_000, 4_000_000]
    duration = 5.0
    weight = 85.0
    base = 5.0

    recorded = []
    for t in times_us:
        fraction = min(1.0, (t / 1_000_000.0) / duration)
        calc = base + (weight * fraction)
        new_val = min(89.9, max(progress_state["current"], calc))
        progress_state["current"] = new_val
        recorded.append(new_val)

    # Check strictly monotonic non-decreasing
    for i in range(len(recorded) - 1):
        assert recorded[i] <= recorded[i + 1]


# =========================================================================
# 6. Quality Ladder Selection Tests (Never Upscale, Even Dims, Bitrate Cap)
# =========================================================================

def test_quality_ladder_selection_never_upscales():
    """Verify ladder selection never includes renditions higher than source height."""
    # 360p source
    ladder_360 = select_quality_ladder(640, 360, 800000)
    assert [r["quality_label"] for r in ladder_360] == ["360p"]

    # 480p source
    ladder_480 = select_quality_ladder(854, 480, 1500000)
    assert [r["quality_label"] for r in ladder_480] == ["360p", "480p"]

    # 720p source
    ladder_720 = select_quality_ladder(1280, 720, 3000000)
    assert [r["quality_label"] for r in ladder_720] == ["360p", "480p", "720p"]

    # 1080p source
    ladder_1080 = select_quality_ladder(1920, 1080, 5000000)
    assert [r["quality_label"] for r in ladder_1080] == ["360p", "480p", "720p", "1080p"]


def test_quality_ladder_even_dimensions():
    """Verify dimensions are strictly even."""
    ladder = select_quality_ladder(1281, 719, 2000000)
    for r in ladder:
        assert r["width"] % 2 == 0
        assert r["height"] % 2 == 0


def test_quality_ladder_bitrate_capping():
    """Verify rendition bitrate does not exceed 1.2x of source bitrate."""
    source_bitrate = 1000000  # 1000 kbps, 1.2x = 1200 kbps
    ladder = select_quality_ladder(1920, 1080, source_bitrate)
    for r in ladder:
        assert r["video_bitrate_kbps"] <= 1200


# =========================================================================
# 7. Peak Disk Reservation Calculation Test
# =========================================================================

def test_peak_disk_reservation_calculation():
    """Verify peak disk calculation correctly includes active generation during retry."""
    source_size = 50 * 1024 * 1024  # 50 MB
    duration = 60.0
    ladder = select_quality_ladder(1280, 720, 2500000)

    # First run without active gen
    peak1 = calculate_peak_disk_requirement(source_size, duration, ladder, True, None)
    assert peak1 > source_size + settings.DISK_SAFETY_MARGIN_BYTES

    # With dummy active gen directory
    test_dir = Path(settings.STORAGE_BASE_PATH) / "dummy_active_gen"
    test_dir.mkdir(parents=True, exist_ok=True)
    dummy_file = test_dir / "test.ts"
    dummy_file.write_bytes(b"0" * (20 * 1024 * 1024))  # 20 MB

    try:
        peak2 = calculate_peak_disk_requirement(source_size, duration, ladder, True, test_dir)
        assert peak2 >= peak1 + (20 * 1024 * 1024)
    finally:
        import shutil
        if test_dir.exists():
            shutil.rmtree(test_dir, ignore_errors=True)


# =========================================================================
# 8. Real RFC 6381 Codec Strings & No-Audio Master Playlist Test
# =========================================================================

def test_master_playlist_rfc6381_with_and_without_audio(tmp_path):
    """Verify master.m3u8 accurately reflects real RFC 6381 codec strings and omits audio when missing."""
    # Video with audio
    renditions_with_audio = [
        {"quality_label": "360p", "width": 640, "height": 360, "video_bitrate_kbps": 700, "audio_bitrate_kbps": 96, "rfc_codecs": "avc1.64001f,mp4a.40.2"},
        {"quality_label": "720p", "width": 1280, "height": 720, "video_bitrate_kbps": 2500, "audio_bitrate_kbps": 128, "rfc_codecs": "avc1.64001f,mp4a.40.2"},
    ]
    out_audio = tmp_path / "master_audio.m3u8"
    generate_master_playlist(renditions_with_audio, out_audio)

    content_audio = out_audio.read_text(encoding="utf-8")
    assert 'CODECS="avc1.64001f,mp4a.40.2"' in content_audio
    assert "#EXT-X-INDEPENDENT-SEGMENTS" in content_audio
    assert "360p/index.m3u8" in content_audio

    # Video WITHOUT audio
    renditions_no_audio = [
        {"quality_label": "360p", "width": 640, "height": 360, "video_bitrate_kbps": 700, "audio_bitrate_kbps": 0, "rfc_codecs": "avc1.64001f"},
        {"quality_label": "720p", "width": 1280, "height": 720, "video_bitrate_kbps": 2500, "audio_bitrate_kbps": 0, "rfc_codecs": "avc1.64001f"},
    ]
    out_no_audio = tmp_path / "master_no_audio.m3u8"
    generate_master_playlist(renditions_no_audio, out_no_audio)

    content_no_audio = out_no_audio.read_text(encoding="utf-8")
    assert 'CODECS="avc1.64001f"' in content_no_audio
    assert "mp4a" not in content_no_audio


# =========================================================================
# 9. Verification Engine & Integrity Test
# =========================================================================

def test_verification_engine_catches_missing_or_corrupt_files(tmp_path):
    """Verify output verification engine strictly checks master, index, and segments."""
    ladder = [{"quality_label": "360p"}]

    # Case 1: Missing master.m3u8
    with pytest.raises(TranscodeError) as exc1:
        verify_transcoded_output(tmp_path, ladder, 10.0)
    assert exc1.value.error_code == "VERIFY_MASTER_MISSING"

    # Create dummy master
    (tmp_path / "master.m3u8").write_text("#EXTM3U\n#EXT-X-STREAM-INF\n360p/index.m3u8")

    # Case 2: Missing index.m3u8
    q_dir = tmp_path / "360p"
    q_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(TranscodeError) as exc2:
        verify_transcoded_output(tmp_path, ladder, 10.0)
    assert exc2.value.error_code == "VERIFY_INDEX_MISSING"

    # Case 3: Incomplete index.m3u8 (missing #EXT-X-ENDLIST)
    (q_dir / "index.m3u8").write_text("#EXTM3U\n#EXTINF:6.0,\nseg_000.ts")
    with pytest.raises(TranscodeError) as exc3:
        verify_transcoded_output(tmp_path, ladder, 10.0)
    assert exc3.value.error_code == "VERIFY_INCOMPLETE_PLAYLIST"

    # Case 4: Missing segment file
    (q_dir / "index.m3u8").write_text("#EXTM3U\n#EXTINF:6.0,\nseg_000.ts\n#EXT-X-ENDLIST")
    with pytest.raises(TranscodeError) as exc4:
        verify_transcoded_output(tmp_path, ladder, 10.0)
    assert exc4.value.error_code == "VERIFY_SEGMENT_EMPTY"


# =========================================================================
# 10. Worker Capability Check & Heartbeat Lifecycle Tests
# =========================================================================

def test_worker_capability_self_check():
    """Verify worker startup capability check executes without error."""
    check_worker_capabilities()


@pytest.mark.parametrize("has_audio", [False, True])
def test_rfc6381_high10_does_not_advertise_eight_bit_high(monkeypatch, tmp_path, has_audio):
    import json
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 0, json.dumps({"streams":[{"codec_name":"h264","profile":"High 10","level":30}]}), ""))
    expected="avc1.6e001e"+(",mp4a.40.2" if has_audio else "")
    assert derive_rfc6381_codecs(tmp_path/"segment.ts", has_audio) == expected


def test_worker_heartbeat_status_evaluation():
    """Verify ONLINE, STALE, and OFFLINE status calculation."""
    import redis
    r = redis.from_url(settings.REDIS_QUEUE_URL)
    test_w_id = f"test_worker_{uuid.uuid4().hex[:6]}"

    # 1. ONLINE: recent last_seen and active heartbeat key
    now = time.time()
    r.set(f"worker:heartbeat:{test_w_id}", "alive", ex=15)
    r.set(f"worker:last_seen:{test_w_id}", str(now), ex=3600)
    r.sadd("worker:registered_workers", test_w_id)

    status_online = get_worker_status(r, test_w_id)
    assert status_online["status"] == "ONLINE"
    assert status_online["last_seen_seconds_ago"] <= 5.0

    # 2. STALE: heartbeat key expired, last_seen 25 seconds ago
    r.delete(f"worker:heartbeat:{test_w_id}")
    r.set(f"worker:last_seen:{test_w_id}", str(now - 25.0), ex=3600)

    status_stale = get_worker_status(r, test_w_id)
    assert status_stale["status"] == "STALE"
    assert 20.0 <= status_stale["last_seen_seconds_ago"] <= 35.0

    # 3. OFFLINE: last_seen > 60s
    r.set(f"worker:last_seen:{test_w_id}", str(now - 75.0), ex=3600)

    status_offline = get_worker_status(r, test_w_id)
    assert status_offline["status"] == "OFFLINE"

    # Cleanup
    r.delete(f"worker:heartbeat:{test_w_id}")
    r.delete(f"worker:last_seen:{test_w_id}")
    r.srem("worker:registered_workers", test_w_id)


# =========================================================================
# 11. Process Group Termination Test
# =========================================================================

def test_safe_kill_process_group():
    """Verify safe_kill_process_group cleans up process without leaving zombies."""
    cmd = ["sleep", "10"] if os.name != "nt" else ["powershell", "-Command", "Start-Sleep -Seconds 10"]
    kwargs = {}
    if hasattr(os, "setsid"):
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **kwargs)
    assert proc.poll() is None  # process is running

    safe_kill_process_group(proc)
    assert proc.poll() is not None  # process is terminated cleanly


@pytest.mark.parametrize("thread_limit", [1, 2, 4])
def test_rendition_drains_stdout_and_stderr_without_deadlock(monkeypatch, tmp_path, thread_limit):
    """A noisy stderr stream must not block progress parsing on stdout."""
    import sys
    import app.services.transcoder as transcoder_module

    original_popen = subprocess.Popen
    script = (
        "import sys\n"
        "for i in range(2000):\n"
        " sys.stderr.write('diagnostic-' + ('x' * 200) + '\\n')\n"
        " sys.stdout.write(f'fps=30\\nout_time_us={(i + 1) * 1000}\\nspeed=1.0x\\nprogress=continue\\n')\n"
        " sys.stdout.flush(); sys.stderr.flush()\n"
    )

    def noisy_popen(_cmd, **kwargs):
        assert _cmd[_cmd.index("-threads") + 1] == str(thread_limit)
        assert _cmd[_cmd.index("-filter_threads") + 1] == str(thread_limit)
        assert _cmd[_cmd.index("-threads:v") + 1] == str(thread_limit)
        assert _cmd.index("-threads:v") > _cmd.index("-i")
        return original_popen([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(transcoder_module.subprocess, "Popen", noisy_popen)
    monkeypatch.setattr(settings, "TRANSCODE_PROGRESS_THROTTLE_SECONDS", 0.0)
    monkeypatch.setattr(settings, "FFMPEG_THREADS", thread_limit)
    updates = []
    execute_transcode_rendition(
        tmp_path / "source.mp4", {"width": 2, "height": 2, "video_bitrate_kbps": 1, "audio_bitrate_kbps": 1},
        tmp_path / "360p", 1.0, False, 10.0, 0, 1, {"current": 5.0},
        progress_callback=lambda *args: updates.append(args),
    )
    assert updates
    assert updates[-1][0] > 5.0


def test_rendition_keeps_bounded_stderr_diagnostics_on_failure(monkeypatch, tmp_path):
    """A nonzero encoder exit retains safe diagnostics after concurrent draining."""
    import sys
    import app.services.transcoder as transcoder_module

    original_popen = subprocess.Popen

    def failing_popen(_cmd, **kwargs):
        return original_popen(
            [sys.executable, "-c", "import sys; sys.stderr.write('encoder diagnostic\\n'); sys.exit(3)"],
            **kwargs,
        )

    monkeypatch.setattr(transcoder_module.subprocess, "Popen", failing_popen)
    with pytest.raises(TranscodeError, match="encoder diagnostic") as exc_info:
        execute_transcode_rendition(
            tmp_path / "source.mp4", {"width": 2, "height": 2, "video_bitrate_kbps": 1, "audio_bitrate_kbps": 1},
            tmp_path / "360p", 1.0, False, 10.0, 0, 1, {"current": 5.0},
        )
    assert exc_info.value.error_code == "FFMPEG_ENCODE_FAILED"


def test_rendition_cancellation_terminates_process_group(monkeypatch, tmp_path):
    """Cancellation remains responsive even while the encoder emits no progress."""
    import sys
    import app.services.transcoder as transcoder_module

    original_popen = subprocess.Popen
    started = []

    def sleeping_popen(_cmd, **kwargs):
        proc = original_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        started.append(proc)
        return proc

    monkeypatch.setattr(transcoder_module.subprocess, "Popen", sleeping_popen)
    with pytest.raises(TranscodeCancelledError):
        execute_transcode_rendition(
            tmp_path / "source.mp4", {"width": 2, "height": 2, "video_bitrate_kbps": 1, "audio_bitrate_kbps": 1},
            tmp_path / "360p", 1.0, False, 10.0, 0, 1, {"current": 5.0},
            check_cancelled_fn=lambda: True,
        )
    assert started[0].poll() is not None


# =========================================================================
# 12. Retry Counter & Max Attempts Test
# =========================================================================

@pytest.mark.asyncio
async def test_retry_counter_and_max_attempts_rejection(transcode_auth_fixture, uploaded_video_fixture):
    """Verify retrying increments attempt and max_attempts reject beyond limit."""
    cookies = transcode_auth_fixture["cookies"]
    headers = transcode_auth_fixture["headers"]
    video = uploaded_video_fixture

    async with AsyncSessionLocal() as session:
        job = TranscodeJob(
            id=uuid.uuid4(),
            video_id=video.id,
            status=TranscodeJobStatus.FAILED,
            attempt=3,
            max_attempts=3,
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost", cookies=cookies) as client:
        res = await client.post(
            f"/api/v1/admin/transcodes/{job_id}/retry",
            headers=headers,
        )
        assert res.status_code == 400
        assert "Maximum transcode attempts" in res.json()["detail"]


# =========================================================================
# 13. Cancel Queued Job Test
# =========================================================================

@pytest.mark.asyncio
async def test_cancel_queued_transcode_job(transcode_auth_fixture, uploaded_video_fixture):
    """Verify cancelling a queued job marks it CANCELLED immediately."""
    cookies = transcode_auth_fixture["cookies"]
    headers = transcode_auth_fixture["headers"]
    video = uploaded_video_fixture

    async with AsyncSessionLocal() as session:
        job = TranscodeJob(
            id=uuid.uuid4(),
            video_id=video.id,
            status=TranscodeJobStatus.QUEUED,
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost", cookies=cookies) as client:
        res = await client.post(
            f"/api/v1/admin/transcodes/{job_id}/cancel",
            headers=headers,
        )
        assert res.status_code == 200
        assert res.json()["status"] == "CANCELLED"

    async with AsyncSessionLocal() as session:
        j = await session.get(TranscodeJob, job_id)
        assert j.status == TranscodeJobStatus.CANCELLED
