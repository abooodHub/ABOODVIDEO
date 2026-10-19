"""Comprehensive Phase 2A Tests for ABOODVIDEO Studio Sequential Local Season Processing.

Tests:
1. Three episodes process sequentially to READY_TO_UPLOAD
2. Episode 02 never starts encoding before Episode 01 reaches READY_TO_UPLOAD
3. GPU Concurrency: exactly one active encoder at any time
4. State transitions persist to SQLite
5. Restart does not reprocess READY_TO_UPLOAD episodes
6. Processing resumes from the next unfinished episode
7. Conservative recovery for interrupted transient states on restart
8. Forced episode failure becomes FAILED
9. Retry can process the failed episode again
10. Skip changes episode to SKIPPED and continues sequential loop
11. Stop-after-current finishes current episode to READY_TO_UPLOAD then pauses
12. Pause/resume works cleanly
13. Filename RTL rendering isolation (dir="ltr", unicode-bidi isolate)
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
import pytest

from studio.core import batch_runner as batch_runner_module
from studio.core.batch_store import BatchStore, EpisodeState
from studio.core.batch_runner import BatchRunner
from studio.core.episode_scanner import scan_season_folder


def authorize_direct_test_processing(runner: BatchRunner, batch_id: str) -> None:
    """Model an explicit same-session Start click for deterministic single-episode tests."""
    runner._active_batch_id = batch_id
    runner.store.update_batch(batch_id, status="ACTIVE")
    runner._run_authorization = (runner._studio_session_id, batch_id, "test-explicit-start")
    runner._work_authorized = True
    runner._pause_requested = False
    runner._worker_stop_event.clear()
    runner._current_cancel_event.clear()


def make_tiny_video(target_path: Path, duration: float = 1.0, width: int = 640, height: int = 360) -> Path:
    """Creates a tiny valid video file using FFmpeg for lightning-fast real pipeline tests."""
    target_path = Path(target_path).resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-f", "lavfi",
        "-i", f"testsrc=size={width}x{height}:rate=25",
        "-f", "lavfi",
        "-i", "sine=frequency=1000:sample_rate=48000",
        "-t", str(duration),
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "64k",
        str(target_path),
    ]
    subprocess.run(cmd, check=True)
    return target_path


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    return tmp_path / "jobs.db"


@pytest.fixture
def sample_season_dir(tmp_path: Path) -> Path:
    season_dir = tmp_path / "Season_Test"
    season_dir.mkdir(parents=True, exist_ok=True)
    make_tiny_video(season_dir / "01.mkv", duration=1.0)
    make_tiny_video(season_dir / "02.mkv", duration=1.0)
    make_tiny_video(season_dir / "03.mkv", duration=1.0)
    return season_dir


@pytest.fixture(autouse=True)
def isolated_converted_output(tmp_path: Path, monkeypatch):
    """Keep processing tests inside their disposable pytest workspace."""
    output_root = tmp_path / "converted-output"
    output_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(batch_runner_module, "load_studio_config", lambda: {
        "default_encoder": "cpu",
        "default_preset": "balanced",
        "converted_output_folder": str(output_root),
        "include_360p": True,
    })


# ==============================================================================
# 1 & 2 & 3 & 4: Sequential processing, concurrency = 1, and SQLite persistence
# ==============================================================================
def test_three_episodes_process_sequentially_and_concurrency(sample_season_dir: Path, temp_db: Path):
    """Test 3 episodes process sequentially in order with concurrency lock and reach READY_TO_UPLOAD."""
    store = BatchStore(db_path=temp_db)
    runner = BatchRunner(store=store)

    runner.import_season_folder(sample_season_dir, destination_label="Test / Season 1")
    batch_id = runner.active_batch_id
    assert batch_id is not None

    # Track episode start and finish timestamps to verify strict sequential execution
    execution_timeline = {}

    orig_process_single = runner._process_single_episode

    def tracking_process_single(ep):
        ep_id = ep["id"]
        order = ep["order_index"]
        execution_timeline[order] = {"start": time.time()}
        res = orig_process_single(ep)
        execution_timeline[order]["end"] = time.time()
        return res

    runner._process_single_episode = tracking_process_single

    # Start batch
    runner.start_batch()
    # Wait for completion (3x 1s videos = ~2-3s total)
    timeout = 30.0
    start_t = time.time()
    while runner.is_running and time.time() - start_t < timeout:
        time.sleep(0.2)

    assert not runner.is_running, "Batch worker did not complete within timeout!"

    data = runner.get_current_batch_data()
    assert data["batch"]["status"] == "COMPLETED_LOCAL"
    episodes = data["episodes"]
    assert len(episodes) == 3

    for idx, ep in enumerate(episodes, start=1):
        assert ep["order_index"] == idx
        assert ep["state"] == EpisodeState.READY_TO_UPLOAD.value
        assert ep["progress"] == 100.0
        assert ep["package_bytes"] > 0
        # Verify work_dir has manifest.json
        manifest_file = Path(ep["work_dir"]) / "manifest.json"
        assert manifest_file.exists()

    # Verify sequential invariant: ep2 starts strictly after ep1 ends, ep3 after ep2 ends
    assert 1 in execution_timeline and 2 in execution_timeline and 3 in execution_timeline
    assert execution_timeline[2]["start"] >= execution_timeline[1]["end"] - 0.05
    assert execution_timeline[3]["start"] >= execution_timeline[2]["end"] - 0.05


# ==============================================================================
# 5 & 6: Restart does not reprocess READY_TO_UPLOAD and resumes unfinished
# ==============================================================================
def test_restart_preserves_ready_to_upload_and_resumes(sample_season_dir: Path, temp_db: Path):
    """Episode 01 reaches READY_TO_UPLOAD. After full Studio restart, it is NOT re-encoded."""
    store1 = BatchStore(db_path=temp_db)
    runner1 = BatchRunner(store=store1)
    runner1.import_season_folder(sample_season_dir)

    # Process only episode 1
    episodes = store1.get_episodes(runner1.active_batch_id)
    ep1 = episodes[0]
    authorize_direct_test_processing(runner1, runner1.active_batch_id)
    success = runner1._process_single_episode(ep1)
    assert success is True, runner1.store.get_episode(ep1["id"]).get("error")

    ep1_db = store1.get_episode(ep1["id"])
    assert ep1_db["state"] == EpisodeState.READY_TO_UPLOAD.value
    ep1_manifest_mtime = (Path(ep1_db["work_dir"]) / "manifest.json").stat().st_mtime

    # Simulate Studio complete shutdown
    del runner1
    del store1

    # Studio Restart: new store and runner instance
    store2 = BatchStore(db_path=temp_db)
    runner2 = BatchRunner(store=store2)

    data = runner2.get_current_batch_data()
    assert data["active"] is True
    episodes_after = data["episodes"]
    assert episodes_after[0]["state"] == EpisodeState.READY_TO_UPLOAD.value

    # Track calls to ensure Episode 01 is skipped
    processed_orders = []
    orig_process = runner2._process_single_episode

    def spy_process(ep):
        processed_orders.append(ep["order_index"])
        return orig_process(ep)

    runner2._process_single_episode = spy_process

    # Run remaining batch
    runner2.start_batch()
    timeout = 30.0
    start_t = time.time()
    while runner2.is_running and time.time() - start_t < timeout:
        time.sleep(0.2)

    # Confirm Episode 01 was NOT processed again
    assert 1 not in processed_orders, "Episode 01 was incorrectly re-processed after restart!"
    assert 2 in processed_orders and 3 in processed_orders

    # Confirm Episode 01 manifest was untouched
    ep1_after_mtime = (Path(ep1_db["work_dir"]) / "manifest.json").stat().st_mtime
    assert ep1_manifest_mtime == ep1_after_mtime


# ==============================================================================
# 7: Conservative recovery of interrupted transient states on restart
# ==============================================================================
def test_conservative_restart_recovery_for_transient_state(sample_season_dir: Path, temp_db: Path):
    """An interrupted encode returns to idle PENDING and requires explicit Start."""
    store1 = BatchStore(db_path=temp_db)
    runner1 = BatchRunner(store=store1)
    runner1.import_season_folder(sample_season_dir)
    batch_id = runner1.active_batch_id
    episodes = store1.get_episodes(batch_id)

    # Simulate episode 2 being interrupted mid-encoding
    store1.update_episode(
        episodes[1]["id"],
        state=EpisodeState.ENCODING.value,
        current_stage="ENCODING",
        work_dir=str(sample_season_dir / "nonexistent_incomplete_dir"),
    )

    del runner1
    del store1

    # Restart Studio
    store2 = BatchStore(db_path=temp_db)
    runner2 = BatchRunner(store=store2)

    ep2 = store2.get_episode(episodes[1]["id"])
    assert ep2["state"] == EpisodeState.PENDING.value
    assert store2.get_batch(batch_id)["status"] == "PAUSED"
    assert runner2.is_running is False


# ==============================================================================
# 8 & 9: Forced failure and Retry
# ==============================================================================
def test_forced_episode_failure_and_retry(sample_season_dir: Path, temp_db: Path):
    """A failed episode halts sequential loop, user clicks Retry, and episode succeeds."""
    store = BatchStore(db_path=temp_db)
    runner = BatchRunner(store=store)
    runner.import_season_folder(sample_season_dir)
    episodes = store.get_episodes(runner.active_batch_id)

    # Force error by pointing source_path of ep 1 to non-existent file
    store.update_episode(episodes[0]["id"], source_path=str(sample_season_dir / "missing.mkv"))

    runner.start_batch()
    time.sleep(0.8)
    assert not runner.is_running

    ep1_failed = store.get_episode(episodes[0]["id"])
    assert ep1_failed["state"] == EpisodeState.FAILED.value
    assert ep1_failed["error"] is not None

    # Fix file and call retry_episode
    store.update_episode(episodes[0]["id"], source_path=str(sample_season_dir / "01.mkv"))
    runner.retry_episode(episodes[0]["id"])

    ep1_retried = store.get_episode(episodes[0]["id"])
    assert ep1_retried["state"] == EpisodeState.PENDING.value
    assert ep1_retried["error"] is None

    # Resume and verify success
    runner.resume_batch()
    timeout = 30.0
    start_t = time.time()
    while runner.is_running and time.time() - start_t < timeout:
        time.sleep(0.2)

    assert store.get_episode(episodes[0]["id"])["state"] == EpisodeState.READY_TO_UPLOAD.value


# ==============================================================================
# 10: Skip episode
# ==============================================================================
def test_skip_episode_and_continue(sample_season_dir: Path, temp_db: Path):
    """Skipping an episode moves it to SKIPPED and allows the remaining queue to finish."""
    store = BatchStore(db_path=temp_db)
    runner = BatchRunner(store=store)
    runner.import_season_folder(sample_season_dir)
    episodes = store.get_episodes(runner.active_batch_id)

    # Skip episode 1
    runner.skip_episode(episodes[0]["id"])
    assert store.get_episode(episodes[0]["id"])["state"] == EpisodeState.SKIPPED.value

    runner.start_batch()
    timeout = 30.0
    start_t = time.time()
    while runner.is_running and time.time() - start_t < timeout:
        time.sleep(0.2)

    assert store.get_episode(episodes[0]["id"])["state"] == EpisodeState.SKIPPED.value
    assert store.get_episode(episodes[1]["id"])["state"] == EpisodeState.READY_TO_UPLOAD.value
    assert store.get_episode(episodes[2]["id"])["state"] == EpisodeState.READY_TO_UPLOAD.value


# ==============================================================================
# 11: Stop after current
# ==============================================================================
def test_stop_after_current(sample_season_dir: Path, temp_db: Path):
    """Stop-after-current allows current episode to reach READY_TO_UPLOAD then pauses before next."""
    store = BatchStore(db_path=temp_db)
    runner = BatchRunner(store=store)
    runner.import_season_folder(sample_season_dir)
    episodes = store.get_episodes(runner.active_batch_id)

    # Signal stop after current right at start
    runner.stop_after_current()
    runner.start_batch()

    timeout = 30.0
    start_t = time.time()
    while runner.is_running and time.time() - start_t < timeout:
        time.sleep(0.2)

    # Episode 1 must be finished to READY_TO_UPLOAD
    assert store.get_episode(episodes[0]["id"])["state"] == EpisodeState.READY_TO_UPLOAD.value
    # Episode 2 must remain PENDING
    assert store.get_episode(episodes[1]["id"])["state"] == EpisodeState.PENDING.value
    assert store.get_batch(runner.active_batch_id)["status"] == "PAUSED"


# ==============================================================================
# 12: Pause and resume
# ==============================================================================
def test_pause_and_resume(sample_season_dir: Path, temp_db: Path):
    """Pause stops between episodes, resume continues remaining episodes."""
    store = BatchStore(db_path=temp_db)
    runner = BatchRunner(store=store)
    runner.import_season_folder(sample_season_dir)
    episodes = store.get_episodes(runner.active_batch_id)

    # Process episode 1 manually
    authorize_direct_test_processing(runner, runner.active_batch_id)
    runner._process_single_episode(episodes[0])
    assert store.get_episode(episodes[0]["id"])["state"] == EpisodeState.READY_TO_UPLOAD.value, store.get_episode(episodes[0]["id"]).get("error")

    # Set pause
    runner.pause_batch()
    assert store.get_batch(runner.active_batch_id)["status"] == "PAUSED"

    # Resume
    runner.resume_batch()
    timeout = 30.0
    start_t = time.time()
    while runner.is_running and time.time() - start_t < timeout:
        time.sleep(0.2)

    assert store.get_episode(episodes[1]["id"])["state"] == EpisodeState.READY_TO_UPLOAD.value
    assert store.get_episode(episodes[2]["id"])["state"] == EpisodeState.READY_TO_UPLOAD.value


# ==============================================================================
# 13: Filename RTL rendering isolation verification
# ==============================================================================
def test_filename_rtl_rendering_fix():
    """Verify that filenames use explicit LTR isolation to prevent BiDi 'mkv.01' inversion."""
    css_path = Path("studio/web/static/css/studio.css")
    js_path = Path("studio/web/static/js/studio.js")

    assert css_path.exists()
    assert js_path.exists()

    css_content = css_path.read_text(encoding="utf-8")
    js_content = js_path.read_text(encoding="utf-8")

    # CSS must define LTR isolation
    assert ".ltr-isolate" in css_content
    assert "direction: ltr" in css_content
    assert "unicode-bidi: isolate" in css_content

    # JS must wrap filename with ltr-isolate class and dir="ltr"
    assert "ltr-isolate" in js_content
    assert 'bdiName.setAttribute("dir", "ltr")' in js_content or 'dir="ltr"' in js_content
