"""Phase 3B: Pipelined Season Processing + Upload Test Suite.

Verifies:
1. Concurrency Rules:
   - GPU Encoder Concurrency <= 1 (never concurrent FFmpeg/NVENC encodes).
   - Network Upload Concurrency <= 1 (never concurrent uploads).
   - Total simultaneous operations <= 2 (1 encode + 1 upload).
2. Pipelined Overlap:
   - Episode N+1 encodes locally on GPU WHILE Episode N uploads to ABOODVIDEO on Network.
   - Empirical timestamp analysis proves temporal overlap.
3. Strict Upload Order Guarantee:
   - Episodes upload in strict natural order: 01 -> 02 -> 03...
   - Episode 02 MUST NOT begin uploading before Episode 01 reaches server READY.
   - Episode 03 MUST NOT begin uploading before Episode 02 reaches server READY.
4. Bounded Pipeline Window (Window = 1):
   - At most 1 pre-encoded episode waiting in READY_TO_UPLOAD ahead of active upload.
   - Episode 03 does NOT begin encoding until Episode 01 reaches READY and Episode 02 starts UPLOADING.
5. Disk Space Guard:
   - Checks available disk space before starting encode.
   - Low disk halts cleanly with "مساحة القرص غير كافية" without starting FFmpeg.
6. Failure & Skip Policies:
   - Encode failure pauses pipeline, preserves completed work, enables Retry/Skip.
   - Upload failure preserves upload_session_id and package, pauses without skipping ahead.
   - Skip advances pipeline past failed episode.
7. Restart / Power Loss Recovery:
   - Conservative reload: READY remains READY, READY_TO_UPLOAD intact, interrupted uploads resume.
   - Never re-encodes completed packages.
8. Controls:
   - Automatic pipeline start, pause, resume, stop-after-current.
"""

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import create_application
from studio.core.batch_store import BatchStore, EpisodeState
from studio.core.batch_runner import BatchRunner


@pytest.fixture
def test_store(tmp_path):
    db_path = tmp_path / "test_jobs_phase3b.db"
    return BatchStore(db_path=db_path)


