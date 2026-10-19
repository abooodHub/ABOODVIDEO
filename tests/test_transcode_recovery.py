"""Crash-recovery and resumable-checkpoint tests for the transcode pipeline.

Covers:
A. worker disappears during PROCESSING -> reconciler marks job terminal, video retryable
B. RQ AbandonedJobError -> DB does not remain PROCESSING
C. complete + partial renditions -> retry reuses only verified complete renditions
D. playlist missing ENDLIST -> rendition rejected
E. segment missing -> rendition rejected
F. source SHA/profile changed -> old checkpoint rejected
G. all renditions verified -> recovery scan fully green
H. reconciler never kills a healthy long-running transcode (heartbeat alive)
"""
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest
from redis import Redis
from rq import Queue
from rq.registry import FailedJobRegistry

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.user import User, UserRole
from app.models.video import Video, VideoStatus, TranscodeJob, TranscodeJobStatus
from app.security.hashing import hash_password
from app.services.rendition_checkpoint import (
    build_rendition_fingerprint,
    copy_rendition_artifacts,
    find_reusable_renditions,
    scan_generation_renditions,
    verify_rendition_checkpoint,
    write_checkpoint,
)
from app.services.transcoder import generate_master_playlist
from app.workers import reconciler
from app.workers.reconciler import (
    TRANSCODE_ABANDONED_ERROR_CODE,
    reconcile_stalled_transcode_jobs,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

@pytest.fixture
async def recovery_video():
    async with AsyncSessionLocal() as session:
        owner = User(
            id=uuid.uuid4(),
            email=f"recovery_{uuid.uuid4().hex[:8]}@aboodvideo.local",
            username=f"recovery_{uuid.uuid4().hex[:8]}",
            hashed_password=hash_password("RecoveryPass123!"),
            role=UserRole.OWNER,
            is_active=True,
        )
        session.add(owner)
        await session.commit()

        video = Video(
            id=uuid.uuid4(),
            user_id=owner.id,
            title="Recovery Test Video",
            status=VideoStatus.PROCESSING,
            source_filename="recovery.mkv",
            source_size_bytes=100 * 1024 * 1024,
            source_duration_seconds=120.0,
            source_resolution_width=1920,
            source_resolution_height=1080,
            source_bitrate=5_000_000,
            source_codec="h264",
        )
        session.add(video)
        await session.commit()
        await session.refresh(video)
        return video


def _make_rendition_dir(base: Path, label: str, segments: int = 5, seg_duration: float = 6.0,
                        endlist: bool = True, skip_segment: int | None = None,
                        zero_segment: int | None = None) -> Path:
    """Build a synthetic HLS rendition directory."""
    rdir = base / label
    rdir.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-TARGETDURATION:6"]
    for i in range(segments):
        lines.append(f"#EXTINF:{seg_duration:.6f},")
        lines.append(f"seg_{i:03d}.ts")
        if i != skip_segment:
            payload = b"" if i == zero_segment else (b"FAKETS" * 200)
            (rdir / f"seg_{i:03d}.ts").write_bytes(payload)
    if endlist:
        lines.append("#EXT-X-ENDLIST")
    (rdir / "index.m3u8").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rdir


def _ladder(*labels):
    presets = {
        "360p": (640, 360, 700), "480p": (854, 480, 1200), "720p": (1280, 720, 2500),
        "1080p": (1920, 1080, 4500), "2160p": (3840, 2160, 20000),
    }
    return [
        {"quality_label": l, "width": presets[l][0], "height": presets[l][1],
         "video_bitrate_kbps": presets[l][2], "audio_bitrate_kbps": 128}
        for l in labels
    ]


def _probe(duration=30.0):
    return {"width": 1920, "height": 1080, "duration": duration, "bitrate": 5_000_000,
            "video_codec": "h264", "has_audio": True}


# ---------------------------------------------------------------------------
# A. Worker disappears during PROCESSING
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconciler_marks_job_terminal_when_worker_gone(recovery_video, monkeypatch):
    video = recovery_video
    stale_time = datetime.now(timezone.utc) - timedelta(hours=2)
    async with AsyncSessionLocal() as session:
        job = TranscodeJob(
            id=uuid.uuid4(), video_id=video.id, status=TranscodeJobStatus.PROCESSING,
            attempt=1, max_attempts=3, worker_id="worker-vanished-1",
            current_rendition="2160p", progress_percent=85.0,
        )
        job.updated_at = stale_time
        session.add(job)
        await session.commit()
        job_id = job.id

    monkeypatch.setattr(reconciler, "get_worker_status", lambda conn, worker_id=None: {"status": "OFFLINE", "worker_id": worker_id})

    handled = await reconcile_stalled_transcode_jobs(threshold_minutes=10, redis_conn=None)
    assert handled >= 1

    async with AsyncSessionLocal() as session:
        job = await session.get(TranscodeJob, job_id)
        assert job.status == TranscodeJobStatus.FAILED
        assert job.error_code == TRANSCODE_ABANDONED_ERROR_CODE
        assert job.completed_at is not None
        video = await session.get(Video, video.id)
        assert video.status == VideoStatus.FAILED  # retryable per trigger semantics


# ---------------------------------------------------------------------------
# B. RQ AbandonedJobError -> DB must not remain PROCESSING
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconciler_marks_terminal_when_rq_abandoned(recovery_video, monkeypatch):
    video = recovery_video
    async with AsyncSessionLocal() as session:
        job = TranscodeJob(
            id=uuid.uuid4(), video_id=video.id, status=TranscodeJobStatus.PROCESSING,
            attempt=2, max_attempts=3, worker_id="worker-crashed-1",
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    redis_conn = Redis.from_url(settings.REDIS_QUEUE_URL)
    queue = Queue("transcode", connection=redis_conn)
    rq_job = None
    try:
        rq_job = queue.enqueue(
            "app.workers.transcode_worker.process_transcode_job",
            str(job_id),
            job_timeout=60,
        )
        FailedJobRegistry("transcode", connection=redis_conn).add(rq_job)

        monkeypatch.setattr(reconciler, "get_worker_status", lambda conn, worker_id=None: {"status": "OFFLINE"})

        handled = await reconcile_stalled_transcode_jobs(threshold_minutes=0, redis_conn=redis_conn)
        assert handled >= 1

        async with AsyncSessionLocal() as session:
            job = await session.get(TranscodeJob, job_id)
            assert job.status == TranscodeJobStatus.FAILED
            assert job.error_code == TRANSCODE_ABANDONED_ERROR_CODE
    finally:
        if rq_job is not None:
            try:
                FailedJobRegistry("transcode", connection=redis_conn).remove(rq_job)
            except Exception:
                pass
        try:
            redis_conn.delete(f"rq:job:{rq_job.id}" if rq_job else "")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# C. Retry reuses only VERIFIED complete renditions
# ---------------------------------------------------------------------------

def test_retry_reuses_only_verified_complete_renditions(tmp_path):
    gen = tmp_path / str(uuid.uuid4())
    for label in ("360p", "480p", "720p"):
        _make_rendition_dir(gen, label)
    # 1080p partial: no ENDLIST, missing final segment
    _make_rendition_dir(gen, "1080p", segments=5, endlist=False, skip_segment=4)

    ladder = _ladder("360p", "480p", "720p", "1080p")
    probe = _probe()
    reusable = find_reusable_renditions(
        uuid.uuid4(), ladder, probe, source_sha256=None,
        base_temp_dir=tmp_path, probe_media=False,
    )
    # base_temp_dir points at the parent containing our synthetic generation dir
    assert set(reusable.keys()) == {"360p", "480p", "720p"}
    assert "1080p" not in reusable
    assert all(reusable[l]["verification"]["ok"] for l in reusable)


# ---------------------------------------------------------------------------
# D. Playlist missing ENDLIST -> rejected
# ---------------------------------------------------------------------------

def test_checkpoint_rejects_playlist_without_endlist(tmp_path):
    rdir = _make_rendition_dir(tmp_path, "720p", endlist=False)
    res = verify_rendition_checkpoint(rdir, {"quality_label": "720p", "width": 1280, "height": 720},
                                      source_duration=30.0, probe_media=False)
    assert not res["ok"]
    assert any("ENDLIST" in r for r in res["reasons"])


# ---------------------------------------------------------------------------
# E. Segment missing -> rejected
# ---------------------------------------------------------------------------

def test_checkpoint_rejects_missing_segment(tmp_path):
    rdir = _make_rendition_dir(tmp_path, "720p", skip_segment=3)
    res = verify_rendition_checkpoint(rdir, {"quality_label": "720p", "width": 1280, "height": 720},
                                      source_duration=30.0, probe_media=False)
    assert not res["ok"]
    assert any("missing" in r for r in res["reasons"])


def test_checkpoint_rejects_zero_byte_segment(tmp_path):
    rdir = _make_rendition_dir(tmp_path, "720p", zero_segment=1)
    res = verify_rendition_checkpoint(rdir, {"quality_label": "720p", "width": 1280, "height": 720},
                                      source_duration=30.0, probe_media=False)
    assert not res["ok"]


# ---------------------------------------------------------------------------
# F. Source SHA / profile changed -> old checkpoint rejected
# ---------------------------------------------------------------------------

def test_checkpoint_rejects_fingerprint_mismatch(tmp_path):
    rdir = _make_rendition_dir(tmp_path, "720p")
    probe = _probe()
    rendition = _ladder("720p")[0]
    fp_original = build_rendition_fingerprint("sha-original", probe, rendition)
    write_checkpoint(rdir, fp_original)

    res_ok = verify_rendition_checkpoint(rdir, rendition, source_duration=30.0,
                                         fingerprint=fp_original, probe_media=False)
    assert res_ok["ok"]
    assert res_ok["fingerprint_match"] is True

    fp_changed = build_rendition_fingerprint("sha-CHANGED", probe, rendition)
    res_bad = verify_rendition_checkpoint(rdir, rendition, source_duration=30.0,
                                          fingerprint=fp_changed, probe_media=False)
    assert not res_bad["ok"]
    assert res_bad["fingerprint_match"] is False


def test_checkpoint_retroactive_mode_when_no_checkpoint_file(tmp_path):
    rdir = _make_rendition_dir(tmp_path, "720p")
    fp = build_rendition_fingerprint("sha-x", _probe(), _ladder("720p")[0])
    res = verify_rendition_checkpoint(rdir, _ladder("720p")[0], source_duration=30.0,
                                      fingerprint=fp, probe_media=False)
    assert res["ok"]            # media checks pass -> reusable
    assert res["retroactive"]   # but flagged as retroactive verification
    assert res["fingerprint_match"] is None


# ---------------------------------------------------------------------------
# G. All renditions verified -> final assembly inputs complete
# ---------------------------------------------------------------------------

def test_all_renditions_verified_supports_master_generation(tmp_path):
    gen = tmp_path / "gen-complete"
    ladder = _ladder("360p", "720p", "1080p")
    for r in ladder:
        _make_rendition_dir(gen, r["quality_label"])

    results = scan_generation_renditions(gen, ladder, _probe(), probe_media=False)
    assert all(res["ok"] for res in results.values())
    assert not any(res["retroactive"] for res in results.values())

    configs = [dict(r, rfc_codecs="avc1.64001f,mp4a.40.2") for r in ladder]
    generate_master_playlist(configs, gen / "master.m3u8")
    master = (gen / "master.m3u8").read_text(encoding="utf-8")
    for r in ladder:
        assert f'{r["width"]}x{r["height"]}' in master
        assert f'{r["quality_label"]}/index.m3u8' in master


# ---------------------------------------------------------------------------
# H. Healthy long-running transcode must never be reconciled away
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconciler_never_touches_healthy_running_job(recovery_video, monkeypatch):
    video = recovery_video
    async with AsyncSessionLocal() as session:
        job = TranscodeJob(
            id=uuid.uuid4(), video_id=video.id, status=TranscodeJobStatus.PROCESSING,
            attempt=1, max_attempts=3, worker_id="worker-healthy-1",
            progress_percent=60.0,
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    # Heartbeat alive: worker ONLINE even though the job has been running long.
    monkeypatch.setattr(reconciler, "get_worker_status",
                        lambda conn, worker_id=None: {"status": "ONLINE", "worker_id": "worker-healthy-1"})

    handled = await reconcile_stalled_transcode_jobs(threshold_minutes=0, redis_conn=None)
    assert handled == 0

    async with AsyncSessionLocal() as session:
        job = await session.get(TranscodeJob, job_id)
        assert job.status == TranscodeJobStatus.PROCESSING
        assert job.error_code is None


# ---------------------------------------------------------------------------
# Artifact copy safety
# ---------------------------------------------------------------------------

def test_copy_rendition_artifacts_creates_independent_copy(tmp_path):
    src = _make_rendition_dir(tmp_path / "old-gen", "720p")
    dst = tmp_path / "new-gen" / "720p"
    copy_rendition_artifacts(src, dst)
    assert (dst / "index.m3u8").exists()
    assert (dst / "seg_000.ts").exists()
    assert (dst / "index.m3u8").read_bytes() == (src / "index.m3u8").read_bytes()
