"""No-media, SQLite-only acceptance checks for the Studio batch run gate."""
from __future__ import annotations

import json
import tempfile
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio.core import batch_runner as batch_runner_module
from studio.core.batch_runner import BatchRunner
from studio.core.batch_store import BatchStore, EpisodeState


def make_case(root: Path, state: str) -> tuple[BatchStore, str, str]:
    db = BatchStore(root / f"{state.lower()}.sqlite")
    batch = db.create_batch(str(root / "season"))
    episode = db.add_episodes(batch["id"], [{
        "source_path": str(root / "season" / "01.mkv"),
        "filename": "01.mkv",
        "order_index": 1,
        "state": state,
    }])[0]
    db.update_batch(batch["id"], status="PAUSED")
    if state in (EpisodeState.ENCODING.value, EpisodeState.UPLOADING.value):
        package = root / f"package-{state.lower()}"
        package.mkdir()
        payload = package / "retained.ts"
        payload.write_bytes(b"test fixture only")
        (package / "manifest.json").write_text(json.dumps({"files": [
            {"path": payload.name, "size_bytes": payload.stat().st_size},
        ]}), encoding="utf-8")
        db.update_episode(episode["id"], work_dir=str(package))
    return db, batch["id"], episode["id"]


def check_restart_matrix(root: Path) -> None:
    cases = (
        EpisodeState.PENDING.value,
        EpisodeState.FAILED.value,
        EpisodeState.READY_TO_UPLOAD.value,
        EpisodeState.ENCODING.value,
        EpisodeState.UPLOADING.value,
        EpisodeState.READY.value,
    )
    for state in cases:
        case_root = root / state.lower()
        case_root.mkdir()
        store, batch_id, episode_id = make_case(case_root, state)
        runner = BatchRunner(store=store)
        del runner
        runner = BatchRunner(store=store)  # simulated application restart
        for _ in range(4):
            runner.get_current_batch_data()  # simulated read-only polling
            time.sleep(0.01)
        assert store.get_batch(batch_id)["status"] == "PAUSED", state
        assert not runner.is_running and not runner.is_uploading, state
        assert not runner._worker_may_claim(), state
        assert store.get_episode(episode_id)["state"] != EpisodeState.ENCODING.value, state


def check_explicit_active_control(root: Path) -> None:
    case = root / "active-control"
    case.mkdir()
    store, batch_id, episode_id = make_case(case, EpisodeState.PENDING.value)
    runner = BatchRunner(store=store)
    entered = threading.Event()
    runner._sequential_worker_loop = lambda: entered.set()
    runner.start_batch(batch_id)  # explicit same-session start action
    assert entered.wait(2), "explicit start did not enter its worker"
    assert store.get_batch(batch_id)["status"] == "ACTIVE"
    fake_process = object()
    launches = []
    original = batch_runner_module.popen_headless
    batch_runner_module.popen_headless = lambda command, **kwargs: launches.append(command) or fake_process
    try:
        command = ["fake-ffmpeg", "-version"]
        result = runner._authorized_encoder_launch(batch_id, episode_id, command)
        assert result is fake_process and launches == [command]
    finally:
        batch_runner_module.popen_headless = original
        runner.pause_batch()
    assert store.get_batch(batch_id)["status"] == "PAUSED"
    try:
        runner._authorized_encoder_launch(batch_id, episode_id, ["must-not-launch"])
    except RuntimeError:
        pass
    else:
        raise AssertionError("paused batch passed the process launch gate")


def check_invalid_reupload_never_falls_back_to_encode(root: Path) -> None:
    case = root / "invalid-reupload"
    case.mkdir()
    store, batch_id, episode_id = make_case(case, EpisodeState.READY_TO_UPLOAD.value)
    runner = BatchRunner(store=store)
    runner._active_batch_id = batch_id
    runner.inspect_local_package = lambda _episode_id: {"compatible": False, "reason": "fixture package rejected"}
    try:
        runner.reupload_local_package(episode_id)
    except ValueError as exc:
        assert "fixture package rejected" in str(exc)
    else:
        raise AssertionError("invalid retained package did not stop re-upload")
    assert store.get_batch(batch_id)["status"] == "PAUSED"
    assert store.get_episode(episode_id)["state"] == EpisodeState.READY_TO_UPLOAD.value
    assert not runner.is_running and not runner.is_uploading


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="aboodvideo-pause-gate-", ignore_cleanup_errors=True) as temp:
        root = Path(temp)
        check_restart_matrix(root)
        check_explicit_active_control(root)
        check_invalid_reupload_never_falls_back_to_encode(root)
    print("PASS: six paused restart cases, four read-only polls each; fake-process ACTIVE control; paused launch denied.")


if __name__ == "__main__":
    main()
