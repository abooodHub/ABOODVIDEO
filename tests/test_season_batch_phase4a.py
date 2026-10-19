"""Package retention policy regressions using disposable local fixtures.

Converted packages are always retained. Manual and automatic cleanup requests
must preserve source media, subtitles, fonts, HLS, poster, manifest, and DB metadata.
"""
import os
import hashlib
from pathlib import Path
import pytest

from studio.core.batch_store import BatchStore, EpisodeState
from studio.core.batch_runner import BatchRunner



@pytest.fixture
def test_env(tmp_path: Path):
    """Sets up an isolated test environment with custom temp managed root."""
    managed_temp = tmp_path / "studio_managed_temp"
    managed_temp.mkdir(parents=True, exist_ok=True)

    db_path = tmp_path / "jobs_phase4a.db"
    store = BatchStore(db_path=db_path)
    runner = BatchRunner(store=store)

    season_dir = tmp_path / "Season 1"
    season_dir.mkdir(parents=True, exist_ok=True)
    fonts_dir = season_dir / "fonts"
    fonts_dir.mkdir(parents=True, exist_ok=True)

    # Source files
    mkv_path = season_dir / "01.mkv"
    mkv_content = b"FAKE_MKV_SOURCE_VIDEO_BYTES_ABC123" * 100
    mkv_path.write_bytes(mkv_content)

    sub_path = season_dir / "01.ass"
    sub_content = "[Script Info]\nTitle: Test Arabic Sub\n\n[Events]\nDialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,مرحبا بالعالم"
    sub_path.write_text(sub_content, encoding="utf-8")

    font_path = fonts_dir / "TestFont.ttf"
    font_content = b"FAKE_TTF_FONT_BYTES_12345"
    font_path.write_bytes(font_content)

    return {
        "tmp_path": tmp_path,
        "managed_temp": managed_temp,
        "store": store,
        "runner": runner,
        "season_dir": season_dir,
        "mkv_path": mkv_path,
        "mkv_content": mkv_content,
        "sub_path": sub_path,
        "sub_content": sub_content,
        "font_path": font_path,
        "font_content": font_content,
    }


def _create_mock_package(work_dir: Path, file_sizes: dict = None) -> int:
    """Creates dummy package files inside work_dir and returns expected total bytes."""
    work_dir.mkdir(parents=True, exist_ok=True)
    hls_dir = work_dir / "720p"
    hls_dir.mkdir(parents=True, exist_ok=True)
    subs_dir = work_dir / "subtitles"
    subs_dir.mkdir(parents=True, exist_ok=True)
    pkg_fonts_dir = work_dir / "fonts"
    pkg_fonts_dir.mkdir(parents=True, exist_ok=True)

    if file_sizes is None:
        file_sizes = {
            "manifest.json": 500,
            "poster.webp": 1500,
            "720p/index.m3u8": 800,
            "720p/seg_001.ts": 50000,
            "720p/seg_002.ts": 60000,
            "subtitles/ar.ass": 1200,
            "fonts/TestFont.ttf": 25000,
        }

    total_bytes = 0
    for rel_path, size in file_sizes.items():
        fp = work_dir / rel_path
        fp.parent.mkdir(parents=True, exist_ok=True)
        content = b"X" * size
        fp.write_bytes(content)
        total_bytes += size

    return total_bytes


def _prepare_episode(test_env, state=EpisodeState.READY.value):
    store = test_env["store"]
    batch = store.create_batch(season_folder_path=str(test_env["season_dir"]))
    store.update_batch(batch["id"], status="PAUSED")
    work_dir = test_env["managed_temp"] / f"batch_{batch['id']}" / "ep_01"
    package_bytes = _create_mock_package(work_dir)
    episode = store.add_episodes(batch["id"], [{
        "source_path": str(test_env["mkv_path"]),
        "subtitle_path": str(test_env["sub_path"]),
        "order_index": 1, "state": state, "work_dir": str(work_dir),
    }])[0]
    store.update_episode(episode["id"], server_status=state, package_bytes=package_bytes)
    return batch["id"], episode["id"], work_dir