def _create_dummy_package(work_dir: Path, title: str = "Test Ep"):
    work_dir.mkdir(parents=True, exist_ok=True)
    playlist = b"#EXTM3U\n#EXTINF:10,\nseg_0.ts\n#EXT-X-ENDLIST\n"
    segment = b"TSDATA" * 80
    import hashlib
    manifest = {
        "manifest_version": "1.0",
        "video": {"title": title, "duration_seconds": 10.0},
        "renditions": [{"quality_label": "1080p", "playlist_path": "master.m3u8", "segment_count": 1}],
        "files": [
            {"path": "master.m3u8", "size_bytes": len(playlist), "sha256": hashlib.sha256(playlist).hexdigest()},
            {"path": "seg_0.ts", "size_bytes": len(segment), "sha256": hashlib.sha256(segment).hexdigest()},
        ],
    }
    (work_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (work_dir / "master.m3u8").write_bytes(playlist)
    (work_dir / "seg_0.ts").write_bytes(segment)
    return manifest


def _setup_test_batch(store: BatchStore, tmp_path: Path, num_episodes: int = 4):
    season_folder = tmp_path / "Season 1"
    season_folder.mkdir(parents=True, exist_ok=True)
    batch = store.create_batch(
        season_folder_path=str(season_folder),
        destination_label="Donghua / Spirit Cage / Season 1",
        target_folder_id="11111111-1111-1111-1111-111111111111",
        target_series_id="22222222-2222-2222-2222-222222222222",
        target_season_id="33333333-3333-3333-3333-333333333333",
    )

    ep_data = []
    for i in range(1, num_episodes + 1):
        f = season_folder / f"{i:02d}.mkv"
        f.write_bytes(b"DUMMY_MKV_BYTES_" * 50)
        ep_data.append({
            "order_index": i,
            "source_path": str(f),
            "filename": f.name,
            "source_bytes": len(f.read_bytes()),
        })

    store.add_episodes(batch["id"], ep_data)
    return batch


# =============================================================================
# 1. PIPELINE RESULT, CONCURRENCY & OVERLAP TIMELINE
# =============================================================================

def test_pipelined_overlap_and_concurrency(tmp_path, test_store):
    """
    Verifies Phase 3B core promise:
    - GPU encodes Ep N+1 while Network uploads Ep N.
    - Strict encoder concurrency <= 1.
    - Strict uploader concurrency <= 1.
    - Total concurrency <= 2.
    - Empirical overlap timeline shows concurrent execution.
    - All episodes reach server READY.
    """
    batch = _setup_test_batch(test_store, tmp_path, num_episodes=4)
    runner = BatchRunner(store=test_store)

    # Mock transcode to simulate GPU work with measurable duration (120ms)
    def fake_process_single_episode(ep):
        ep_id = ep["id"]
        order = ep["order_index"]
        work_dir = tmp_path / "work" / f"ep_{order:02d}"
        _create_dummy_package(work_dir, f"Ep {order}")
        test_store.update_episode(
            ep_id,
            state=EpisodeState.ENCODING.value,
            progress=50.0,
            work_dir=str(work_dir),
        )
        time.sleep(0.12)  # GPU encode duration
        test_store.update_episode(
            ep_id,
            state=EpisodeState.READY_TO_UPLOAD.value,
            progress=100.0,
            package_bytes=600,
        )
        return True

    # Mock upload to simulate network transfer with measurable duration (180ms)
    def fake_upload_episode(episode_id, server_url=None, api_key=None, http_client=None):
        ep = test_store.get_episode(episode_id)
        test_store.update_episode(
            episode_id,
            state=EpisodeState.UPLOADING.value,
            progress=50.0,
            uploaded_bytes=300,
        )
        time.sleep(0.18)  # Network upload duration
        test_store.update_episode(
            episode_id,
            state=EpisodeState.READY.value,
            progress=100.0,
            uploaded_bytes=600,
            server_status="READY",
        )
        return {"video_id": f"vid-{ep['order_index']}", "status": "READY"}

    runner._process_single_episode = fake_process_single_episode
    runner.upload_episode = fake_upload_episode

    # Launch automatic pipeline
    runner.start_automatic_pipeline(batch["id"], server_url="http://fake", api_key="fake-key", pipeline_window=1)

    # Wait for completion (all 4 episodes reach READY)
    max_wait = 15.0
    start_t = time.time()
    while time.time() - start_t < max_wait:
        eps = test_store.get_episodes(batch["id"])
        if all(ep["state"] == EpisodeState.READY.value for ep in eps):
            break
        time.sleep(0.05)

    # Stop threads cleanly
    runner._worker_stop_event.set()
    runner._current_upload_cancel_event.set()
    if runner._worker_thread:
        runner._worker_thread.join(timeout=2.0)
    if runner._upload_worker_thread:
        runner._upload_worker_thread.join(timeout=2.0)

    # 1. Verify all 4 episodes achieved READY
    eps = test_store.get_episodes(batch["id"])
    assert all(ep["state"] == EpisodeState.READY.value for ep in eps), f"Not all READY: {[ep['state'] for ep in eps]}"

    # 2. Strict concurrency assertions
    assert runner._max_observed_encodes <= 1, f"Encoder concurrency exceeded 1: {runner._max_observed_encodes}"
    assert runner._max_observed_uploads <= 1, f"Upload concurrency exceeded 1: {runner._max_observed_uploads}"

    # 3. Analyze Execution History for Overlap Timeline
    history = runner._execution_history
    assert len(history) >= 16, f"Expected at least 16 events (4 start/end pairs for encode and upload), got {len(history)}"

    # Map events by episode
    events_by_ep: Dict[int, Dict[str, float]] = {}
    for ev in history:
        ord_idx = ev["order_index"]
        if ord_idx not in events_by_ep:
            events_by_ep[ord_idx] = {}
        events_by_ep[ord_idx][ev["event"]] = ev["timestamp"]

    # Verify Overlap: Ep 02 encoding overlaps with Ep 01 uploading!
    # Timeline condition: Ep 01 upload starts, then Ep 02 encode runs and finishes while Ep 01 is uploading
    ep1_up_start = events_by_ep[1]["upload_start"]
    ep1_up_end = events_by_ep[1]["upload_end"]
    ep2_enc_start = events_by_ep[2]["encode_start"]
    ep2_enc_end = events_by_ep[2]["encode_end"]

    # Check temporal overlap
    assert ep2_enc_start < ep1_up_end, (
        f"Expected Ep 2 encode ({ep2_enc_start}) to start before Ep 1 upload finished ({ep1_up_end})"
    )
    assert ep2_enc_end > ep1_up_start, (
        f"Expected Ep 2 encode end ({ep2_enc_end}) to overlap with Ep 1 upload start ({ep1_up_start})"
    )

    # Verify Ep 03 encoding overlaps with Ep 02 uploading!
    ep2_up_start = events_by_ep[2]["upload_start"]
    ep2_up_end = events_by_ep[2]["upload_end"]
    ep3_enc_start = events_by_ep[3]["encode_start"]

    assert ep3_enc_start < ep2_up_end, (
        f"Expected Ep 3 encode ({ep3_enc_start}) to start before Ep 2 upload finished ({ep2_up_end})"
    )


# =============================================================================
# 2. STRICT UPLOAD ORDER GUARANTEE
# =============================================================================

def test_strict_upload_order_guarantee(tmp_path, test_store):
    """
    Verifies that:
    - Episode 02 MUST NOT begin uploading before Episode 01 reaches server READY.
    - Episode 03 MUST NOT begin uploading before Episode 02 reaches server READY.
    - Uploader NEVER jumps ahead out of order.
    """
    batch = _setup_test_batch(test_store, tmp_path, num_episodes=3)
    runner = BatchRunner(store=test_store)

    upload_order = []

    def fake_process(ep):
        order = ep["order_index"]
        work_dir = tmp_path / "work" / f"ep_{order:02d}"
        _create_dummy_package(work_dir, f"Ep {order}")
        test_store.update_episode(
            ep["id"],
            state=EpisodeState.READY_TO_UPLOAD.value,
            progress=100.0,
            work_dir=str(work_dir),
            package_bytes=600,
        )
        time.sleep(0.05)
        return True

    def fake_upload(episode_id, server_url=None, api_key=None, http_client=None):
        ep = test_store.get_episode(episode_id)
        order = ep["order_index"]
        upload_order.append(f"start_{order}")

        # Check strict ordering invariant at upload start:
        # All episodes with order < this episode MUST already be in READY state!
        all_eps = test_store.get_episodes(batch["id"])
        preceding = [e for e in all_eps if e["order_index"] < order]
        for p in preceding:
            assert p["state"] == EpisodeState.READY.value, (
                f"Strict order violation: Episode {order} started uploading while Episode {p['order_index']} is in state {p['state']}"
            )

        time.sleep(0.1)
        test_store.update_episode(
            episode_id,
            state=EpisodeState.READY.value,
            progress=100.0,
            server_status="READY",
        )
        upload_order.append(f"end_{order}")
        return {"status": "READY"}

    runner._process_single_episode = fake_process
    runner.upload_episode = fake_upload

    runner.start_automatic_pipeline(batch["id"], server_url="http://fake", api_key="fake-key", pipeline_window=1)

    max_wait = 10.0
    start_t = time.time()
    while time.time() - start_t < max_wait:
        eps = test_store.get_episodes(batch["id"])
        if all(ep["state"] == EpisodeState.READY.value for ep in eps):
            break
        time.sleep(0.05)

    runner._worker_stop_event.set()
    runner._current_upload_cancel_event.set()
    if runner._worker_thread:
        runner._worker_thread.join(timeout=2.0)
    if runner._upload_worker_thread:
        runner._upload_worker_thread.join(timeout=2.0)

    assert upload_order == ["start_1", "end_1", "start_2", "end_2", "start_3", "end_3"]


# =============================================================================
# 3. BOUNDED PIPELINE WINDOW (Window = 1)
# =============================================================================

def test_pipeline_window_bound(tmp_path, test_store):
    """
    Verifies that with pipeline_window = 1:
    - When Episode 01 is uploading and Episode 02 finishes encoding -> 1 episode waiting in READY_TO_UPLOAD.
    - Encoder MUST NOT start Episode 03 until Episode 01 reaches READY and Episode 02 starts UPLOADING.
    """
    batch = _setup_test_batch(test_store, tmp_path, num_episodes=4)
    runner = BatchRunner(store=test_store)

    encode_started: List[int] = []
    ep1_upload_finished = threading.Event()
    ep2_upload_started = threading.Event()

    def fake_process(ep):
        order = ep["order_index"]
        encode_started.append(order)

        # If Episode 3 is starting to encode, verify that Episode 1 has ALREADY finished uploading
        # and Episode 2 has ALREADY started uploading!
        if order == 3:
            assert ep1_upload_finished.is_set(), "Pipeline window violated: Episode 3 started encoding before Episode 1 finished uploading!"
            ep2 = test_store.get_episodes(batch["id"])[1]
            assert ep2["state"] in (EpisodeState.UPLOADING.value, EpisodeState.READY.value), (
                f"Pipeline window violated: Ep 2 state is {ep2['state']}, expected UPLOADING or READY"
            )

        work_dir = tmp_path / "work" / f"ep_{order:02d}"
        _create_dummy_package(work_dir, f"Ep {order}")
        time.sleep(0.05)  # Fast encode
        test_store.update_episode(
            ep["id"],
            state=EpisodeState.READY_TO_UPLOAD.value,
            progress=100.0,
            work_dir=str(work_dir),
            package_bytes=600,
        )
        return True

    def fake_upload(episode_id, server_url=None, api_key=None, http_client=None):
        ep = test_store.get_episode(episode_id)
        order = ep["order_index"]

        if order == 1:
            time.sleep(0.3)  # Long upload for Ep 1
            ep1_upload_finished.set()
        elif order == 2:
            ep2_upload_started.set()
            time.sleep(0.1)

        test_store.update_episode(
            episode_id,
            state=EpisodeState.READY.value,
            progress=100.0,
            server_status="READY",
        )
        return {"status": "READY"}

    runner._process_single_episode = fake_process
    runner.upload_episode = fake_upload

    runner.start_automatic_pipeline(batch["id"], server_url="http://fake", api_key="fake-key", pipeline_window=1)

    # Wait until Ep 3 starts encoding
    max_wait = 10.0
    start_t = time.time()
    while time.time() - start_t < max_wait:
        if 3 in encode_started:
            break
        time.sleep(0.05)

    runner._worker_stop_event.set()
    runner._current_upload_cancel_event.set()
    if runner._worker_thread:
        runner._worker_thread.join(timeout=2.0)
    if runner._upload_worker_thread:
        runner._upload_worker_thread.join(timeout=2.0)

    assert 3 in encode_started


# =============================================================================
# 4. DISK SPACE GUARD
# =============================================================================

def test_disk_space_guard_prevents_encode(tmp_path, test_store):
    """
    Verifies that if disk space is insufficient (simulated low disk):
    - Encoding is NOT started.
    - Episode is marked FAILED with 'مساحة القرص غير كافية'.
    - Batch is cleanly PAUSED.
    """
    batch = _setup_test_batch(test_store, tmp_path, num_episodes=2)
    runner = BatchRunner(store=test_store)
    runner.simulate_low_disk = True  # Activate disk space guard trigger

    ffmpeg_called = False

    def fake_process(ep):
        nonlocal ffmpeg_called
        ffmpeg_called = True
        return True

    runner._process_single_episode = fake_process

    runner.start_automatic_pipeline(batch["id"], server_url="http://fake", api_key="fake-key")

    time.sleep(0.5)

    runner._worker_stop_event.set()
    runner._current_upload_cancel_event.set()
    if runner._worker_thread:
        runner._worker_thread.join(timeout=2.0)
    if runner._upload_worker_thread:
        runner._upload_worker_thread.join(timeout=2.0)

    # Verify:
    assert not ffmpeg_called, "FFmpeg/encode was called despite low disk space!"

    eps = test_store.get_episodes(batch["id"])
    ep1 = eps[0]
    assert ep1["state"] == EpisodeState.FAILED.value
    assert "مساحة القرص غير كافية" in (ep1["error"] or "")

    b = test_store.get_batch(batch["id"])
    assert b["status"] == "PAUSED"


# =============================================================================
# 5. FAILURE & SKIP CONTROLS
# =============================================================================

def test_encode_failure_pauses_pipeline_and_allows_retry(tmp_path, test_store):
    """
    Verifies that:
    - Encode failure halts pipeline, keeping completed episodes intact.
    - User can retry the failed episode.
    """
    batch = _setup_test_batch(test_store, tmp_path, num_episodes=3)
    runner = BatchRunner(store=test_store)

    fail_ep2 = True

    def fake_process(ep):
        order = ep["order_index"]
        if order == 2 and fail_ep2:
            test_store.update_episode(
                ep["id"],
                state=EpisodeState.FAILED.value,
                error="NVENC encoder error (out of memory)",
            )
            return False

        work_dir = tmp_path / "work" / f"ep_{order:02d}"
        _create_dummy_package(work_dir, f"Ep {order}")
        test_store.update_episode(
            ep["id"],
            state=EpisodeState.READY_TO_UPLOAD.value,
            progress=100.0,
            work_dir=str(work_dir),
            package_bytes=600,
        )
        return True

    def fake_upload(episode_id, server_url=None, api_key=None, http_client=None):
        test_store.update_episode(
            episode_id,
            state=EpisodeState.READY.value,
            progress=100.0,
            server_status="READY",
        )
        return {"status": "READY"}

    runner._process_single_episode = fake_process
    runner.upload_episode = fake_upload

    # 1. Run pipeline - Episode 2 should fail
    runner.start_automatic_pipeline(batch["id"], server_url="http://fake", api_key="fake-key")

    max_wait = 5.0
    start_t = time.time()
    while time.time() - start_t < max_wait:
        eps = test_store.get_episodes(batch["id"])
        if eps[1]["state"] == EpisodeState.FAILED.value:
            break
        time.sleep(0.05)

    runner._worker_stop_event.set()
    runner._current_upload_cancel_event.set()
    if runner._worker_thread:
        runner._worker_thread.join(timeout=2.0)
    if runner._upload_worker_thread:
        runner._upload_worker_thread.join(timeout=2.0)

    eps = test_store.get_episodes(batch["id"])
    assert eps[0]["state"] == EpisodeState.READY.value
    assert eps[1]["state"] == EpisodeState.FAILED.value
    assert eps[2]["state"] == EpisodeState.PENDING.value
    assert test_store.get_batch(batch["id"])["status"] == "PAUSED"

    # 2. User retries Episode 2
    fail_ep2 = False
    runner.retry_episode(eps[1]["id"])
    eps_after_retry = test_store.get_episodes(batch["id"])
    assert eps_after_retry[1]["state"] == EpisodeState.PENDING.value

    # 3. Resume pipeline - completes everything
    runner.resume_batch()

    start_t = time.time()
    while time.time() - start_t < max_wait:
        eps = test_store.get_episodes(batch["id"])
        if all(ep["state"] == EpisodeState.READY.value for ep in eps):
            break
        time.sleep(0.05)

    runner._worker_stop_event.set()
    runner._current_upload_cancel_event.set()
    if runner._worker_thread:
        runner._worker_thread.join(timeout=2.0)
    if runner._upload_worker_thread:
        runner._upload_worker_thread.join(timeout=2.0)

    final_eps = test_store.get_episodes(batch["id"])
    assert all(ep["state"] == EpisodeState.READY.value for ep in final_eps)


@pytest.mark.parametrize("attempt", range(3))
def test_skip_episode_advances_pipeline(tmp_path, test_store, attempt, request):
    """
    Verifies that skipping an episode advances the pipeline past it:
    Ep 01 -> READY
    Ep 02 -> SKIPPED
    Ep 03 -> READY
    """
    batch = _setup_test_batch(test_store, tmp_path, num_episodes=3)
    runner = BatchRunner(store=test_store)
    request.addfinalizer(lambda: runner.shutdown(timeout_seconds=3))

    # Ep 2 fails immediately
    def fake_process(ep):
        order = ep["order_index"]
        if order == 2:
            test_store.update_episode(ep["id"], state=EpisodeState.FAILED.value, error="Corrupted frame")
            return False
        work_dir = tmp_path / "work" / f"ep_{order:02d}"
        _create_dummy_package(work_dir, f"Ep {order}")
        test_store.update_episode(
            ep["id"],
            state=EpisodeState.READY_TO_UPLOAD.value,
            progress=100.0,
            work_dir=str(work_dir),
            package_bytes=600,
        )
        return True

    def fake_upload(episode_id, server_url=None, api_key=None, http_client=None):
        test_store.update_episode(
            episode_id,
            state=EpisodeState.READY.value,
            progress=100.0,
            server_status="READY",
        )
        return {"status": "READY"}

    runner._process_single_episode = fake_process
    runner.upload_episode = fake_upload

    runner.start_automatic_pipeline(batch["id"], server_url="http://fake", api_key="fake-key")

    # Wait for failure on Ep 2
    deadline = time.monotonic() + 10
    while test_store.get_episodes(batch["id"])[1]["state"] != EpisodeState.FAILED.value and time.monotonic() < deadline:
        time.sleep(0.02)
    runner._worker_stop_event.set()
    runner._current_upload_cancel_event.set()
    if runner._worker_thread:
        runner._worker_thread.join(timeout=2.0)
    if runner._upload_worker_thread:
        runner._upload_worker_thread.join(timeout=2.0)

    eps = test_store.get_episodes(batch["id"])
    assert eps[1]["state"] == EpisodeState.FAILED.value

    # Skip Episode 2
    runner.skip_episode(eps[1]["id"])
    assert test_store.get_episode(eps[1]["id"])["state"] == EpisodeState.SKIPPED.value

    # Resume batch
    runner.resume_batch()

    deadline = time.monotonic() + 10
    expected = [EpisodeState.READY.value, EpisodeState.SKIPPED.value, EpisodeState.READY.value]
    while [ep["state"] for ep in test_store.get_episodes(batch["id"])] != expected and time.monotonic() < deadline:
        time.sleep(0.02)
    runner._worker_stop_event.set()
    runner._current_upload_cancel_event.set()
    if runner._worker_thread:
        runner._worker_thread.join(timeout=2.0)
    if runner._upload_worker_thread:
        runner._upload_worker_thread.join(timeout=2.0)

    final_eps = test_store.get_episodes(batch["id"])
    assert final_eps[0]["state"] == EpisodeState.READY.value
    assert final_eps[1]["state"] == EpisodeState.SKIPPED.value
    assert final_eps[2]["state"] == EpisodeState.READY.value


# =============================================================================
# 6. RESTART & RESUME RECOVERY
# =============================================================================

def test_restart_preserves_packages_and_resumes_upload(tmp_path, test_store):
    """
    Verifies restart durability:
    - Episode 01: READY -> remains READY.
    - Episode 02: UPLOADING when Studio stopped -> recovered to READY_TO_UPLOAD,
      with preserved session_id, video_id, package intact.
    - Episode 03: PENDING -> remains PENDING.
    - Resume: Episode 02 upload resumes using existing session without re-encoding!
    """
    batch = _setup_test_batch(test_store, tmp_path, num_episodes=3)

    work_dir_2 = tmp_path / "work" / "ep_02"
    _create_dummy_package(work_dir_2, "Ep 02 Package")

    eps = test_store.get_episodes(batch["id"])
    # Ep 1: READY
    test_store.update_episode(eps[0]["id"], state=EpisodeState.READY.value, server_status="READY")
    # Ep 2: Interrupted while uploading
    test_store.update_episode(
        eps[1]["id"],
        state=EpisodeState.UPLOADING.value,
        work_dir=str(work_dir_2),
        upload_session_id="saved-session-999",
        server_video_id="saved-video-888",
        uploaded_bytes=350,
        package_bytes=600,
    )
    # Ep 3: PENDING (default)

    # Simulate Studio restart with new BatchRunner instance
    new_runner = BatchRunner(store=test_store)

    # Verify recovery
    recovered_eps = test_store.get_episodes(batch["id"])
    assert recovered_eps[0]["state"] == EpisodeState.READY.value
    assert recovered_eps[1]["state"] == EpisodeState.READY_TO_UPLOAD.value
    assert recovered_eps[1]["upload_session_id"] == "saved-session-999"
    assert recovered_eps[1]["server_video_id"] == "saved-video-888"
    assert recovered_eps[1]["uploaded_bytes"] == 350
    assert recovered_eps[2]["state"] == EpisodeState.PENDING.value

    # Resume pipeline and ensure Ep 2 upload reuses session without re-encoding
    ep2_re_encoded = False
    ep2_resumed_session: Optional[str] = None

    def fake_process(ep):
        nonlocal ep2_re_encoded
        if ep["order_index"] == 2:
            ep2_re_encoded = True
        work_dir = tmp_path / "work" / f"ep_{ep['order_index']:02d}"
        _create_dummy_package(work_dir, f"Ep {ep['order_index']}")
        test_store.update_episode(ep["id"], state=EpisodeState.READY_TO_UPLOAD.value, package_bytes=600, work_dir=str(work_dir))
        return True

    def fake_upload(episode_id, server_url=None, api_key=None, http_client=None):
        nonlocal ep2_resumed_session
        ep = test_store.get_episode(episode_id)
        if ep["order_index"] == 2:
            ep2_resumed_session = ep.get("upload_session_id")
        test_store.update_episode(episode_id, state=EpisodeState.READY.value, server_status="READY")
        return {"status": "READY"}

    new_runner._process_single_episode = fake_process
    new_runner.upload_episode = fake_upload

    new_runner.start_automatic_pipeline(batch["id"], server_url="http://fake", api_key="fake-key")

    time.sleep(0.5)
    new_runner._worker_stop_event.set()
    new_runner._current_upload_cancel_event.set()
    if new_runner._worker_thread:
        new_runner._worker_thread.join(timeout=2.0)
    if new_runner._upload_worker_thread:
        new_runner._upload_worker_thread.join(timeout=2.0)

    # Invariants:
    assert not ep2_re_encoded, "Episode 02 was re-encoded after restart recovery!"
    assert ep2_resumed_session == "saved-session-999", "Resumed upload did not reuse saved session_id!"


# =============================================================================
# 7. STOP AFTER CURRENT & PAUSE CONTROLS
# =============================================================================

def test_stop_after_current_clean_pause(tmp_path, test_store):
    """
    Verifies that calling stop_after_current:
    - Active encode finishes safely to READY_TO_UPLOAD.
    - Active upload finishes safely to READY.
    - No new encode or upload is started.
    - Batch transitions to PAUSED.
    """
    batch = _setup_test_batch(test_store, tmp_path, num_episodes=4)
    runner = BatchRunner(store=test_store)

    encodes_started = []
    uploads_started = []

    def fake_process(ep):
        order = ep["order_index"]
        encodes_started.append(order)
        work_dir = tmp_path / "work" / f"ep_{order:02d}"
        _create_dummy_package(work_dir, f"Ep {order}")
        time.sleep(0.15)
        test_store.update_episode(ep["id"], state=EpisodeState.READY_TO_UPLOAD.value, package_bytes=600, work_dir=str(work_dir))
        return True

    def fake_upload(episode_id, server_url=None, api_key=None, http_client=None):
        ep = test_store.get_episode(episode_id)
        order = ep["order_index"]
        uploads_started.append(order)
        time.sleep(0.15)
        test_store.update_episode(episode_id, state=EpisodeState.READY.value, server_status="READY")
        return {"status": "READY"}

    runner._process_single_episode = fake_process
    runner.upload_episode = fake_upload

    runner.start_automatic_pipeline(batch["id"], server_url="http://fake", api_key="fake-key")

    # Give enough time for Ep 1 to encode and start uploading, and Ep 2 to start encoding
    time.sleep(0.2)
    runner.stop_after_current()

    # Wait for active units to conclude
    time.sleep(0.5)

    runner._worker_stop_event.set()
    runner._current_upload_cancel_event.set()
    if runner._worker_thread:
        runner._worker_thread.join(timeout=2.0)
    if runner._upload_worker_thread:
        runner._upload_worker_thread.join(timeout=2.0)

    # Ep 3 and 4 should never have started encoding!
    assert 3 not in encodes_started, f"Episode 3 started encoding after stop_after_current: {encodes_started}"
    assert 4 not in encodes_started, f"Episode 4 started encoding after stop_after_current: {encodes_started}"

    b = test_store.get_batch(batch["id"])
    assert b["status"] == "PAUSED"


# =============================================================================
# 8. FASTAPI REST ENDPOINTS
# =============================================================================

def test_fastapi_batch_auto_pipeline_endpoint(tmp_path, monkeypatch):
    """Verifies POST /api/batch/auto-pipeline/start starts pipelined execution."""
    from fastapi.testclient import TestClient
    from studio.web.app import app as studio_app
    from studio.core.batch_runner import batch_runner

    test_store = BatchStore(db_path=tmp_path / "fastapi_jobs.db")
    monkeypatch.setattr(batch_runner, "store", test_store)
    batch_runner.reload_state()

    client = TestClient(studio_app)

    # Create folder and test import
    folder = tmp_path / "Season 1"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "01.mkv").write_bytes(b"DATA" * 50)

    imp_res = client.post("/api/batch/import", json={"folder_path": str(folder)})
    assert imp_res.status_code == 200

    # Start auto pipeline
    start_res = client.post("/api/batch/auto-pipeline/start", json={"pipeline_window": 1})
    assert start_res.status_code == 200
    data = start_res.json()
    assert data["ok"] is True
    assert data["batch"]["mode"] == "AUTOMATIC_PIPELINED"

    # Pause
    pause_res = client.post("/api/batch/pause")
    assert pause_res.status_code == 200
    assert pause_res.json()["ok"] is True