def _snapshot(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("state", [EpisodeState.READY.value, EpisodeState.READY_TO_UPLOAD.value,
                                  EpisodeState.UPLOADING.value, EpisodeState.FAILED.value])
@pytest.mark.parametrize("is_auto", [False, True], ids=["manual", "automatic"])
def test_packages_and_sources_are_retained_for_every_cleanup_request(test_env, state, is_auto):
    _, episode_id, work_dir = _prepare_episode(test_env, state)
    # SQLite is checked separately; compare only media assets byte for byte.
    before = _snapshot(test_env["season_dir"])
    package_before = _snapshot(work_dir)
    record_before = test_env["store"].get_episode(episode_id)
    with pytest.raises(ValueError, match="تحفظ الحزم المحلية"):
        test_env["runner"].clean_episode_local_package(episode_id, is_auto=is_auto)
    assert _snapshot(test_env["season_dir"]) == before
    assert _snapshot(work_dir) == package_before
    assert set(package_before) == {"manifest.json", "poster.webp", "720p/index.m3u8",
                                  "720p/seg_001.ts", "720p/seg_002.ts", "subtitles/ar.ass",
                                  "fonts/TestFont.ttf"}
    assert test_env["store"].get_episode(episode_id) == record_before


def test_restart_keeps_ready_package_without_encoding_or_upload(test_env, monkeypatch):
    batch_id, episode_id, work_dir = _prepare_episode(test_env)
    package_before = _snapshot(work_dir)
    runner = BatchRunner(store=test_env["store"])
    calls = []
    monkeypatch.setattr(runner, "_process_single_episode", lambda episode: calls.append("encode"))
    monkeypatch.setattr(runner, "upload_episode", lambda *args, **kwargs: calls.append("upload"))
    runner.reload_state()
    runner._sequential_worker_loop()
    runner._pipelined_encoder_loop()
    runner._pipelined_upload_loop()
    assert not calls
    assert not runner.is_running and not runner.is_uploading
    assert test_env["store"].get_batch(batch_id)["status"] == "PAUSED"
    assert test_env["store"].get_episode(episode_id)["state"] == EpisodeState.READY.value
    assert _snapshot(work_dir) == package_before


def test_repeated_cleanup_requests_do_not_change_package_or_accounting(test_env):
    batch_id, episode_id, work_dir = _prepare_episode(test_env)
    package_before = _snapshot(work_dir)
    for _ in range(2):
        with pytest.raises(ValueError, match="تحفظ الحزم المحلية"):
            test_env["runner"].clean_episode_local_package(episode_id)
    assert _snapshot(work_dir) == package_before
    episode = test_env["store"].get_episode(episode_id)
    assert not episode["local_package_cleaned"] and not episode["freed_bytes"]
    assert not test_env["store"].get_batch_summary(batch_id)["total_freed_bytes"]


def test_outside_directory_is_retained(test_env):
    _, episode_id, _ = _prepare_episode(test_env)
    outside = test_env["tmp_path"] / "critical_user_folder"
    outside.mkdir()
    canary = outside / "important.doc"
    canary.write_bytes(b"DO NOT DELETE")
    test_env["store"].update_episode(episode_id, work_dir=str(outside))
    with pytest.raises(ValueError, match="تحفظ الحزم المحلية"):
        test_env["runner"].clean_episode_local_package(episode_id)
    assert canary.read_bytes() == b"DO NOT DELETE"


def test_symlink_target_is_retained(test_env, monkeypatch):
    _, episode_id, work_dir = _prepare_episode(test_env)
    outside = test_env["tmp_path"] / "outside_target"
    outside.mkdir()
    canary = outside / "keep_me.txt"
    canary.write_bytes(b"KEEP")
    link = work_dir.parent / "ep_symlink"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Refusal applies before filesystem operations even without Windows symlink privileges.
        link.mkdir()
        monkeypatch.setattr(Path, "is_symlink", lambda self: self == link)
    test_env["store"].update_episode(episode_id, work_dir=str(link))
    with pytest.raises(ValueError, match="تحفظ الحزم المحلية"):
        test_env["runner"].clean_episode_local_package(episode_id)
    assert canary.read_bytes() == b"KEEP"
